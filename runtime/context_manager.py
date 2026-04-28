"""Context Manager for Agent Runtime.

Manages multi-turn conversation context, session persistence, rolling summaries,
structured memory extraction, and context assembly. Uses only the Python standard
library — zero third-party dependencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ConversationTurn:
    """A single turn in a multi-turn conversation.

    Attributes:
        role: Message role — "user", "assistant", or "tool".
        content: Text content of the turn.
        timestamp: ISO 8601 timestamp string (e.g. "2026-04-15T14:30:22").
        name: Optional tool/function name (used for tool-role turns).
        tool_calls: Optional list of tool call dicts for parallel tool calls.
    """

    role: str
    content: str
    timestamp: str
    name: Optional[str] = None
    tool_calls: Optional[list[dict]] = None
    thinking: Optional[str] = None
    stat: Optional[dict] = None
    images: Optional[list] = None
    audio: Optional[str] = None
    prompt_template: Optional[str] = None
    arguments: Optional[dict] = None


@dataclass
class MemoryEntry:
    """A single structured memory entry extracted from a conversation.

    Attributes:
        entry_type: Category — "fact", "preference", "decision", or "entity".
        content: Human-readable description of the memory.
        source_turn_index: Index of the conversation turn this was extracted from.
        confidence: Confidence score in [0.0, 1.0].
        created_at: ISO 8601 timestamp when this entry was created.
    """

    entry_type: str
    content: str
    source_turn_index: int
    confidence: float
    created_at: str


@dataclass
class IntrospectionSnapshot:
    """Observability snapshot of the current context management state.

    Attributes:
        session_id: Unique session identifier.
        total_turns: Total number of conversation turns recorded.
        summarized_turns: Number of turns compressed into the rolling summary.
        recent_window_size: Number of recent turns retained in full (≤ K).
        memory_entry_count: Total number of structured memory entries.
        memory_entries_by_type: Count of entries per entry_type.
        summary_version: Rolling summary version (0 = no summary yet).
        estimated_context_tokens: Estimated token count of the assembled context.
        token_budget: Optional token budget limit.
    """

    session_id: str
    total_turns: int
    summarized_turns: int
    recent_window_size: int
    memory_entry_count: int
    memory_entries_by_type: dict[str, int]
    summary_version: int
    estimated_context_tokens: int
    token_budget: Optional[int]


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Rough token count estimate: characters / 4.

    This is intentionally approximate and used only for budget control.
    """
    return len(text) // 4


# ---------------------------------------------------------------------------
# Lightweight YAML front-matter parser
# ---------------------------------------------------------------------------


def _parse_yaml_value(raw: str) -> object:
    """Parse a single scalar YAML value (string or integer)."""
    raw = raw.strip()
    # Quoted string
    if (raw.startswith('"') and raw.endswith('"')) or (
        raw.startswith("'") and raw.endswith("'")
    ):
        return raw[1:-1]
    # Integer
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    # Unquoted string (including empty)
    return raw


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Parse a front-matter + body document.

    The document must start with ``---`` on its own line, followed by YAML
    key-value pairs, and closed by another ``---`` line.  Everything after
    the closing ``---`` is returned as *body_text*.

    Supported YAML subset:
    - String values (quoted or unquoted)
    - Integer values
    - Lists (``- item`` format, one item per line)
    - Nested dicts (indented ``key: value`` pairs)

    Args:
        text: Raw document text.

    Returns:
        A ``(yaml_dict, body_text)`` tuple.

    Raises:
        ValueError: When the front-matter is missing, malformed, or the
            closing ``---`` delimiter is absent.
    """
    if not text.startswith("---"):
        raise ValueError(
            "Invalid front-matter: document must start with '---' delimiter"
        )

    # Find the closing ---
    # The opening --- is at position 0; search for the next --- after it.
    rest = text[3:]  # skip opening ---
    # Allow optional newline right after opening ---
    if rest.startswith("\r\n"):
        rest = rest[2:]
    elif rest.startswith("\n"):
        rest = rest[1:]
    else:
        raise ValueError(
            "Invalid front-matter: '---' must be followed by a newline"
        )

    close_match = re.search(r"^---[ \t]*$", rest, re.MULTILINE)
    if close_match is None:
        raise ValueError(
            "Invalid front-matter: missing closing '---' delimiter"
        )

    yaml_block = rest[: close_match.start()]
    body_text = rest[close_match.end():]
    # Strip leading newline from body
    if body_text.startswith("\r\n"):
        body_text = body_text[2:]
    elif body_text.startswith("\n"):
        body_text = body_text[1:]

    result = _parse_yaml_block(yaml_block, indent=0)
    return result, body_text


def _parse_yaml_block(block: str, indent: int) -> dict:
    """Recursively parse an indented YAML block into a dict."""
    result: dict = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        # Skip blank lines
        if not line.strip():
            i += 1
            continue

        # Determine current line's indentation
        stripped = line.lstrip(" ")
        current_indent = len(line) - len(stripped)

        if current_indent != indent:
            # This line belongs to a different (outer) scope — stop
            break

        # List item at this indent level (shouldn't normally happen at top
        # level, but handle gracefully)
        if stripped.startswith("- "):
            raise ValueError(
                f"Invalid front-matter: unexpected list item at indent {indent}: {line!r}"
            )

        # Key: value pair
        if ":" not in stripped:
            raise ValueError(
                f"Invalid front-matter: expected 'key: value' but got: {line!r}"
            )

        colon_pos = stripped.index(":")
        key = stripped[:colon_pos].strip()
        value_part = stripped[colon_pos + 1:]

        if not key:
            raise ValueError(
                f"Invalid front-matter: empty key in line: {line!r}"
            )

        # Peek ahead to determine value type
        # Case 1: value on same line (scalar, or inline [] / {})
        if value_part.strip():
            inline = value_part.strip()
            if inline == "[]":
                result[key] = []
            elif inline == "{}":
                result[key] = {}
            else:
                result[key] = _parse_yaml_value(value_part)
            i += 1
            continue

        # Case 2: value is empty — look ahead for list items or nested dict
        # Collect continuation lines (deeper indent)
        j = i + 1
        child_lines = []
        while j < len(lines):
            next_line = lines[j]
            if not next_line.strip():
                j += 1
                continue
            next_stripped = next_line.lstrip(" ")
            next_indent = len(next_line) - len(next_stripped)
            if next_indent <= indent:
                break
            child_lines.append(next_line)
            j += 1

        if not child_lines:
            # Empty value
            result[key] = ""
            i += 1
            continue

        # Determine if child is a list or nested dict
        first_child = child_lines[0].lstrip(" ")
        child_indent = len(child_lines[0]) - len(child_lines[0].lstrip(" "))

        if first_child.startswith("- "):
            # List
            items = []
            for cl in child_lines:
                cl_stripped = cl.lstrip(" ")
                cl_indent = len(cl) - len(cl_stripped)
                if cl_indent == child_indent and cl_stripped.startswith("- "):
                    items.append(_parse_yaml_value(cl_stripped[2:]))
                elif cl_indent > child_indent:
                    raise ValueError(
                        f"Invalid front-matter: unexpected indentation in list under key '{key}': {cl!r}"
                    )
                else:
                    raise ValueError(
                        f"Invalid front-matter: inconsistent list indentation under key '{key}': {cl!r}"
                    )
            result[key] = items
        else:
            # Nested dict
            child_block = "\n".join(child_lines)
            result[key] = _parse_yaml_block(child_block, indent=child_indent)

        i = j

    return result


def _serialize_yaml_value(value: object, indent: int = 0) -> str:
    """Serialize a Python value to a YAML front-matter string fragment.

    Returns the serialized text.  For dicts and non-empty lists the returned
    string is multi-line and already includes the *indent* prefix on every
    line.  For scalars and empty collections it returns a single-line string
    (no leading indent).
    """
    prefix = " " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = []
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                child = _serialize_yaml_value(v, indent=indent + 2)
                lines.append(f"{prefix}{k}:\n{child}")
            else:
                lines.append(f"{prefix}{k}: {_serialize_yaml_value(v)}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = []
        for item in value:
            lines.append(f"{prefix}- {_serialize_yaml_value(item)}")
        return "\n".join(lines)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    # String — quote if it contains special characters or is empty
    s = str(value)
    if not s or any(c in s for c in (':', '#', '"', "'", '\n', '\r')):
        escaped = s.replace('"', '\\"')
        return f'"{escaped}"'
    return s


def _build_front_matter(front_matter: dict) -> str:
    """Render a dict as a YAML front-matter block (between --- delimiters)."""
    lines = ["---"]
    for key, value in front_matter.items():
        if isinstance(value, list) and value:
            # Non-empty list: key on its own line, items indented below
            serialized = _serialize_yaml_value(value, indent=2)
            lines.append(f"{key}:")
            lines.append(serialized)
        elif isinstance(value, dict) and value:
            # Non-empty dict: key on its own line, children indented below
            serialized = _serialize_yaml_value(value, indent=2)
            lines.append(f"{key}:")
            lines.append(serialized)
        else:
            # Scalar, empty list "[]", empty dict "{}"
            lines.append(f"{key}: {_serialize_yaml_value(value)}")
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Conversation serialization / deserialization
# ---------------------------------------------------------------------------


def serialize_conversation(
    turns: list[ConversationTurn],
    front_matter: dict,
) -> str:
    """Serialize conversation turns to a front-matter + Markdown string.

    The front-matter contains the fields provided in *front_matter*.
    Each turn is rendered as::

        ## Turn {i} [{timestamp}]
        **role:** {role}

        {content}

    Args:
        turns: List of :class:`ConversationTurn` objects.
        front_matter: Dict with keys such as ``session_id``, ``created_at``,
            ``updated_at``, ``turn_count``, ``references``.

    Returns:
        A string with YAML front-matter followed by Markdown body.
    """
    header = _build_front_matter(front_matter)
    body_parts = []
    for i, turn in enumerate(turns):
        section = f"## Turn {i} [{turn.timestamp}]\n**role:** {turn.role}\n\n{turn.content}\n"
        body_parts.append(section)
    body = "\n".join(body_parts)
    return header + "\n" + body


def parse_conversation(text: str) -> tuple[dict, list[ConversationTurn]]:
    """Parse a front-matter + Markdown string back to structured conversation data.

    Args:
        text: Raw document text produced by :func:`serialize_conversation`.

    Returns:
        A ``(front_matter_dict, list[ConversationTurn])`` tuple.

    Raises:
        ValueError: When the input is missing ``---`` delimiters, has malformed
            YAML, or contains truncated / malformed turn sections.
    """
    try:
        front_matter, body = parse_front_matter(text)
    except ValueError:
        raise  # re-raise with original message

    turns: list[ConversationTurn] = []

    if not body.strip():
        return front_matter, turns

    # Split body into turn sections using "## Turn N [timestamp]" headers
    turn_pattern = re.compile(
        r"^## Turn (\d+) \[([^\]]*)\][ \t]*$", re.MULTILINE
    )
    matches = list(turn_pattern.finditer(body))

    if not matches:
        # Body has content but no turn headers — malformed
        raise ValueError(
            "Invalid conversation body: no '## Turn N [timestamp]' headers found"
        )

    for idx, match in enumerate(matches):
        turn_index = int(match.group(1))
        timestamp = match.group(2)

        # Extract section content between this header and the next
        section_start = match.end()
        section_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        section = body[section_start:section_end]

        # Parse **role:** line
        role_match = re.search(r"^\*\*role:\*\*\s*(.+)$", section, re.MULTILINE)
        if role_match is None:
            raise ValueError(
                f"Invalid conversation body: missing '**role:**' in Turn {turn_index}"
            )
        role = role_match.group(1).strip()

        # Content is everything after the role line — strip only surrounding newlines
        role_end = role_match.end()
        content_raw = section[role_end:]
        # Strip leading newline(s) and trailing newline(s) only
        content = content_raw.lstrip("\n\r").rstrip("\n\r")

        turns.append(
            ConversationTurn(
                role=role,
                content=content,
                timestamp=timestamp,
            )
        )

    return front_matter, turns


# ---------------------------------------------------------------------------
# Tool call serialization
# ---------------------------------------------------------------------------


def serialize_tool_call(
    front_matter: dict,
    arguments: dict,
    result: str,
) -> str:
    """Serialize a tool call record to a front-matter + Markdown string.

    The Markdown body contains ``## Arguments`` and ``## Result`` sections.

    Args:
        front_matter: Dict with keys such as ``tool_name``, ``session_id``,
            ``turn_index``, ``timestamp``.
        arguments: Tool call arguments dict (serialized as JSON in the body).
        result: Tool call result string.

    Returns:
        A string with YAML front-matter followed by Markdown body.
    """
    import json

    header = _build_front_matter(front_matter)
    args_json = json.dumps(arguments, ensure_ascii=False, indent=2)
    body = (
        f"## Arguments\n\n```json\n{args_json}\n```\n\n"
        f"## Result\n\n```\n{result}\n```\n"
    )
    return header + "\n" + body


# ---------------------------------------------------------------------------
# Summary serialization
# ---------------------------------------------------------------------------


def serialize_summary(front_matter: dict, summary_text: str) -> str:
    """Serialize a rolling summary to a front-matter + Markdown string.

    Args:
        front_matter: Dict with keys such as ``session_id``,
            ``summary_version``, ``summarized_up_to_turn``, ``updated_at``.
        summary_text: The summary body text.

    Returns:
        A string with YAML front-matter followed by the summary body.
    """
    header = _build_front_matter(front_matter)
    return header + "\n" + summary_text


def serialize_memory(front_matter: dict, entries: list) -> str:
    """Serialize structured memory entries to a front-matter + JSON string.

    Args:
        front_matter: Dict with keys such as ``session_id``,
            ``entry_count``, ``updated_at``.
        entries: List of :class:`MemoryEntry` objects.

    Returns:
        A string with YAML front-matter followed by a JSON array body.
    """
    import json as _json

    header = _build_front_matter(front_matter)
    body = _json.dumps(
        [
            {
                "entry_type": e.entry_type,
                "content": e.content,
                "source_turn_index": e.source_turn_index,
                "confidence": e.confidence,
                "created_at": e.created_at,
            }
            for e in entries
        ],
        ensure_ascii=False,
        indent=2,
    )
    return header + "\n" + body


def _extract_tagged_block(text: str, tag: str) -> str:
    """Extract the content between ``<tag>`` and ``</tag>`` in *text*.

    Returns an empty string when the tags are not found.
    """
    pattern = re.compile(
        rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>",
        re.DOTALL,
    )
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# ContextManager
# ---------------------------------------------------------------------------

import datetime
import json
import logging
import os
import tempfile
from typing import Callable, Optional


class ContextManager:
    """Manages multi-turn conversation sessions: directory creation, file I/O,
    tool call recording, and artifact storage.

    Args:
        infer_fn: Callable used for LLM-assisted operations (summarization,
            memory extraction).  Signature: ``(request) -> result``.
        chats_dir: Base directory for session storage.  Defaults to
            ``"./chats"``.
        recent_turns_k: Number of recent turns to retain in full when
            assembling context.
        summary_model_id: Hard-coded override for the model ID used for
            rolling summaries.  When non-empty, takes precedence over the
            ``SUMMARY_MODEL_ID`` environment variable.  Leave empty (default)
            to rely solely on the environment variable, which is re-read on
            every call so changes take effect without a restart.
        max_tokens_in_context: Hard-coded override for the token threshold
            that triggers compression.  When not ``None``, takes precedence
            over the ``MAX_TOKENS_IN_CONTEXT`` environment variable.  Leave
            as ``None`` (default) to rely solely on the environment variable,
            which is re-read on every call.  Default when neither is set: 65536.
        memory_confidence_threshold: Minimum confidence score for a
            ``MemoryEntry`` to be retained.
    """

    _DEFAULT_MAX_TOKENS: int = 65536

    def __init__(
        self,
        infer_fn: Callable,
        chats_dir: str = "./chats",
        recent_turns_k: int = 10,
        summary_model_id: str = "",
        max_tokens_in_context: Optional[int] = None,
        memory_confidence_threshold: float = 0.7,
    ) -> None:
        self._infer_fn = infer_fn
        self._chats_dir = chats_dir
        self._recent_turns_k = recent_turns_k
        # Store constructor overrides; None / "" means "defer to env var at call time"
        self._summary_model_id_override: str = summary_model_id
        self._max_tokens_override: Optional[int] = max_tokens_in_context
        self._memory_confidence_threshold = memory_confidence_threshold
        self._memory_store: dict[str, list[MemoryEntry]] = {}

    # ------------------------------------------------------------------
    # Dynamic configuration properties (re-read env vars on every access)
    # ------------------------------------------------------------------

    @property
    def _summary_model_id(self) -> str:
        """Return the effective summary model ID.

        Priority: constructor override > ``SUMMARY_MODEL_ID`` env var > ``""``
        (Phase 1 / storage-only mode).
        """
        if self._summary_model_id_override:
            return self._summary_model_id_override
        return os.environ.get("SUMMARY_MODEL_ID", "")

    @property
    def _max_tokens_in_context(self) -> int:
        """Return the effective token threshold for compression.

        Priority: constructor override > ``MAX_TOKENS_IN_CONTEXT`` env var >
        65536 (default).  Invalid env-var values are ignored with a warning
        and the default is used instead.
        """
        if self._max_tokens_override is not None:
            return self._max_tokens_override
        env_val = os.environ.get("MAX_TOKENS_IN_CONTEXT", "")
        if env_val.strip():
            try:
                return int(env_val)
            except ValueError:
                logging.warning(
                    "ContextManager: invalid MAX_TOKENS_IN_CONTEXT value %r, "
                    "using default %d",
                    env_val,
                    self._DEFAULT_MAX_TOKENS,
                )
        return self._DEFAULT_MAX_TOKENS

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def create_session(self) -> str:
        """Create a new session directory and return the session_id.

        The session_id is a timestamp string in ``YYYY-MM-DD_HH-MM-SS``
        format.  The ``/chats`` base directory is created automatically if it
        does not exist.

        Returns:
            The session_id string.
        """
        session_id = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        session_dir = os.path.join(self._chats_dir, session_id)
        os.makedirs(session_dir, exist_ok=True)
        return session_id

    def session_exists(self, session_id: str) -> bool:
        """Return ``True`` if the session directory exists."""
        session_dir = os.path.join(self._chats_dir, session_id)
        return os.path.isdir(session_dir)

    def recover_session(self, session_id: str) -> bool:
        """Attempt to recover a session whose directory is missing.

        When the server restarts (or the session was created on a different
        instance), the in-memory state is gone but the ``chat_data`` directory
        may still exist on disk.  This method ensures the directory is present
        so that :meth:`assemble_context` can load the persisted history and
        :meth:`save_conversation` can write without error.

        If the directory does not exist under ``_chats_dir`` at all, it is
        created as an empty session (equivalent to :meth:`create_session` for
        an externally supplied ID).

        Args:
            session_id: The session ID supplied by the client.

        Returns:
            ``True`` if the session directory already existed on disk (genuine
            recovery), ``False`` if it had to be created from scratch.
        """
        session_dir = os.path.join(self._chats_dir, session_id)
        existed = os.path.isdir(session_dir)
        os.makedirs(session_dir, exist_ok=True)
        return existed

    # ------------------------------------------------------------------
    # Conversation file I/O
    # ------------------------------------------------------------------

    def _conversation_path(self, session_id: str) -> str:
        return os.path.join(self._chats_dir, session_id, "conversation.json")

    def save_conversation(
        self,
        session_id: str,
        turns: list[ConversationTurn],
        last_total_tokens: Optional[int] = None,
    ) -> None:
        """Serialize *turns* and atomically write to ``conversation.json``.

        The file is a JSON object with a ``meta`` block (session metadata) and
        a ``messages`` array where each element maps directly to a conversation
        turn.  Pretty-printed for human readability.

        Args:
            session_id: Target session.
            turns: Conversation turns to persist.
            last_total_tokens: Total token count (prompt + completion) of the
                most recent inference round.  Stored in ``meta`` so that
                ``update_rolling_summary`` can use it as a compression trigger.
        """
        conv_path = self._conversation_path(session_id)

        # Preserve created_at from existing file when available.
        existing_created_at: Optional[str] = None
        if os.path.isfile(conv_path):
            try:
                with open(conv_path, "r", encoding="utf-8") as fh:
                    existing = json.load(fh)
                existing_created_at = existing.get("meta", {}).get("created_at")
            except (ValueError, OSError, KeyError):
                pass

        now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        meta: dict = {
            "session_id": session_id,
            "created_at": existing_created_at or now,
            "updated_at": now,
            "turn_count": len(turns),
        }
        if last_total_tokens is not None:
            meta["last_total_tokens"] = last_total_tokens

        messages = []
        for turn in turns:
            msg: dict = {
                "role": turn.role,
                "content": turn.content,
                "timestamp": turn.timestamp,
            }
            if turn.name:
                msg["name"] = turn.name
            if turn.tool_calls:
                msg["tool_calls"] = turn.tool_calls
            if turn.thinking:
                msg["thinking"] = turn.thinking
            if turn.stat:
                msg["stat"] = turn.stat
            if turn.images:
                msg["images"] = turn.images
            if turn.audio:
                msg["audio"] = turn.audio
            if turn.prompt_template:
                msg["prompt_template"] = turn.prompt_template
            if turn.arguments:
                msg["arguments"] = turn.arguments
            messages.append(msg)

        data = {"meta": meta, "messages": messages}
        text = json.dumps(data, ensure_ascii=False, indent=2)
        self._atomic_write(conv_path, text)

    def load_conversation(self, session_id: str) -> list[ConversationTurn]:
        """Read and deserialize ``conversation.json`` for *session_id*.

        Returns:
            List of :class:`ConversationTurn` objects.

        Raises:
            FileNotFoundError: When the conversation file does not exist.
            ValueError: When the file content is malformed JSON.
        """
        conv_path = self._conversation_path(session_id)
        with open(conv_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        turns: list[ConversationTurn] = []
        for msg in data.get("messages", []):
            turns.append(ConversationTurn(
                role=msg["role"],
                content=msg.get("content", ""),
                timestamp=msg.get("timestamp", ""),
                name=msg.get("name"),
                tool_calls=msg.get("tool_calls"),
                thinking=msg.get("thinking"),
                stat=msg.get("stat"),
                images=msg.get("images"),
                audio=msg.get("audio"),
                prompt_template=msg.get("prompt_template"),
                arguments=msg.get("arguments"),
            ))
        return turns

    # ------------------------------------------------------------------
    # Tool call recording
    # ------------------------------------------------------------------

    def record_tool_call(
        self,
        session_id: str,
        turn_index: int,
        tool_name: str,
        arguments: dict,
        result: str,
        timestamp: str,
    ) -> str:
        """No-op — tool call results are now stored inline in ``conversation.json``.

        Kept for API compatibility; callers should be updated to stop calling this.
        Returns an empty string.
        """
        return ""

    # ------------------------------------------------------------------
    # Rolling summary
    # ------------------------------------------------------------------

    def _summary_path(self, session_id: str) -> str:
        return os.path.join(self._chats_dir, session_id, "summary.md")

    def _memory_path(self, session_id: str) -> str:
        return os.path.join(self._chats_dir, session_id, "memory.md")

    def get_last_total_tokens(self, session_id: str) -> Optional[int]:
        """Return the ``last_total_tokens`` value from ``conversation.json`` meta.

        Returns ``None`` when the file does not exist or the field is absent.
        """
        conv_path = self._conversation_path(session_id)
        if not os.path.isfile(conv_path):
            return None
        try:
            with open(conv_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            val = data.get("meta", {}).get("last_total_tokens")
            return int(val) if isinstance(val, int) else None
        except (ValueError, OSError):
            return None

    def update_rolling_summary(
        self, session_id: str, turns: list[ConversationTurn],
        last_total_tokens: Optional[int] = None,
    ) -> None:
        """Trigger context compression for *session_id* if the token threshold is exceeded.

        Delegates to :meth:`compress_context`.  Kept for backward compatibility.
        """
        self.compress_context(session_id, turns, last_total_tokens=last_total_tokens)

    # ------------------------------------------------------------------
    # Memory persistence
    # ------------------------------------------------------------------

    def save_memory(self, session_id: str, entries: list[MemoryEntry]) -> None:
        """Persist *entries* to ``memory.md`` in the session directory.

        Args:
            session_id: Target session.
            entries: List of :class:`MemoryEntry` objects to persist.
        """
        now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        front_matter: dict = {
            "session_id": session_id,
            "entry_count": len(entries),
            "updated_at": now,
        }
        text = serialize_memory(front_matter, entries)
        self._atomic_write(self._memory_path(session_id), text)

    def load_memory(self, session_id: str) -> list[MemoryEntry]:
        """Load structured memory entries from ``memory.md``.

        Returns an empty list when the file does not exist or is malformed.
        """
        memory_path = self._memory_path(session_id)
        if not os.path.isfile(memory_path):
            return []
        try:
            with open(memory_path, "r", encoding="utf-8") as fh:
                text = fh.read()
            _, body = parse_front_matter(text)
            entries_data = json.loads(body)
            if not isinstance(entries_data, list):
                return []
            entries: list[MemoryEntry] = []
            for item in entries_data:
                entries.append(
                    MemoryEntry(
                        entry_type=item["entry_type"],
                        content=item["content"],
                        source_turn_index=int(item["source_turn_index"]),
                        confidence=float(item["confidence"]),
                        created_at=item["created_at"],
                    )
                )
            return entries
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------------
    # Unified context compression (single LLM call)
    # ------------------------------------------------------------------

    def compress_context(
        self,
        session_id: str,
        turns: list[ConversationTurn],
        last_total_tokens: Optional[int] = None,
    ) -> None:
        """Compress conversation history in a single LLM call.

        Produces both a rolling summary and structured memory entries from the
        turns that fall outside the recent-K window.  Results are persisted to
        ``summary.md`` and ``memory.md`` respectively.

        Triggered when ALL of the following are true:

        - ``SUMMARY_MODEL_ID`` is configured (non-empty).
        - ``last_total_tokens`` exceeds the effective ``MAX_TOKENS_IN_CONTEXT``
          threshold (env var, default 65536).

        Both configuration values are re-read from the environment on every
        call so changes take effect without a restart.

        If the LLM call fails entirely, a warning is logged and both files are
        left unchanged.  If only the memory JSON is malformed, the summary is
        still saved and a warning is logged for the memory part.

        Args:
            session_id: Target session.
            turns: Current full list of conversation turns.
            last_total_tokens: Total tokens (prompt + completion) from the most
                recent inference.  When ``None``, the value is read from the
                ``last_total_tokens`` field in ``conversation.md``.
        """
        if not self._summary_model_id:
            return

        # Resolve last_total_tokens
        effective_tokens = last_total_tokens
        if effective_tokens is None:
            effective_tokens = self.get_last_total_tokens(session_id)
        if effective_tokens is None:
            return  # no token data available yet
        if effective_tokens <= self._max_tokens_in_context:
            return  # still within budget

        # Determine which turns to compress.
        #
        # Normal case (turns > K): keep the most recent K turns verbatim;
        # compress everything before them.
        #
        # Dense case (turns <= K but token budget already exceeded): every turn
        # is large — compress all turns except the very last user message so
        # the model still has the immediate request in context.
        k = self._recent_turns_k
        if len(turns) > k:
            summarized_up_to = len(turns) - k - 1
        else:
            # All turns fit within the K window but are too large together.
            # Compress everything up to (but not including) the last turn so
            # the final user message is always preserved verbatim.
            summarized_up_to = len(turns) - 2  # -1 keeps last turn; -2 is its predecessor

        # Nothing to compress (only 1 turn or empty)
        if summarized_up_to < 0:
            return

        # Read existing summary for version tracking and incremental update
        existing_summary, existing_fm = self.get_summary(session_id)
        existing_version: int = existing_fm.get("summary_version", 0)  # type: ignore[assignment]
        if not isinstance(existing_version, int):
            existing_version = 0

        # Build the turns text for the turns being compressed
        turns_text = "\n".join(
            f"[Turn {i}] {t.role}: {t.content}"
            for i, t in enumerate(turns[: summarized_up_to + 1])
        )

        # Compose prompt — two clearly separated tasks with strict output format
        if existing_summary:
            history_section = (
                f"## Previous summary\n{existing_summary}\n\n"
                f"## New turns to incorporate\n{turns_text}"
            )
        else:
            history_section = f"## Conversation turns\n{turns_text}"

        now_iso = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        prompt = f"""\
You are a conversation analysis assistant. Read the conversation history below \
and complete TWO tasks. Output ONLY the two tagged blocks — no other text.

{history_section}

---

**Task 1 — Rolling Summary**
Write a concise prose summary that preserves:
- User intents and goals
- Decisions made
- Key tool call results
- Unresolved questions or pending actions

If a previous summary is provided, produce an updated summary that incorporates \
the new turns into it (do not just append — rewrite as a coherent whole).

**Task 2 — Structured Memory**
Extract facts, preferences, decisions, and named entities worth remembering \
long-term. For each entry assign a confidence score (0.0–1.0) reflecting how \
clearly it was stated. Only include entries with meaningful information; omit \
trivial or uncertain items.

**Output format (strictly follow — no extra text outside the tags):**
<summary>
(summary prose here)
</summary>
<memory>
[
  {{
    "entry_type": "fact|preference|decision|entity",
    "content": "concise description",
    "source_turn_index": 0,
    "confidence": 0.9,
    "created_at": "{now_iso}"
  }}
]
</memory>
"""

        try:
            from runtime.models import InferenceRequest, Message as _Message  # local import to avoid circular deps
            infer_request = InferenceRequest(
                model_id=self._summary_model_id,
                messages=[_Message(role="user", content=prompt)],
            )
            result = self._infer_fn(infer_request)
            raw_output: str
            # InferenceResult: extract content from the last non-usage assistant message
            if hasattr(result, "messages") and result.messages:
                last_msg = next(
                    (m for m in reversed(result.messages) if getattr(m, "role", None) not in ("usage",)),
                    None,
                )
                raw_output = (getattr(last_msg, "content", None) or "") if last_msg else ""
            elif hasattr(result, "content"):
                raw_output = result.content or ""
            elif isinstance(result, dict) and "content" in result:
                raw_output = result["content"] or ""
            else:
                raw_output = str(result)
        except Exception as exc:  # noqa: BLE001
            logging.warning(
                "compress_context: LLM call failed for session %s: %s",
                session_id,
                exc,
            )
            return  # leave both files unchanged

        # --- Parse summary block ---
        summary_text = _extract_tagged_block(raw_output, "summary")
        if not summary_text:
            # Fallback: treat the entire output as the summary if tags are missing
            summary_text = raw_output.strip()
            logging.warning(
                "compress_context: <summary> tag not found in LLM output for "
                "session %s; using full output as summary",
                session_id,
            )

        # Persist summary
        now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        summary_fm: dict = {
            "session_id": session_id,
            "summary_version": existing_version + 1,
            "summarized_up_to_turn": summarized_up_to,
            "updated_at": now,
        }
        self._atomic_write(
            self._summary_path(session_id),
            serialize_summary(summary_fm, summary_text),
        )

        # --- Parse memory block ---
        memory_json_str = _extract_tagged_block(raw_output, "memory")
        if memory_json_str:
            try:
                entries_data = json.loads(memory_json_str)
                if not isinstance(entries_data, list):
                    raise ValueError("Expected a JSON array inside <memory>")
                entries: list[MemoryEntry] = []
                for item in entries_data:
                    entry = MemoryEntry(
                        entry_type=item["entry_type"],
                        content=item["content"],
                        source_turn_index=int(item["source_turn_index"]),
                        confidence=float(item["confidence"]),
                        created_at=item.get("created_at", now),
                    )
                    if entry.confidence >= self._memory_confidence_threshold:
                        entries.append(entry)
                # Persist to memory.md and update in-memory cache
                self.save_memory(session_id, entries)
                self._memory_store[session_id] = entries
            except Exception as exc:  # noqa: BLE001
                logging.warning(
                    "compress_context: failed to parse <memory> block for "
                    "session %s: %s",
                    session_id,
                    exc,
                )
        else:
            logging.warning(
                "compress_context: <memory> tag not found in LLM output for "
                "session %s; memory.md not updated",
                session_id,
            )

    def get_summary(self, session_id: str) -> tuple[str, dict]:
        """Return ``(summary_text, front_matter_dict)`` for *session_id*.

        Returns ``("", {})`` when ``summary.md`` does not exist.
        """
        summary_path = self._summary_path(session_id)
        if not os.path.isfile(summary_path):
            return ("", {})
        with open(summary_path, "r", encoding="utf-8") as fh:
            text = fh.read()
        fm, body = parse_front_matter(text)
        return (body, fm)

    # ------------------------------------------------------------------
    # Structured memory (public API — kept for backward compatibility)
    # ------------------------------------------------------------------

    def extract_memory(
        self,
        session_id: str,
        turns: list[ConversationTurn],
        last_total_tokens: Optional[int] = None,
    ) -> None:
        """Trigger context compression for *session_id* if the token threshold is exceeded.

        Delegates to :meth:`compress_context`.  Kept for backward compatibility.
        """
        self.compress_context(session_id, turns, last_total_tokens=last_total_tokens)

    def get_memory_entries(
        self,
        session_id: str,
        entry_type: Optional[str] = None,
    ) -> list[MemoryEntry]:
        """Return structured memory entries for *session_id*.

        Reads from ``memory.md`` on disk when available; falls back to the
        in-memory cache (``_memory_store``) for entries written in the current
        process but not yet flushed, or when the file does not exist.

        Args:
            session_id: Target session.
            entry_type: When provided, only entries whose ``entry_type``
                equals this value are returned.

        Returns:
            List of :class:`MemoryEntry` objects.
        """
        # Prefer persisted file; fall back to in-memory cache
        entries = self.load_memory(session_id)
        if not entries:
            entries = self._memory_store.get(session_id, [])
        if entry_type is not None:
            entries = [e for e in entries if e.entry_type == entry_type]
        return entries

    # ------------------------------------------------------------------
    # Artifact storage
    # ------------------------------------------------------------------

    def store_artifact(self, session_id: str, filename: str, data: bytes) -> str:
        """Write *data* as ``artifact-{filename}`` in the session directory.

        Args:
            session_id: Target session.
            filename: Original filename (will be prefixed with ``artifact-``).
            data: Raw bytes to write.

        Returns:
            Absolute path to the stored artifact file.
        """
        artifact_name = f"artifact-{filename}"
        file_path = os.path.join(self._chats_dir, session_id, artifact_name)

        # Atomic binary write
        dir_path = os.path.dirname(file_path)
        fd, tmp_path = tempfile.mkstemp(dir=dir_path)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp_path, file_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        # Update references in conversation.md
        self._add_reference(session_id, artifact_name)

        return file_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _atomic_write(path: str, text: str) -> None:
        """Write *text* to *path* atomically (temp file + os.replace)."""
        dir_path = os.path.dirname(path)
        os.makedirs(dir_path, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=dir_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _add_reference(self, session_id: str, ref: str) -> None:
        """No-op — references were part of the old Markdown format.

        Kept for API compatibility only.
        """

    # ------------------------------------------------------------------
    # Context assembly
    # ------------------------------------------------------------------

    def assemble_context(
        self,
        session_id: str,
        new_messages: list[dict],
        token_budget: Optional[int] = None,
    ) -> list[dict]:
        """Assemble the context window for *session_id*.

        Assembly strategy depends on whether the token threshold has been
        exceeded (i.e. whether ``summary.md`` exists for this session):

        **No summary (within token budget):**
        1. All conversation turns (full history, no truncation)
        2. *new_messages* appended as-is

        **Summary exists (token threshold was exceeded at some point):**
        1. Rolling summary → ``{"role": "system", "content": "## Summary\\n{text}"}``
        2. Structured memory entries → one ``{"role": "system", ...}`` per entry
        3. Most recent ``min(K, len(turns))`` conversation turns
        4. *new_messages* appended as-is

        The ``recent_turns_k`` parameter only controls how many turns are kept
        verbatim when compression is active.  It has no effect when the
        conversation is still within the token budget.

        When *token_budget* is provided and > 0, the assembled list is
        trimmed to fit within the budget:
        - Structured memory entries are removed oldest-first.
        - If still over budget after removing all memory, the summary message
          is removed.

        Args:
            session_id: Target session.  When empty or the session does not
                exist, *new_messages* is returned as-is.
            new_messages: New messages to append at the end.
            token_budget: Optional maximum token count.  ``<= 0`` means no
                limit (treated as ``None``).

        Returns:
            List of message dicts compatible with the Runtime.infer interface.
        """
        # Guard: empty session_id or non-existent session
        if not session_id or not self.session_exists(session_id):
            return list(new_messages)

        # Normalise token_budget
        effective_budget: Optional[int] = None
        if token_budget is not None and token_budget > 0:
            effective_budget = token_budget

        # Load conversation turns
        try:
            turns = self.load_conversation(session_id)
        except (FileNotFoundError, ValueError):
            turns = []

        # 1. Rolling summary (present only when compression has been triggered)
        summary_text, summary_fm = self.get_summary(session_id)
        summary_msg: Optional[dict] = None
        if summary_text.strip():
            summary_msg = {
                "role": "system",
                "content": f"## Summary\n{summary_text}",
            }

        if summary_msg is None:
            # No compression has occurred yet — inject full history verbatim.
            turn_msgs: list[dict] = [
                {k: v for k, v in asdict(t).items() if v is not None}
                for t in turns
            ]
            assembled = turn_msgs + list(new_messages)

            # Apply token budget if set (trim memory-less assembled list)
            if effective_budget is not None:
                total_tokens = sum(estimate_tokens(str(msg)) for msg in assembled)
                # Nothing to trim here beyond dropping oldest turns, but that
                # would be lossy without a summary — leave as-is and let the
                # caller handle overflow.
            return assembled

        # Compression is active — use summary + recent-K window.

        # 2. Structured memory entries
        memory_entries = self.get_memory_entries(session_id)
        memory_msgs: list[dict] = [
            {
                "role": "system",
                "content": f"## Memory\n{entry.entry_type}: {entry.content}",
            }
            for entry in memory_entries
        ]

        # 3. Recent K turns
        k = self._recent_turns_k
        recent_turns = turns[-k:] if len(turns) > k else turns
        turn_msgs = [
            {k: v for k, v in asdict(t).items() if v is not None}
            for t in recent_turns
        ]

        # Assemble full list
        assembled = []
        assembled.append(summary_msg)
        assembled.extend(memory_msgs)
        assembled.extend(turn_msgs)
        assembled.extend(new_messages)

        # Apply token budget if set
        if effective_budget is not None:
            total_tokens = sum(estimate_tokens(str(msg)) for msg in assembled)

            if total_tokens > effective_budget:
                while memory_msgs and total_tokens > effective_budget:
                    removed = memory_msgs.pop(0)
                    total_tokens -= estimate_tokens(str(removed))

                # Rebuild assembled after memory truncation
                assembled = [summary_msg]
                assembled.extend(memory_msgs)
                assembled.extend(turn_msgs)
                assembled.extend(new_messages)

                total_tokens = sum(estimate_tokens(str(msg)) for msg in assembled)

            # If still over budget, remove the summary
            if total_tokens > effective_budget:
                assembled = [m for m in assembled if m is not summary_msg]

        return assembled

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def introspect(self, session_id: str) -> "IntrospectionSnapshot":
        """Return an observability snapshot for *session_id*.

        Computed from live state — never cached.

        The following invariants always hold in the returned snapshot:
        - ``total_turns == summarized_turns + recent_window_size``
        - ``memory_entry_count == sum(memory_entries_by_type.values())``

        Args:
            session_id: Target session.

        Returns:
            An :class:`IntrospectionSnapshot` instance.
        """
        # total_turns
        try:
            turns = self.load_conversation(session_id)
            total_turns = len(turns)
        except (FileNotFoundError, ValueError):
            total_turns = 0

        # summarized_turns and summary_version
        _, summary_fm = self.get_summary(session_id)
        if summary_fm:
            raw_summarized = summary_fm.get("summarized_up_to_turn", -1)
            summarized_turns = int(raw_summarized) + 1 if isinstance(raw_summarized, int) else 0
            summary_version = int(summary_fm.get("summary_version", 0))
        else:
            summarized_turns = 0
            summary_version = 0

        # recent_window_size: clamped to [0, K]
        k = self._recent_turns_k
        raw_window = total_turns - summarized_turns
        recent_window_size = max(0, min(raw_window, k))

        # Enforce invariant: total_turns == summarized_turns + recent_window_size
        # Adjust summarized_turns if needed (e.g. when total_turns < summarized_turns)
        summarized_turns = total_turns - recent_window_size

        # Memory entries
        all_entries = self.get_memory_entries(session_id)
        memory_entry_count = len(all_entries)
        memory_entries_by_type: dict[str, int] = {}
        for entry in all_entries:
            memory_entries_by_type[entry.entry_type] = (
                memory_entries_by_type.get(entry.entry_type, 0) + 1
            )

        # Estimated context tokens
        assembled = self.assemble_context(session_id, [])
        estimated_context_tokens = sum(
            estimate_tokens(str(msg)) for msg in assembled
        )

        return IntrospectionSnapshot(
            session_id=session_id,
            total_turns=total_turns,
            summarized_turns=summarized_turns,
            recent_window_size=recent_window_size,
            memory_entry_count=memory_entry_count,
            memory_entries_by_type=memory_entries_by_type,
            summary_version=summary_version,
            estimated_context_tokens=estimated_context_tokens,
            token_budget=None,
        )
