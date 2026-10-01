"""Context Manager for Agent Service.

Manages multi-turn conversation context, session persistence, rolling summaries,
structured memory extraction, and context assembly. Uses only the Python standard
library — zero third-party dependencies.
"""

from __future__ import annotations

import difflib
import gzip
import logging
import re
import stat
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class JournalConflictError(RuntimeError):
    def __init__(self, message: str, files: list[str]):
        super().__init__(message)
        self.files = files

    def to_dict(self) -> dict:
        return {
            "error": "JournalConflict",
            "message": str(self),
            "files": self.files,
            "can_force": True,
        }


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
        name: Optional tool/function name (used for tool-role turns), or agent nickname for assistant-role turns.
        tool_calls: Optional list of tool call dicts for parallel tool calls.
        thinking: Optional thinking/reasoning content from the model.
        stat: Optional dict of token and timing statistics.
        images: Optional list of base64-encoded image strings (multimodal).
        audio: Optional base64-encoded audio string (multimodal).
        prompt_template: Optional prompt template ID for this turn.
        arguments: Optional dict of template arguments for this turn.
        agent_id: Optional agent_id for all roles, identifying which agent produced this turn
            (set on assistant and tool turns alike, so the frontend can group by agent).
        tool_id: Optional tool_id for tool-role turns, identifying which tool produced this result.
        tool_use_id: Optional protocol-level tool call ID linking a tool result to its assistant tool call.
        completed_at: Kept for backward compatibility with old sessions.
            For new sessions, msg.timestamp is the inference completion time,
            and stat.first_token_timestamp is the first token time.
        started_at: Optional ISO 8601 timestamp indicating when the underlying
            operation actually started. For assistant turns this is the real
            model-request send time; for tool turns it is the callable/MCP
            execution start time.
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
    agent_id: Optional[str] = None
    tool_id: Optional[str] = None
    tool_use_id: Optional[str] = None
    completed_at: Optional[str] = None
    mentions: Optional[list[str]] = None
    started_at: Optional[str] = None


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
# Token estimation & shared utilities (re-exported from common)
# ---------------------------------------------------------------------------

from runtime.common import estimate_tokens  # noqa: F401 — re-export


# ---------------------------------------------------------------------------
# Lightweight YAML front-matter parser (delegated to common)
# ---------------------------------------------------------------------------

from runtime.common import (  # noqa: F401 — re-export for backward compat
    _parse_yaml_value,
    _parse_yaml_block,
    parse_front_matter,
    serialize_yaml_value as _serialize_yaml_value,
    build_front_matter as _build_front_matter,
)


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
# Compression-prompt guards
# ---------------------------------------------------------------------------
#
# The compression prompt is built from the INCREMENTAL delta: only the turns
# NOT yet covered by the previous (valid) summary are sent to the LLM.  In
# normal operation that delta is inherently bounded, because compression is
# triggered as soon as the inference context exceeds ``MAX_TOKENS_IN_CONTEXT``
# (env var, default 65536) — so between two compressions at most about one
# "trigger-threshold worth" of new turns can accumulate.  The delta is
# therefore sent IN FULL; no artificial prompt budget is applied.
#
# Guards:
#
#   - a single pathological turn (e.g. a multi-MB tool output) is still capped
#     at ``_SUMMARY_TURN_MAX_CHARS`` chars (head 3/4 + tail 1/4); this is a
#     per-turn content-quality limit, not a total budget;
#   - if the summary model rejects the request with a context-overflow error
#     (the window is not known client-side, and the delta can grow unbounded
#     while compression keeps failing), the prompt is rebuilt with a token
#     budget halved from the natural prompt size (down to
#     ``_SUMMARY_PROMPT_MIN_TOKENS``), dropping the OLDEST delta turns first
#     (a marker documents the loss);
#   - an ACCEPTED request that returns an EMPTY answer is also retried:
#     when completion tokens were used (``stat.completion_tokens > 0``) a
#     thinking model spent the remaining completion budget on reasoning and
#     never reached the answer (the prompt nearly filled the provider's
#     TOTAL token window) - the prompt is shrunk like an overflow retry so
#     the model has room to both think and answer; when no completion tokens
#     were used at all the same prompt is resent a few times;
#   - repeated failures enter a backoff window so over-budget sessions do not
#     hammer the summary model with the same doomed request on every turn.

_SUMMARY_PROMPT_MIN_TOKENS: int = 8192
_SUMMARY_TURN_MAX_CHARS: int = 20000
#: Cap for the "previous summary" section when no retry budget is active; a
#: rolling summary should be compact (quality limit, not a window constraint).
_PREV_SUMMARY_MAX_TOKENS: int = 16384
#: Maximum number of overflow retries (each one halves the prompt budget).
_MAX_OVERFLOW_RETRIES: int = 12
#: Maximum number of same-prompt retries after an empty answer that used no
#: completion tokens at all (transient provider behavior).
_MAX_EMPTY_OUTPUT_RETRIES: int = 2
_MEMORY_MAX_ENTRIES: int = 200
_COMPRESS_BACKOFF_TOKEN_GROWTH: float = 1.25
_COMPRESS_BACKOFF_SECONDS: float = 1800.0
_COMPRESS_BACKOFF_MIN_FAILURES: int = 3

#: Fragments that reveal a summary is the compression prompt's own output-format
#: example (persisted when a failed LLM call's input prompt was mistakenly
#: treated as the model output).  Such files must never be injected into
#: inference context nor trusted as "previous summary" for incremental merges.
_PLACEHOLDER_SUMMARY_MARKERS: tuple[str, ...] = ("(concise summary prose",)
_PLACEHOLDER_MEMORY_CONTENTS: frozenset[str] = frozenset(
    {"self-contained descriptive sentence"}
)
_VALID_ENTRY_TYPES: frozenset[str] = frozenset(
    {"fact", "preference", "decision", "entity"}
)
_OVERFLOW_ERROR_KEYWORDS: tuple[str, ...] = (
    "exceed",
    "context size",
    "context length",
    "context_length",
    "too many tokens",
    "maximum context",
    "prompt is too long",
    "range of input",
    "max_tokens",
)

# Fast CJK-aware token estimate.  ``estimate_llm_tokens`` in runtime.common has
# the same semantics but walks the text character by character in Python, which
# is far too slow for multi-megabyte histories; the regex-based count below
# runs in C and matches it.
_CJK_TOKEN_CHAR_RE = re.compile(
    "[\u2E80-\u30FF\u31F0-\u31FF\u3400-\u4DBF\u4E00-\u9FFF"
    "\uAC00-\uD7AF\uF900-\uFAFF\U00020000-\U000323AF]"
)


def _estimate_tokens_fast(text: str) -> int:
    """Fast CJK-aware token estimate (same semantics as ``estimate_llm_tokens``)."""
    if not text:
        return 0
    cjk = len(_CJK_TOKEN_CHAR_RE.findall(text))
    other = len(text) - cjk
    return cjk + ((other + 3) // 4 if other else 0)


def _is_placeholder_summary(text: str) -> bool:
    """Return True when *text* is (part of) the prompt's format example.

    A placeholder summary is a corrupted artifact: the model never produced
    it, so it must be treated as "no summary exists".
    """
    return any(marker in text for marker in _PLACEHOLDER_SUMMARY_MARKERS)


def _is_placeholder_memory_entry(content: str) -> bool:
    """Return True when a memory entry is the prompt's format example."""
    stripped = (content or "").strip()
    return not stripped or stripped in _PLACEHOLDER_MEMORY_CONTENTS


def _is_overflow_error(error: str) -> bool:
    """Return True when *error* looks like a context-window overflow rejection."""
    lowered = (error or "").lower()
    return any(keyword in lowered for keyword in _OVERFLOW_ERROR_KEYWORDS)


def _truncate_turn_content(content: str, max_chars: int = _SUMMARY_TURN_MAX_CHARS) -> str:
    """Cap a single turn's content for the compression prompt.

    Keeps the head (3/4) and the tail (1/4); a marker documents the omission.
    """
    if max_chars <= 0 or len(content) <= max_chars:
        return content
    head = max_chars * 3 // 4
    tail = max_chars // 4
    omitted = len(content) - head - tail
    return f"{content[:head]}\n[... {omitted} characters omitted ...]\n{content[len(content) - tail:]}"


def _truncate_to_token_budget(text: str, token_budget: int) -> str:
    """Truncate *text* (head/tail) so its token estimate fits *token_budget*."""
    if token_budget <= 0:
        return ""
    est = _estimate_tokens_fast(text)
    if est <= token_budget:
        return text
    ratio = len(text) / est  # chars per estimated token
    chars = max(1, int(token_budget * ratio))
    if chars >= len(text):
        return text
    head = chars * 3 // 4
    tail = chars // 4
    omitted = len(text) - head - tail
    return f"{text[:head]}\n[... {omitted} characters omitted ...]\n{text[len(text) - tail:]}"


_COMPRESSION_PROMPT_HEAD = (
    "You are a conversation analysis assistant. Read the conversation history "
    "below and complete TWO tasks. Output ONLY the two tagged blocks \u2014 no other text.\n\n"
)

_COMPRESSION_PROMPT_TAIL = """
---

**Task 1 \u2014 Rolling Summary (conversation FRAMEWORK)**

Focus on what determines the DIRECTION of the conversation: the user's \
instructions and the assistant's final responses. This summary will replace the \
original conversation in the model's context window, so it must preserve the \
narrative arc \u2014 what was asked, what was decided, and where things stand now.

Prioritise these (they define the framework):
- **User instructions & goals**: every user message that states a request, \
  goal, question, or feedback. Reproduce the full intent \u2014 do not reduce a \
  detailed request to a one-liner. Pay special attention to the LAST user \
  message, as it typically sets the current task.
- **Assistant final responses** (marked [ASSISTANT \u2014 FINAL RESPONSE]): these \
  are the assistant's synthesised answers, decisions, and deliverables. \
  Capture the conclusions reached, the approach chosen, the solution delivered, \
  and the reasoning behind key choices.
- **Key decisions & rationale**: what was decided, by whom, and why. Include \
  trade-offs discussed (e.g. "chose X over Y because Z").
- **Unresolved items**: questions still open, tasks pending, explicit next \
  steps the user or assistant committed to.

Do NOT duplicate raw factual data into the summary. The following belong in \
Task 2 (Memory), not here:
- Tool call arguments and raw tool outputs (file listings, search result \
  snippets, stack traces, command output).
- Specific file paths, version numbers, port numbers, configuration values \
  (unless essential to understanding a decision).
- Intermediate error messages and their step-by-step resolution.

If a previous summary is provided, merge it with the new turns into a single \
coherent narrative. Do NOT just append \u2014 rewrite from scratch as one integrated \
summary. Keep the most recent summary's information when still relevant; drop \
only what has been superseded.

**Task 2 \u2014 Structured Memory (factual DETAILS)**

Extract SPECIFIC, FACTUAL details from the conversation \u2014 especially from \
tool-call loops, tool results, and intermediate reasoning \u2014 that are useful \
reference material for future sessions. These are the raw facts deliberately \
left out of the summary. Be thorough \u2014 err on the side of including borderline \
items. Assign a confidence score (0.0\u20131.0).

Categories (use in entry_type):
- "fact": objective, verifiable information (e.g. "the server runs on port \
  8080", "database name is app_production", "error message was 'connection \
  refused on 127.0.0.1:5432'", "file src/auth.py is 342 lines")
- "preference": user likes/dislikes (e.g. "prefers async/await over raw \
  promises", "dislikes ORMs, prefers raw SQL")
- "decision": a choice made with rationale (e.g. "decided to use Redis for \
  caching because latency must be < 5ms")
- "entity": a named thing the user cares about (e.g. "Working on project \
  'AcmeChat'", "Uses AWS S3 bucket 'uploads-prod'")

Pay extra attention to:
- **Tool results**: file paths discovered, search results, command output, \
  stack traces, error messages, test failure details.
- **Code & configuration**: specific code snippets, shell commands, SQL \
  queries, config values, environment variables mentioned or discovered.
- **Version / environment info**: language versions, library versions, OS \
  details, hardware specs mentioned.

For each entry:
- Write a self-contained sentence that makes sense without surrounding context.
- Include specific names, versions, numbers when available.
- Skip truly trivial chit-chat ("hello", "thanks") but include anything that \
  might be useful to recall in a future session.

**Output format (strictly follow \u2014 no extra text outside the tags):**
<summary>
(concise summary prose, typically 2\u20135 paragraphs)
</summary>
<memory>
[
  {
    "entry_type": "fact|preference|decision|entity",
    "content": "self-contained descriptive sentence",
    "source_turn_index": 0,
    "confidence": 0.9,
    "created_at": "{current_ts}"
  }
]
</memory>
"""


def _turn_compression_label(turn: "ConversationTurn") -> str:
    """Annotate a turn's type for the compression prompt."""
    if turn.role == "user":
        return "[USER INSTRUCTION]"
    if turn.role == "assistant":
        if turn.tool_calls:
            return "[ASSISTANT \u2014 TOOL CALLS (intermediate)]"
        return "[ASSISTANT \u2014 FINAL RESPONSE]"
    if turn.role == "tool":
        tool_name = f" ({turn.name})" if turn.name else ""
        return f"[TOOL RESULT{tool_name}]"
    if turn.role == "system":
        return "[SYSTEM]"
    return f"[{turn.role}]"


# ---------------------------------------------------------------------------
# ContextManager
# ---------------------------------------------------------------------------

import datetime
import json
import logging
import os
import subprocess
import tempfile
import time

from runtime.common import now_iso
from typing import Callable, Optional

from runtime.common import (
    get_workspace,
    parse_iso_timestamp as _journal_parse_ts,
    utc_now_iso as _journal_now_iso,
    sha256_bytes as _journal_sha256,
    safe_rel_path as _journal_safe_rel_path,
    atomic_write_json as _journal_atomic_write_json,
    atomic_write_text,
    session_timestamp,
)


def _journal_normalize_label(raw: object) -> str:
    """Normalise a manifest path label.

    In addition to workspace-relative labels, absolute labels for the
    permitted ``/tmp`` scratch area are accepted (same rule as
    ``builtin_tools_coding._validate_path``).
    """
    label = str(raw).replace("\\", "/")
    if os.path.isabs(label):
        root = os.path.realpath("/tmp")
        resolved = os.path.realpath(label)
        if not (resolved == root or resolved.startswith(root + os.sep)):
            raise ValueError(f"Unsafe journal path: {label}")
        return label
    return _journal_safe_rel_path(label)


def _journal_resolve_workspace_path(workspace: str, rel_path: str) -> str:
    label = _journal_normalize_label(rel_path)
    if os.path.isabs(label):
        # Absolute labels point at the /tmp scratch area, not the workspace.
        return os.path.realpath(label)
    root = os.path.realpath(workspace)
    resolved = os.path.realpath(os.path.join(root, label))
    if not (resolved == root or resolved.startswith(root + os.sep)):
        raise ValueError(f"Journal path escapes workspace: {label}")
    return resolved


def _read_journal_sidecar(journal_dir: str, blob_ref: dict) -> bytes:
    rel_file = _journal_safe_rel_path(blob_ref.get("file", ""))
    blob_path = os.path.realpath(os.path.join(journal_dir, rel_file))
    root = os.path.realpath(journal_dir)
    if not (blob_path == root or blob_path.startswith(root + os.sep)):
        raise ValueError(f"Journal blob escapes journal directory: {rel_file}")
    with open(blob_path, "rb") as fh:
        raw = gzip.decompress(fh.read())
    expected_sha = blob_ref.get("sha256")
    if expected_sha and _journal_sha256(raw) != expected_sha:
        raise ValueError("Journal sidecar sha256 mismatch")
    return raw


def _read_journal_git_blob(blob_ref: dict, workspace: str) -> bytes:
    oid = blob_ref.get("oid")
    if not oid:
        raise ValueError("Git journal blob is missing oid")
    result = subprocess.run(
        ["git", "cat-file", "-p", oid],
        cwd=workspace,
        capture_output=True,
        text=False,
    )
    if result.returncode != 0:
        raise ValueError((result.stderr or b"").decode("utf-8", errors="replace") or "Could not read git blob")
    raw = result.stdout or b""
    expected_sha = blob_ref.get("sha256")
    if expected_sha and _journal_sha256(raw) != expected_sha:
        raise ValueError("Git journal blob sha256 mismatch")
    return raw


def _read_journal_blob(blob_ref: dict, workspace: str, journal_dir: str) -> bytes:
    store = blob_ref.get("store")
    if store == "sidecar":
        return _read_journal_sidecar(journal_dir, blob_ref)
    if store == "git":
        return _read_journal_git_blob(blob_ref, workspace)
    raise ValueError(f"Unsupported journal blob store: {store}")


def materialize_blob(blob_ref: dict, target_path: str, workspace: str, journal_dir: str) -> None:
    parent = os.path.dirname(target_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        os.unlink(target_path)
    except FileNotFoundError:
        pass
    if not blob_ref.get("exists"):
        return
    raw = _read_journal_blob(blob_ref, workspace, journal_dir)
    if blob_ref.get("is_symlink"):
        os.symlink(raw.decode("utf-8", errors="surrogateescape"), target_path)
        uid, gid = blob_ref.get("uid"), blob_ref.get("gid")
        if uid is not None and gid is not None:
            try:
                os.lchown(target_path, uid, gid)
            except (OSError, AttributeError):
                pass
        return
    fd, tmp_path = tempfile.mkstemp(dir=parent or None)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        os.replace(tmp_path, target_path)
        tmp_path = ""
        file_mode = blob_ref.get("file_mode")
        if file_mode is None:
            file_mode = 0o755 if blob_ref.get("mode") == "100755" else 0o644
        os.chmod(target_path, file_mode)
        uid, gid = blob_ref.get("uid"), blob_ref.get("gid")
        if uid is not None and gid is not None:
            try:
                os.chown(target_path, uid, gid)
            except (OSError, AttributeError):
                pass
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


def _current_file_matches(path: str, expected: dict) -> bool:
    if not expected.get("exists"):
        return not os.path.exists(path) and not os.path.islink(path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if bool(expected.get("is_symlink")) != stat.S_ISLNK(st.st_mode):
        return False
    if expected.get("is_symlink"):
        try:
            raw = os.readlink(path).encode("utf-8", errors="surrogateescape")
        except OSError:
            return False
    else:
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            return False
    expected_sha = expected.get("sha256")
    return not expected_sha or _journal_sha256(raw) == expected_sha


def _restore_journal_baseline(path: str, baseline: dict, workspace: str, journal_dir: str) -> None:
    if not baseline.get("exists"):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    materialize_blob(baseline, path, workspace, journal_dir)


def _journal_manifest_sort_key(item: tuple[str, dict]) -> datetime.datetime:
    manifest = item[1]
    parsed = _journal_parse_ts(manifest.get("timestamp"))
    if parsed is not None:
        return parsed
    turn_key = str(manifest.get("turn_key", ""))
    try:
        return datetime.datetime.strptime(turn_key, "%y%m%d_%H%M%S")
    except ValueError:
        return datetime.datetime.min


def _iter_journal_manifests(session_dir: str) -> list[tuple[str, dict]]:
    root = Path(session_dir)
    if not root.exists():
        return []
    manifests: list[tuple[str, dict]] = []
    for path in root.glob("**/file_journals/*/manifest.json"):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
            manifests.append((str(path), manifest))
        except (OSError, ValueError):
            continue
    manifests.sort(key=_journal_manifest_sort_key)
    return manifests


def _manifest_belongs_to_session(manifest: dict, session_id: Optional[str]) -> bool:
    if not session_id:
        return True
    manifest_session = str(manifest.get("session_id") or "")
    return manifest_session == session_id or manifest_session.startswith(f"{session_id}-sub_")


def _manifest_has_file_changes(manifest: dict) -> bool:
    files = manifest.get("files")
    if not isinstance(files, dict):
        return False
    return any(isinstance(entry, dict) and "baseline" in entry and "after" in entry for entry in files.values())


def _restore_file_plan(workspace: str, restore_plan: dict[str, dict], force: bool = False) -> dict:
    conflicts: list[str] = []
    for rel_path, item in restore_plan.items():
        target_path = _journal_resolve_workspace_path(workspace, rel_path)
        if not _current_file_matches(target_path, item["expected_current"]):
            conflicts.append(rel_path)
    if conflicts and not force:
        return {
            "error": "JournalConflict",
            "message": "Current files do not match journal after-state",
            "files": conflicts,
        }

    restored_files: list[str] = []
    for rel_path, item in restore_plan.items():
        target_path = _journal_resolve_workspace_path(workspace, rel_path)
        _restore_journal_baseline(target_path, item["baseline"], workspace, item["journal_dir"])
        restored_files.append(rel_path)
    return {"restored_files": restored_files}


def _move_journal_files_dir_to_undone(journal_dir: str) -> None:
    """Move a turn journal's sidecar blobs from files/ to undone_files/.

    The blobs are intentionally kept for inspection, but moving the directory
    makes it explicit that the corresponding workspace changes have already
    been reverted.
    """
    src_dir = os.path.join(journal_dir, "files")
    dst_dir = os.path.join(journal_dir, "undone_files")
    if not os.path.isdir(src_dir):
        return

    if not os.path.exists(dst_dir):
        os.replace(src_dir, dst_dir)
        return

    os.makedirs(dst_dir, exist_ok=True)
    for root, dirs, files in os.walk(src_dir, topdown=False):
        rel_root = os.path.relpath(root, src_dir)
        target_root = dst_dir if rel_root == "." else os.path.join(dst_dir, rel_root)
        os.makedirs(target_root, exist_ok=True)
        for name in files:
            os.replace(os.path.join(root, name), os.path.join(target_root, name))
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    try:
        os.rmdir(src_dir)
    except OSError:
        pass


def _rewrite_blob_refs_to_undone_files(value):
    if isinstance(value, dict):
        if value.get("store") == "sidecar" and isinstance(value.get("file"), str):
            file_ref = value["file"]
            if file_ref == "files" or file_ref.startswith("files/"):
                value["file"] = "undone_files" + file_ref[len("files"):]
        for child in value.values():
            _rewrite_blob_refs_to_undone_files(child)
    elif isinstance(value, list):
        for child in value:
            _rewrite_blob_refs_to_undone_files(child)


def _mark_manifest_file_changes_undone(manifest_path: str, manifest: dict) -> None:
    """Move active file-change records/blobs to undone markers.

    This is used after a journaled change is actually restored, whether via the
    explicit undo tool or via revoking a user message. keep_files revokes do not
    call this helper because no file restoration took place.
    """
    files = manifest.pop("files", None)
    if isinstance(files, dict):
        _rewrite_blob_refs_to_undone_files(files)
        manifest["undone_files"] = files
    _move_journal_files_dir_to_undone(os.path.dirname(manifest_path))


def undo_latest_file_journal_turn(
    workspace: str,
    session_dir: str,
    session_id: Optional[str] = None,
    force: bool = False,
) -> dict:
    manifests = [
        item for item in _iter_journal_manifests(session_dir)
        if _manifest_belongs_to_session(item[1], session_id)
        and not item[1].get("revoked")
        and not item[1].get("undone")
        and _manifest_has_file_changes(item[1])
    ]
    if not manifests:
        return {"error": "NoJournalToUndo", "message": "No file journal turn is available to undo"}

    manifest_path, manifest = manifests[-1]
    journal_dir = os.path.dirname(manifest_path)
    restore_plan: dict[str, dict] = {}
    for rel_path, entry in manifest.get("files", {}).items():
        if not isinstance(entry, dict) or "baseline" not in entry or "after" not in entry:
            continue
        safe_rel = _journal_normalize_label(entry.get("path") or rel_path)
        restore_plan[safe_rel] = {
            "baseline": entry["baseline"],
            "expected_current": entry["after"],
            "journal_dir": journal_dir,
        }
    if not restore_plan:
        return {"error": "NoJournalToUndo", "message": "No file changes were found in the latest journal turn"}

    result = _restore_file_plan(workspace, restore_plan, force=force)
    if result.get("error"):
        return result

    now = _journal_now_iso()
    manifest["undone"] = True
    manifest["undone_at"] = now
    _mark_manifest_file_changes_undone(manifest_path, manifest)
    manifest["updated_at"] = now
    _journal_atomic_write_json(manifest_path, manifest)
    return {
        "turn_key": manifest.get("turn_key"),
        "restored_files": result["restored_files"],
        "undone": True,
    }


def revoke_session_file_changes(
    workspace: str,
    session_dir: str,
    session_id: str,
    timestamp: str,
    force: bool = False,
    keep_files: bool = False,
) -> dict:
    threshold = _journal_parse_ts(timestamp)
    if threshold is None:
        return {"error": "InvalidTimestamp", "message": f"Invalid revoke timestamp: {timestamp}"}

    selected: list[tuple[str, dict]] = []
    for manifest_path, manifest in _iter_journal_manifests(session_dir):
        if manifest.get("revoked") or not _manifest_belongs_to_session(manifest, session_id):
            continue
        manifest_ts = _journal_parse_ts(manifest.get("timestamp"))
        if manifest_ts is None:
            continue
        if manifest_ts >= threshold and _manifest_has_file_changes(manifest):
            selected.append((manifest_path, manifest))

    if not selected:
        return {"skipped": True, "reason": "no_matching_file_journals", "restored_files": []}

    restore_plan: dict[str, dict] = {}
    for manifest_path, manifest in selected:
        journal_dir = os.path.dirname(manifest_path)
        for rel_path, entry in manifest.get("files", {}).items():
            if not isinstance(entry, dict) or "baseline" not in entry or "after" not in entry:
                continue
            safe_rel = _journal_normalize_label(entry.get("path") or rel_path)
            if safe_rel not in restore_plan:
                restore_plan[safe_rel] = {
                    "baseline": entry["baseline"],
                    "expected_current": entry["after"],
                    "journal_dir": journal_dir,
                }
            else:
                restore_plan[safe_rel]["expected_current"] = entry["after"]

    if not restore_plan:
        return {"skipped": True, "reason": "no_file_changes", "restored_files": []}

    if keep_files:
        now = _journal_now_iso()
        revoked_turns: list[str] = []
        for manifest_path, manifest in selected:
            manifest["revoked"] = True
            manifest["revoked_at"] = now
            manifest["kept_files"] = True
            manifest["updated_at"] = now
            _journal_atomic_write_json(manifest_path, manifest)
            revoked_turns.append(str(manifest.get("turn_key")))
        return {
            "revoked": True,
            "revoked_turns": revoked_turns,
            "restored_files": [],
            "kept_files": True,
        }

    result = _restore_file_plan(workspace, restore_plan, force=force)
    if result.get("error"):
        return result

    now = _journal_now_iso()
    revoked_turns: list[str] = []
    for manifest_path, manifest in selected:
        manifest["revoked"] = True
        manifest["revoked_at"] = now
        _mark_manifest_file_changes_undone(manifest_path, manifest)
        manifest["updated_at"] = now
        _journal_atomic_write_json(manifest_path, manifest)
        revoked_turns.append(str(manifest.get("turn_key")))

    return {
        "revoked": True,
        "revoked_turns": revoked_turns,
        "restored_files": result["restored_files"],
    }


def _diff_bytes(before: bytes, after: bytes, before_name: str, after_name: str) -> str:
    before_lines = before.decode("utf-8", errors="replace").splitlines(keepends=True)
    after_lines = after.decode("utf-8", errors="replace").splitlines(keepends=True)
    return "".join(difflib.unified_diff(before_lines, after_lines, fromfile=before_name, tofile=after_name))


def _journal_dirs(session_dir: str) -> list[str]:
    """Return ``file_journals`` directory paths for *session_dir* and its sub-sessions."""
    dirs: list[str] = []
    parent = os.path.join(session_dir, "file_journals")
    if os.path.isdir(parent):
        dirs.append(parent)
    try:
        for entry in os.listdir(session_dir):
            # ``delegate`` uses sub_* and ``talk_to`` uses talk_* child
            # conversation directories.  New code journals nested changes on
            # the parent turn, but scan both forms for backward compatibility.
            if (entry.startswith(("sub_", "talk_"))
                    and os.path.isdir(os.path.join(session_dir, entry))):
                sub = os.path.join(session_dir, entry, "file_journals")
                if os.path.isdir(sub):
                    dirs.append(sub)
    except OSError:
        pass
    return dirs


def diff_journal_turn(session_dir: str, turn_key: str) -> str:
    # Search parent and sub-session directories for the journal turn
    manifest_path = None
    for journals_dir in _journal_dirs(session_dir):
        candidate = os.path.join(journals_dir, turn_key, "manifest.json")
        if os.path.isfile(candidate):
            manifest_path = candidate
            break
    if manifest_path is None:
        # Legacy fallback
        manifest_path = os.path.join(session_dir, "file_journals", turn_key, "manifest.json")
    if not os.path.isfile(manifest_path):
        return ""
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    journal_dir = os.path.dirname(manifest_path)
    workspace = manifest.get("workspace") or os.getcwd()
    chunks: list[str] = []
    for rel_path, entry in manifest.get("files", {}).items():
        if not isinstance(entry, dict) or "baseline" not in entry or "after" not in entry:
            continue
        before = b"" if not entry["baseline"].get("exists") else _read_journal_blob(entry["baseline"], workspace, journal_dir)
        after = b"" if not entry["after"].get("exists") else _read_journal_blob(entry["after"], workspace, journal_dir)
        chunks.append(_diff_bytes(before, after, f"a/{rel_path}", f"b/{rel_path}"))
    return "".join(chunks)


def diff_journal_session(session_dir: str) -> str:
    chunks: list[str] = []
    for _manifest_path, manifest in _iter_journal_manifests(session_dir):
        turn_key = manifest.get("turn_key")
        if turn_key:
            chunks.append(diff_journal_turn(session_dir, str(turn_key)))
    return "".join(chunks)


def get_file_journals_list(session_dir: str) -> list[str]:
    """Return ISO timestamps of turns that have a file journal manifest.

    Only returns timestamps whose manifest is not revoked and contains at least
    one file entry with both baseline and after.

    Also scans legacy nested-session directories (``sub_*`` / ``talk_*``) so
    delegate and talk_to changes remain visible alongside the parent session.
    """
    journals_dirs = _journal_dirs(session_dir)
    if not journals_dirs:
        return []

    seen: set[str] = set()
    timestamps: list[str] = []
    for journals_dir in journals_dirs:
        for entry in sorted(os.listdir(journals_dir)):
            manifest_path = os.path.join(journals_dir, entry, "manifest.json")
            if not os.path.isfile(manifest_path):
                continue
            try:
                with open(manifest_path, "r", encoding="utf-8") as fh:
                    manifest = json.load(fh)
            except (OSError, ValueError):
                continue
            if manifest.get("revoked"):
                continue
            if not isinstance(manifest.get("files"), dict) or not manifest["files"]:
                continue
            has_changes = False
            for _rel, entry_data in manifest["files"].items():
                if isinstance(entry_data, dict) and "baseline" in entry_data and "after" in entry_data:
                    has_changes = True
                    break
            if not has_changes:
                continue
            # Use the ISO timestamp from the manifest (matches msg.timestamp).
            ts = manifest.get("timestamp")
            if ts and ts not in seen:
                seen.add(ts)
                timestamps.append(ts)
    return timestamps


def _find_journal_turn_dir(session_dir: str, timestamp: str) -> Optional[str]:
    """Find the journal turn directory whose manifest has the given timestamp.

    Also searches legacy nested-session directories (``sub_*`` / ``talk_*``)
    so delegate and talk_to diffs can be retrieved.
    """
    for journals_dir in _journal_dirs(session_dir):
        for entry in os.listdir(journals_dir):
            manifest_path = os.path.join(journals_dir, entry, "manifest.json")
            if not os.path.isfile(manifest_path):
                continue
            try:
                with open(manifest_path, "r", encoding="utf-8") as fh:
                    manifest = json.load(fh)
            except (OSError, ValueError):
                continue
            if manifest.get("timestamp") == timestamp:
                return os.path.join(journals_dir, entry)
    return None


def get_file_journal_diff(session_dir: str, timestamp: str) -> dict:
    """Return structured per-file diff data for a single file-journal turn.

    *timestamp* is the ISO-format timestamp that appears on the user message
    (matches ``manifest[\"timestamp\"]``).

    Returns:
        A dict with ``turn_key`` and ``files`` (list of per-file objects).
        Each file object contains: path, change_type, baseline, after, diff.
    """
    turn_dir = _find_journal_turn_dir(session_dir, timestamp)
    if turn_dir is None:
        return {"turn_key": timestamp, "files": [], "error": "not_found"}

    manifest_path = os.path.join(turn_dir, "manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    turn_key = manifest.get("turn_key", os.path.basename(turn_dir))
    journal_dir = turn_dir
    workspace = manifest.get("workspace") or os.getcwd()
    files: list[dict] = []

    for rel_path, entry in manifest.get("files", {}).items():
        rel_path = rel_path.replace("\\", "/")
        if not isinstance(entry, dict) or "baseline" not in entry or "after" not in entry:
            continue
        baseline_blob = entry["baseline"]
        after_blob = entry["after"]
        baseline_exists = bool(baseline_blob.get("exists"))
        after_exists = bool(after_blob.get("exists"))

        if baseline_exists and after_exists:
            change_type = "modified"
        elif after_exists and not baseline_exists:
            change_type = "added"
        elif baseline_exists and not after_exists:
            change_type = "deleted"
        else:
            continue

        before = b""
        after = b""
        try:
            if baseline_exists:
                before = _read_journal_blob(baseline_blob, workspace, journal_dir)
        except Exception:
            before = b"<read error>"
        try:
            if after_exists:
                after = _read_journal_blob(after_blob, workspace, journal_dir)
        except Exception:
            after = b"<read error>"

        # Metadata shown in the file-change list. Prefer the current workspace
        # stat for modification time and size, while retaining the journal size
        # as a fallback for files that have since been deleted.
        modified: Optional[int] = None
        size = after_blob.get("size") if after_exists else baseline_blob.get("size")
        workspace_path = os.path.realpath(os.path.join(workspace, rel_path))
        workspace_root = os.path.realpath(workspace)
        try:
            if os.path.commonpath([workspace_root, workspace_path]) == workspace_root:
                workspace_stat = os.lstat(workspace_path)
                modified = int(workspace_stat.st_mtime * 1000)
                size = workspace_stat.st_size
        except (FileNotFoundError, OSError, ValueError):
            pass

        file_obj: dict = {
            "path": rel_path,
            "change_type": change_type,
            "modified": modified,
            "size": size,
            "baseline": before.decode("utf-8", errors="replace"),
            "after": after.decode("utf-8", errors="replace"),
        }
        a_label = f"a/{rel_path}" if baseline_exists else "/dev/null"
        b_label = f"b/{rel_path}" if after_exists else "/dev/null"
        before_lines = file_obj["baseline"].splitlines(keepends=True)
        after_lines = file_obj["after"].splitlines(keepends=True)
        file_obj["diff"] = "".join(difflib.unified_diff(
            before_lines, after_lines,
            fromfile=a_label, tofile=b_label,
        ))
        files.append(file_obj)

    return {"turn_key": turn_key, "files": files}


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
            to rely solely on the environment variable (default ``"summary"``),
            which is re-read on every call so changes take effect without a
            restart.
        max_tokens_in_context: Hard-coded override for the token threshold
            that triggers compression.  When not ``None``, takes precedence
            over the ``MAX_TOKENS_IN_CONTEXT`` environment variable.  Leave
            as ``None`` (default) to rely solely on the environment variable,
            which is re-read on every call.  Default when neither is set: 65536.
        memory_confidence_threshold: Minimum confidence score for a
            ``MemoryEntry`` to be retained.
        model_registry: Optional model registry used to resolve the summary
            model reference (by ID or label).  When provided and the resolved
            ``SUMMARY_MODEL_ID`` is not found in the registry, a warning is
            logged and compression stays disabled.  When ``None`` (library
            usage), the resolved value is used as-is without validation.
    """

    _DEFAULT_MAX_TOKENS: int = 65536
    _DEFAULT_SUMMARY_MODEL_ID: str = "summary"

    def __init__(
        self,
        infer_fn: Callable,
        chats_dir: str = "./chats",
        recent_turns_k: int = 10,
        summary_model_id: str = "",
        max_tokens_in_context: Optional[int] = None,
        memory_confidence_threshold: float = 0.7,
        prompt_template_manager: Optional[object] = None,
        model_registry: Optional[object] = None,
    ) -> None:
        self._infer_fn = infer_fn
        self._chats_dir = chats_dir
        self._recent_turns_k = recent_turns_k
        # Store constructor overrides; None / "" means "defer to env var at call time"
        self._summary_model_id_override: str = summary_model_id
        self._max_tokens_override: Optional[int] = max_tokens_in_context
        self._memory_confidence_threshold = memory_confidence_threshold
        # Optional PromptTemplateManager used to resolve prompt-template
        # references inside system turns when merging context.
        self._prompt_template_manager = prompt_template_manager
        # Optional model registry used to resolve SUMMARY_MODEL_ID by ID/label.
        self._model_registry = model_registry
        self._memory_store: dict[str, list[MemoryEntry]] = {}
        # Per-session compression failure state (consecutive failures, token
        # count and timestamp at last failure) used for backoff.
        self._compress_failure_state: dict[str, dict] = {}
        # Sessions for which the placeholder-summary warning was already logged.
        self._placeholder_summary_warned: set[str] = set()

    # ------------------------------------------------------------------
    # Dynamic configuration properties (re-read env vars on every access)
    # ------------------------------------------------------------------

    @property
    def _summary_model_id(self) -> str:
        """Return the effective summary model ID.

        Priority: constructor override > ``SUMMARY_MODEL_ID`` env var >
        ``"summary"`` (default).  When a model registry is available and the
        resolved value is non-empty, it is looked up in the registry (by ID
        or label); if not found, a warning is logged and ``""`` is returned
        so compression stays disabled.  Other cases produce no log output.
        """
        if self._summary_model_id_override:
            candidate = self._summary_model_id_override
        else:
            candidate = os.environ.get(
                "SUMMARY_MODEL_ID", self._DEFAULT_SUMMARY_MODEL_ID
            )
        if not candidate:
            return ""
        if self._model_registry is None:
            return candidate
        config = self._model_registry.get(candidate)
        if config is None:
            logging.warning(
                "ContextManager: SUMMARY_MODEL_ID=%r not found in model "
                "registry; context compression disabled",
                candidate,
            )
            return ""
        return config.model_id

    @property
    def _max_tokens_in_context(self) -> int:
        """Return the effective token threshold for compression.

        Priority: constructor override > ``MAX_TOKENS_IN_CONTEXT`` env var >
        65536 (default).  Invalid env-var values are ignored with a warning
        and the default is used instead.
        """
        if self._max_tokens_override is not None:
            return self._max_tokens_override
        env_val = os.environ.get("MAX_TOKENS_IN_CONTEXT", "").strip()
        if env_val:
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

        The session_id is a timestamp string in ``YYMMDD_HHMMSS`` format. The
        ``/chats`` base directory is created automatically if it does not exist.

        Returns:
            The session_id string.
        """
        session_id = session_timestamp()
        session_dir = os.path.join(self._chats_dir, session_id)
        if os.path.exists(session_dir):
            raise OSError(f"Session directory already exists: {session_dir}")
        os.makedirs(session_dir)
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
        extra_meta: Optional[dict] = None,
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
            extra_meta: Optional dict of additional fields to merge into the
                ``meta`` block (e.g. ``parent_session_id``, ``tool_ids``).
                These are written alongside the standard fields in one atomic
                write, so no second read-modify-write pass is needed.
        """
        conv_path = self._conversation_path(session_id)

        # Preserve created_at from existing file when available.
        existing_created_at: Optional[str] = None
        existing_last_total_tokens: Optional[int] = None
        if os.path.isfile(conv_path):
            try:
                with open(conv_path, "r", encoding="utf-8") as fh:
                    existing = json.load(fh)
                existing_meta = existing.get("meta", {})
                existing_created_at = existing_meta.get("created_at")
                previous_tokens = existing_meta.get("last_total_tokens")
                if isinstance(previous_tokens, int):
                    existing_last_total_tokens = previous_tokens
            except (ValueError, OSError, KeyError):
                pass

        now = now_iso()
        meta: dict = {
            "session_id": session_id,
            "created_at": existing_created_at or now,
            "updated_at": now,
            "turn_count": len(turns),
        }
        # Incremental persistence may have already written the usage stats,
        # while the final persistence receives an empty message slice and thus
        # has no new ``last_total_tokens`` value.  Do not erase the latest token
        # count in that case: compression uses this field as its trigger.
        effective_last_total_tokens = (
            last_total_tokens
            if last_total_tokens is not None
            else existing_last_total_tokens
        )
        if effective_last_total_tokens is not None:
            meta["last_total_tokens"] = effective_last_total_tokens
        if extra_meta:
            meta.update(extra_meta)

        messages = [
            {k: v for k, v in asdict(turn).items() if v is not None}
            for turn in turns
        ]

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
                agent_id=msg.get("agent_id") or msg.get("assistant_id"),
                tool_id=msg.get("tool_id"),
                tool_use_id=msg.get("tool_use_id"),
                completed_at=msg.get("completed_at"),
                mentions=msg.get("mentions"),
                started_at=msg.get("started_at"),
            ))
        return turns

    def remove_trailing_assistant_message(
        self,
        session_id: str,
        expected_agent_id: Optional[str] = None,
    ) -> bool:
        """Explicitly remove the final assistant message for a Continue request.

        No completeness or content inference is performed. The caller must have
        received an explicit user action requesting this destructive operation.
        Metadata is preserved and the rewrite is atomic.
        """
        conv_path = self._conversation_path(session_id)
        if not os.path.isfile(conv_path):
            raise FileNotFoundError(f"Conversation file not found: {conv_path}")

        with open(conv_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        messages = data.get("messages", [])
        if not isinstance(messages, list) or not messages:
            return False

        last = messages[-1]
        if not isinstance(last, dict) or last.get("role") != "assistant":
            return False
        if expected_agent_id:
            actual_agent_id = last.get("agent_id") or last.get("assistant_id")
            if actual_agent_id and actual_agent_id != expected_agent_id:
                return False

        new_messages = messages[:-1]
        self._reconcile_compression_after_revoke(
            session_id, len(messages) - 1, len(new_messages)
        )

        meta = data.get("meta", {})
        if not isinstance(meta, dict):
            meta = {}
        meta["updated_at"] = now_iso()
        meta["turn_count"] = len(new_messages)
        data["meta"] = meta
        data["messages"] = new_messages
        self._atomic_write(
            conv_path, json.dumps(data, ensure_ascii=False, indent=2)
        )
        return True

    def _remove_session_file(self, path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def _reconcile_compression_after_revoke(
        self,
        session_id: str,
        revoke_index: int,
        new_turn_count: int,
    ) -> None:
        summary_text, summary_fm = self.get_summary(session_id)
        summarized_up_to = summary_fm.get("summarized_up_to_turn", -1)
        if not isinstance(summarized_up_to, int):
            summarized_up_to = -1

        cuts_summarized_history = bool(summary_text.strip()) and revoke_index <= summarized_up_to
        if cuts_summarized_history:
            self._remove_session_file(self._summary_path(session_id))
            self._remove_session_file(self._memory_path(session_id))
            self._memory_store.pop(session_id, None)
            return

        if summary_text.strip() and summarized_up_to >= new_turn_count:
            summary_fm["summarized_up_to_turn"] = max(-1, new_turn_count - 1)
            summary_fm["updated_at"] = now_iso()
            if summary_fm["summarized_up_to_turn"] < 0:
                self._remove_session_file(self._summary_path(session_id))
            else:
                self._atomic_write(self._summary_path(session_id), serialize_summary(summary_fm, summary_text))

        entries = [entry for entry in self.load_memory(session_id) if entry.source_turn_index < new_turn_count]
        if entries:
            self.save_memory(session_id, entries)
            self._memory_store[session_id] = entries
        else:
            self._remove_session_file(self._memory_path(session_id))
            self._memory_store.pop(session_id, None)

    def revoke_conversation(
        self,
        session_id: str,
        timestamp: str,
        force: bool = False,
        keep_files: bool = False,
    ) -> dict:
        """Revoke messages from the conversation starting from the message with the given timestamp.

        Args:
            session_id: Target session.
            timestamp: The timestamp of the user message to revoke from.

        Returns:
            A result dict containing the number of removed messages and the
            git rewrite result for workspace commits associated with the same
            session/timestamp.

        Raises:
            FileNotFoundError: When the conversation file does not exist.
            ValueError: When the file content is malformed JSON or no message found with the given timestamp.
            RuntimeError: When git history rewriting fails.
        """
        conv_path = self._conversation_path(session_id)
        if not os.path.isfile(conv_path):
            raise FileNotFoundError(f"Conversation file not found: {conv_path}")

        with open(conv_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        messages = data.get("messages", [])

        # Find the index of the message with the given timestamp
        revoke_index = -1
        for i, msg in enumerate(messages):
            if msg.get("role") == "user" and msg.get("timestamp") == timestamp:
                revoke_index = i
                break

        if revoke_index == -1:
            raise ValueError(f"No user message found with timestamp: {timestamp}")

        # Remove all messages from the revoke_index onwards
        removed_count = len(messages) - revoke_index
        new_messages = messages[:revoke_index]

        workspace = get_workspace()
        session_dir = os.path.dirname(conv_path)
        journal_result = revoke_session_file_changes(
            workspace,
            session_dir,
            session_id,
            timestamp,
            force=force,
            keep_files=keep_files,
        )
        if isinstance(journal_result, dict) and journal_result.get("error"):
            if journal_result.get("error") == "JournalConflict":
                raise JournalConflictError(
                    journal_result.get("message") or "Current files do not match journal after-state",
                    list(journal_result.get("files") or []),
                )
            if journal_result.get("error") == "InvalidTimestamp":
                raise ValueError(journal_result.get("message") or journal_result["error"])
            raise RuntimeError(journal_result.get("message") or journal_result["error"])
        git_result = {"skipped": True, "reason": "replaced_by_file_journal", "removed_commits": []}
        self._reconcile_compression_after_revoke(session_id, revoke_index, len(new_messages))

        # Update meta and save only after journal validation/restore succeeds. This
        # keeps conversation history and workspace files in sync if the journal
        # refuses to restore because of conflicts.
        now = now_iso()
        meta = data.get("meta", {})
        meta["updated_at"] = now
        meta["turn_count"] = len(new_messages)

        data = {"meta": meta, "messages": new_messages}
        text = json.dumps(data, ensure_ascii=False, indent=2)
        self._atomic_write(conv_path, text)

        return {
            "removed_count": removed_count,
            "git": git_result,
            "journal": journal_result,
        }

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

    def get_session_meta(self, session_id: str) -> dict:
        """Return the ``meta`` object from ``conversation.json``.

        Returns an empty dict when the file does not exist or is malformed.
        Used e.g. by the remote-execution handlers to read the session's
        ``remote_env`` binding (the child environment that owns this
        session's file journals).
        """
        conv_path = self._conversation_path(session_id)
        if not os.path.isfile(conv_path):
            return {}
        try:
            with open(conv_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            meta = data.get("meta") if isinstance(data, dict) else None
            return meta if isinstance(meta, dict) else {}
        except (ValueError, OSError):
            return {}

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
        now = now_iso()
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

    def _compress_backoff_active(
        self, session_id: str, tokens: int, forced: bool = False
    ) -> bool:
        """Return True when compression for *session_id* is in failure backoff.

        After repeated consecutive failures, further attempts are skipped until
        the session's token count grows by 25 % or 30 minutes pass, so an
        over-budget session does not re-send the same doomed request on every
        turn.  ``forced=True`` bypasses the backoff.
        """
        if forced:
            return False
        state = self._compress_failure_state.get(session_id)
        if not state or state.get("fails", 0) < _COMPRESS_BACKOFF_MIN_FAILURES:
            return False
        if tokens >= state.get("tokens", 0) * _COMPRESS_BACKOFF_TOKEN_GROWTH:
            return False
        if time.monotonic() - state.get("at", 0.0) > _COMPRESS_BACKOFF_SECONDS:
            return False
        return True

    def _record_compress_failure(self, session_id: str, tokens: int) -> None:
        state = self._compress_failure_state.setdefault(
            session_id, {"fails": 0, "tokens": 0, "at": 0.0}
        )
        state["fails"] += 1
        state["tokens"] = max(state.get("tokens", 0), tokens)
        state["at"] = time.monotonic()

    def _record_compress_success(self, session_id: str) -> None:
        self._compress_failure_state.pop(session_id, None)
        self._placeholder_summary_warned.discard(session_id)

    def _merge_memory_entries(
        self, session_id: str, new_entries: list[MemoryEntry]
    ) -> list[MemoryEntry]:
        """Merge *new_entries* with the session's existing memory entries.

        The compression prompt does not include the existing memory file, so
        the model only ever emits NEW entries — persisting them alone would
        wipe everything extracted earlier.  Entries are de-duplicated by
        ``(entry_type, content)`` and the result is capped to the most recent
        ``_MEMORY_MAX_ENTRIES`` entries.
        """
        existing = [
            e for e in self.load_memory(session_id)
            if not _is_placeholder_memory_entry(e.content)
        ]
        seen = {(e.entry_type, e.content) for e in existing}
        for entry in new_entries:
            key = (entry.entry_type, entry.content)
            if key not in seen:
                seen.add(key)
                existing.append(entry)
        if len(existing) > _MEMORY_MAX_ENTRIES:
            existing.sort(key=lambda e: e.created_at or "")
            existing = existing[-_MEMORY_MAX_ENTRIES:]
        return existing

    def _build_compression_prompt(
        self,
        delta: list[ConversationTurn],
        delta_start: int,
        previous_summary: str,
        budget_tokens: Optional[int],
        current_ts: str,
    ) -> str:
        """Build the compression prompt for the delta.

        ``delta`` holds the turns NOT yet covered by ``previous_summary``;
        ``delta[i]`` is global turn index ``delta_start + i``.

        ``budget_tokens=None`` (the normal case): the FULL delta is sent —
        only pathological single turns are capped at
        ``_SUMMARY_TURN_MAX_CHARS`` chars and the previous summary at
        ``_PREV_SUMMARY_MAX_TOKENS`` tokens.  ``budget_tokens`` as an int
        (overflow-retry case): the whole prompt is additionally bounded to
        that many tokens — the previous summary is capped at 1/4 of the
        budget, then the OLDEST turns are dropped (a marker documents the
        loss), and as a last resort the newest turn is hard-truncated.
        """
        overhead = (
            _estimate_tokens_fast(_COMPRESSION_PROMPT_HEAD)
            + _estimate_tokens_fast(_COMPRESSION_PROMPT_TAIL)
            + 128  # history section headers + omission marker
        )

        prev = previous_summary
        if budget_tokens is not None:
            prev_budget = max(2048, budget_tokens // 4)
        else:
            prev_budget = _PREV_SUMMARY_MAX_TOKENS
        if prev and _estimate_tokens_fast(prev) > prev_budget:
            prev = _truncate_to_token_budget(prev, prev_budget)

        turns_budget: Optional[int]
        if budget_tokens is not None:
            turns_budget = max(0, budget_tokens - overhead - _estimate_tokens_fast(prev))
        else:
            turns_budget = None  # no total cap: send the full delta

        rendered: list[str] = []
        estimates: list[int] = []
        for i, t in enumerate(delta):
            content = _truncate_turn_content(t.content)
            line = f"Turn {delta_start + i} {_turn_compression_label(t)}: {content}"
            rendered.append(line)
            estimates.append(_estimate_tokens_fast(line) + 1)

        dropped = 0
        if turns_budget is not None:
            total = sum(estimates)
            while total > turns_budget and dropped < len(rendered):
                total -= estimates[dropped]
                dropped += 1

            if dropped == len(rendered):
                # Not a single turn fits — keep only the most recent one,
                # hard-truncated.
                if turns_budget > 0:
                    rendered = [
                        f"Turn {delta_start + len(delta) - 1} "
                        f"{_turn_compression_label(delta[-1])}: "
                        + _truncate_to_token_budget(delta[-1].content, turns_budget)
                    ]
                    dropped = len(delta) - 1
                else:
                    rendered = []
                    dropped = len(delta)
            elif dropped > 0:
                rendered = rendered[dropped:]
                rendered.insert(
                    0,
                    f"[... {dropped} older turn(s) omitted: compression prompt "
                    f"budget of {budget_tokens} tokens exceeded ...]",
                )

        if rendered:
            turns_text = "\n".join(rendered)
        else:
            turns_text = (
                f"[no conversation turns fit the compression prompt budget of "
                f"{budget_tokens} tokens]"
            )
        if prev:
            history_section = (
                f"## Previous summary\n{prev}\n\n"
                f"## New turns to incorporate\n{turns_text}"
            )
        else:
            history_section = f"## Conversation turns\n{turns_text}"

        prompt = _COMPRESSION_PROMPT_HEAD + history_section + _COMPRESSION_PROMPT_TAIL
        return prompt.replace("{current_ts}", current_ts)

    def compress_context(
        self,
        session_id: str,
        turns: list[ConversationTurn],
        last_total_tokens: Optional[int] = None,
        forced: bool = False,
    ) -> None:
        """Compress conversation history in a single LLM call.

        Produces both a rolling summary and structured memory entries from the
        turns that fall outside the recent-K window.  Results are persisted to
        ``summary.md`` and ``memory.md`` respectively.

        Triggered when ALL of the following are true:

        - ``SUMMARY_MODEL_ID`` resolves to a registered model (non-empty;
          default ``"summary"``).
        - ``last_total_tokens`` exceeds the effective ``MAX_TOKENS_IN_CONTEXT``
          threshold (env var, default 65536) — unless *forced* is true.
        - There is at least one turn NOT yet covered by a valid previous
          summary.  Compression is INCREMENTAL: only the delta since the last
          successful summary is sent to the LLM (merged with that previous
          summary), so the prompt stays small no matter how long the session
          history has grown.

        All configuration values are re-read from the environment on every
        call so changes take effect without a restart.

        The delta is sent IN FULL — in normal operation it is inherently
        bounded by the ``MAX_TOKENS_IN_CONTEXT`` trigger threshold, so an
        extra prompt budget would only discard context the summary model
        could handle.  The only pre-emptive limits are per pathological
        single turns (``_SUMMARY_TURN_MAX_CHARS`` chars).  If the summary
        model nevertheless rejects the request with a context-overflow error
        (e.g. a very small summary-model window, or a delta that grew while
        compression kept failing), the prompt is rebuilt with a token budget
        halved from the natural prompt size — dropping the oldest delta turns
        first — down to 8192 tokens.

        An ACCEPTED request that still comes back with an EMPTY answer is
        handled the same way: when the response used completion tokens (a
        thinking model spent the remaining output budget on reasoning —
        typical when the prompt nearly fills the provider's TOTAL token
        window, e.g. context + output capped at 1M), the prompt is shrunk
        like an overflow retry so the model has room to both think and
        answer; when no completion tokens were used at all the same prompt
        is resent up to ``_MAX_EMPTY_OUTPUT_RETRIES`` times.

        Failure semantics (important):

        - If the LLM call fails entirely (HTTP error, timeout, empty output,
          or output that is the prompt's own format example), a warning is
          logged and both files are left UNCHANGED.  A failed result object
          must never be mistaken for model output: on errors the runtime
          returns the INPUT messages (whose last entry is this very prompt,
          containing the ``<summary>``/``<memory>`` format example), and
          parsing that used to persist the template placeholders into
          ``summary.md``/``memory.md``.
        - Consecutive failures put the session into a short backoff window
          (no retries until the session's token count grows by 25 % or 30
          minutes pass) so over-budget sessions do not re-send the same
          doomed request on every turn.
        - If only the memory JSON is malformed, the summary is still saved
          and a warning is logged for the memory part.

        Args:
            session_id: Target session.
            turns: Current full list of conversation turns.
            last_total_tokens: Total tokens (prompt + completion) from the most
                recent inference.  When ``None``, the value is read from the
                ``last_total_tokens`` field in ``conversation.json``.
            forced: When true, skip the token-threshold check and the failure
                backoff (manual regeneration).
        """
        if not self._summary_model_id:
            return

        # Resolve last_total_tokens
        effective_tokens = last_total_tokens
        if effective_tokens is None:
            effective_tokens = self.get_last_total_tokens(session_id)
        if effective_tokens is None:
            return  # no token data available yet
        if not forced and effective_tokens <= self._max_tokens_in_context:
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

        # Read existing summary for version tracking and incremental update.
        # A placeholder summary (the prompt's own format example, persisted by
        # an older bug after a failed LLM call) is NOT a valid previous
        # summary — treat it as "no summary" so the delta restarts from turn 0.
        existing_summary, existing_fm = self.get_summary(session_id)
        existing_version: int = existing_fm.get("summary_version", 0)  # type: ignore[assignment]
        if not isinstance(existing_version, int):
            existing_version = 0
        previous_summary = ""
        delta_start = 0
        if existing_summary.strip() and not _is_placeholder_summary(existing_summary):
            previous_summary = existing_summary
            raw_cut = existing_fm.get("summarized_up_to_turn", -1)
            if isinstance(raw_cut, int):
                delta_start = max(0, min(raw_cut + 1, len(turns)))

        # Incremental compression: only the turns NOT covered by the previous
        # summary are sent to the LLM.  Sending the full history on every call
        # made the prompt grow without bound and blow past the summary model's
        # context window in long sessions.
        delta = turns[delta_start: summarized_up_to + 1]
        if not delta:
            logging.info(
                "compress_context: nothing new to compress for session %s "
                "(previous summary already covers the compressible turns)",
                session_id,
            )
            return

        # Backoff: after repeated failures do not re-send the same doomed
        # request on every turn.
        if self._compress_backoff_active(session_id, effective_tokens or 0, forced=forced):
            logging.debug(
                "compress_context: session %s in compression failure backoff; skipping",
                session_id,
            )
            return

        current_ts = now_iso()
        # None = no budget: the first attempt sends the full delta.  Only a
        # rejection installs a budget (halved from the natural size): either
        # an overflow 400, or an accepted request that came back empty after
        # burning its completion budget on thinking.
        budget: Optional[int] = None
        error = ""
        raw_output = ""
        success = False
        attempt = 0
        empty_retries = 0
        while True:
            attempt += 1
            shrink = False
            prompt = self._build_compression_prompt(
                delta, delta_start, previous_summary, budget, current_ts
            )

            result = None
            try:
                from runtime.models import InferenceRequest, Message as _Message  # local import to avoid circular deps
                infer_request = InferenceRequest(
                    model_id=self._summary_model_id,
                    messages=[_Message(role="user", content=prompt)],
                )
                result = self._infer_fn(infer_request)
                # A FAILED result must never be parsed as model output: on
                # errors the runtime returns the INPUT messages (whose last
                # entry is this very prompt, containing the <summary>/<memory>
                # format example).  Parsing that used to persist the template
                # placeholders into summary.md / memory.md.
                if getattr(result, "success", True) is False:
                    success = False
                    error = str(getattr(result, "error", None) or "unknown error")
                    raw_output = ""
                else:
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
                    error = ""
                    success = True
            except Exception as exc:  # noqa: BLE001
                success = False
                error = str(exc)
                raw_output = ""
                logging.warning(
                    "compress_context: LLM call failed for session %s: %s",
                    session_id,
                    exc,
                )

            if success and not raw_output.strip():
                success = False
                # The provider ACCEPTED the request but no answer came back.
                # (a) completion tokens were used: a thinking model spent the
                #     remaining completion budget on reasoning and never got
                #     to the answer.  This happens when the prompt nearly
                #     fills the provider's TOTAL token window (context +
                #     output), leaving only a few hundred output tokens.
                #     Shrinking the prompt gives the model room to both think
                #     and answer, so retry with a halved budget like overflow.
                # (b) no completion tokens at all: a transient empty
                #     response - resend the same prompt a couple of times.
                stat = getattr(result, "stat", None) if result is not None else None
                used_completion = int(getattr(stat, "completion_tokens", 0) or 0)
                if used_completion > 0:
                    error = (
                        "empty model output (completion budget exhausted "
                        "by thinking)"
                    )
                    shrink = attempt < _MAX_OVERFLOW_RETRIES
                elif empty_retries < _MAX_EMPTY_OUTPUT_RETRIES:
                    empty_retries += 1
                    logging.warning(
                        "compress_context: empty model output for session %s "
                        "(no completion tokens); resending the same prompt "
                        "(%d/%d)",
                        session_id, empty_retries, _MAX_EMPTY_OUTPUT_RETRIES,
                    )
                    continue
                else:
                    error = "empty model output"

            if success:
                break

            # Context-overflow rejection (HTTP 400), or a thinking model that
            # consumed the completion budget on reasoning without answering
            # (accepted request, empty content): retry with a prompt budget
            # halved from the natural prompt size, dropping the oldest delta
            # turns first, down to _SUMMARY_PROMPT_MIN_TOKENS.
            if (
                not shrink
                and error
                and _is_overflow_error(error)
                and attempt < _MAX_OVERFLOW_RETRIES
            ):
                shrink = True
            if shrink:
                if budget is None:
                    budget = max(
                        2 * _SUMMARY_PROMPT_MIN_TOKENS,
                        _estimate_tokens_fast(prompt),
                    )
                next_budget = max(_SUMMARY_PROMPT_MIN_TOKENS, budget // 2)
                if next_budget < budget:
                    budget = next_budget
                    logging.warning(
                        "compress_context: prompt did not fit the summary "
                        "model for session %s (%s); retrying with a %d-token "
                        "prompt budget (oldest delta turns dropped)",
                        session_id, error, budget,
                    )
                    continue
            break

        if not success:
            logging.warning(
                "compress_context: LLM call failed for session %s (%s); "
                "summary.md and memory.md left unchanged",
                session_id, error or "unknown error",
            )
            self._record_compress_failure(session_id, effective_tokens or 0)
            return

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
        if not summary_text or _is_placeholder_summary(summary_text):
            # The model echoed the prompt's format example instead of a real
            # summary (typically after a truncated or failed request).  Never
            # persist that.
            logging.warning(
                "compress_context: LLM output for session %s is a placeholder "
                "echo of the prompt format; not persisting summary/memory",
                session_id,
            )
            self._record_compress_failure(session_id, effective_tokens or 0)
            return

        # Persist summary
        now = now_iso()
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

        # --- Parse memory block (merge with existing entries) ---
        memory_json_str = _extract_tagged_block(raw_output, "memory")
        if memory_json_str:
            try:
                entries_data = json.loads(memory_json_str)
                if not isinstance(entries_data, list):
                    raise ValueError("Expected a JSON array inside <memory>")
                entries: list[MemoryEntry] = []
                rejected = 0
                for item in entries_data:
                    if not isinstance(item, dict):
                        rejected += 1
                        continue
                    entry_type = item.get("entry_type")
                    content = str(item.get("content") or "").strip()
                    if entry_type not in _VALID_ENTRY_TYPES \
                            or _is_placeholder_memory_entry(content):
                        rejected += 1
                        continue
                    try:
                        source_turn_index = int(item.get("source_turn_index", 0))
                        confidence = float(item.get("confidence", 0.0))
                    except (TypeError, ValueError):
                        rejected += 1
                        continue
                    confidence = max(0.0, min(1.0, confidence))
                    if confidence >= self._memory_confidence_threshold:
                        entries.append(
                            MemoryEntry(
                                entry_type=entry_type,
                                content=content,
                                source_turn_index=source_turn_index,
                                confidence=confidence,
                                created_at=str(item.get("created_at") or now),
                            )
                        )
                if rejected:
                    logging.warning(
                        "compress_context: rejected %d malformed/placeholder "
                        "memory entries for session %s",
                        rejected, session_id,
                    )
                merged = self._merge_memory_entries(session_id, entries)
                # Persist to memory.md and update in-memory cache
                self.save_memory(session_id, merged)
                self._memory_store[session_id] = merged
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

        self._record_compress_success(session_id)

    def compress_context_forced(self, session_id: str) -> None:
        """Force regeneration of summary and memory for *session_id*.

        Similar to :meth:`compress_context` but skips the token-threshold
        check, making it suitable for manual re-generation (e.g. after a
        previous automatic attempt failed due to a missing model or API error).

        All other guards still apply (e.g. ``SUMMARY_MODEL_ID`` must resolve
        to a registered model).
        """
        if not self._summary_model_id:
            logging.warning(
                "compress_context_forced: SUMMARY_MODEL_ID not configured; "
                "cannot regenerate summary/memory for session %s",
                session_id,
            )
            return

        turns = self.load_conversation(session_id)
        if not turns:
            logging.warning(
                "compress_context_forced: no turns found for session %s",
                session_id,
            )
            return

        # Pass a large fake token count and forced=True to bypass the threshold
        # check and the failure backoff while still going through the same code
        # path.
        fake_tokens = self._max_tokens_in_context + 1
        self.compress_context(
            session_id, turns, last_total_tokens=fake_tokens, forced=True
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
        # Drop corrupted placeholder entries (echo of the prompt's format
        # example persisted by an older bug after a failed compression).
        entries = [
            e for e in entries if not _is_placeholder_memory_entry(e.content)
        ]
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
        atomic_write_text(path, text)

    def _add_reference(self, session_id: str, ref: str) -> None:
        """No-op — references were part of the old Markdown format.

        Kept for API compatibility only.
        """

    # ------------------------------------------------------------------
    # Context assembly
    # ------------------------------------------------------------------

    def _system_message_text(self, msg: dict) -> str:
        """Return the effective text of a system-role message dict.

        Plain ``content`` messages are returned as-is.  Prompt-template
        references (empty ``content`` + ``prompt_template``) are resolved
        against the optional ``prompt_template_manager`` and their
        ``arguments`` are substituted, mirroring ``Runtime._normalize_messages``.
        Returns ``""`` when nothing can be resolved (so the caller can drop
        the part without breaking assembly).
        """
        content = msg.get("content") or ""
        if content:
            return content
        template_id = msg.get("prompt_template")
        manager = self._prompt_template_manager
        if template_id and manager is not None:
            try:
                template = manager.get(template_id)
            except Exception:  # noqa: BLE001 — never block assembly on a bad template
                template = None
            if template is not None:
                text = getattr(template, "content", None) or ""
                for key, value in (msg.get("arguments") or {}).items():
                    text = text.replace(f"{{{{{key}}}}}", str(value))
                return text
        return ""

    @staticmethod
    def _merge_system_parts(
        system_parts: list[str],
        summary_part: Optional[str],
        memory_parts: list[str],
    ) -> Optional[dict]:
        """Combine system-prompt / summary / memory parts into ONE system message.

        Merging all head system messages into a single ``{"role": "system"}``
        message keeps the assembled context compatible with request-format
        specifications that reject (or discourage) multiple system messages
        (e.g. Anthropic's single top-level ``system`` field and strict
        OpenAI-compatible servers).

        Returns ``None`` when there is no non-empty part to include.
        """
        parts = [p for p in [*system_parts, summary_part, *memory_parts] if p and p.strip()]
        if not parts:
            return None
        return {"role": "system", "content": "\n\n".join(parts)}

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
        1. ONE merged ``{"role": "system", ...}`` message combining the agent /
           session system prompt(s), the rolling summary and the structured
           memory entries
        2. Most recent ``min(K, len(turns))`` conversation turns
        3. *new_messages* appended as-is

        The ``recent_turns_k`` parameter only controls how many turns are kept
        verbatim when compression is active.  It has no effect when the
        conversation is still within the token budget.

        When *token_budget* is provided and > 0, the assembled list is
        trimmed to fit within the budget:
        - Structured memory entries are removed oldest-first.
        - If still over budget after removing all memory, the summary part is
          removed.

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
        summary_present = bool(summary_text.strip())
        if summary_present and _is_placeholder_summary(summary_text):
            # Corrupted artifact of a failed compression (the prompt's own
            # format example was persisted as the "summary").  Stay in the
            # compressed branch so the full history is NOT re-injected into
            # the inference context; only the summary part itself is dropped.
            if session_id not in self._placeholder_summary_warned:
                self._placeholder_summary_warned.add(session_id)
                logger.warning(
                    "assemble_context(session=%s): summary.md contains "
                    "placeholder text from a failed compression; ignoring its "
                    "content (run compress_context_forced to regenerate)",
                    session_id,
                )
            summary_text = ""
        summary_msg: Optional[dict] = None
        if summary_text.strip():
            summary_msg = {
                "role": "system",
                "content": f"## Summary\n{summary_text}",
            }

        if summary_msg is None and not summary_present:
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
                if total_tokens > effective_budget:
                    logger.warning(
                        "assemble_context(session=%s): no summary to trim; "
                        "assembled %d tokens exceeds token_budget=%d — turns are "
                        "never truncated per spec, caller must handle overflow",
                        session_id, total_tokens, effective_budget,
                    )
            return assembled

        # Compression is active — use a SINGLE merged system message plus
        # unsummarized recent context.
        #
        # Multiple system messages at the head of a request violate the
        # request-format conventions of several providers (Anthropic accepts
        # exactly one top-level ``system`` field; many OpenAI-compatible
        # servers reject repeated ``role="system"`` entries).  We therefore
        # merge the agent/session system prompt(s), the rolling summary and
        # the structured memory entries into one ``system`` message.

        # 1. Agent / session system prompt(s) from persisted system turns.
        system_parts: list[str] = []
        for t in turns:
            if t.role == "system" and (t.content or t.prompt_template):
                msg = {k: v for k, v in asdict(t).items() if v is not None}
                text = self._system_message_text(msg)
                if text.strip():
                    system_parts.append(text.strip())

        # 2. Structured memory entries (dropped oldest-first when over budget).
        memory_entries = self.get_memory_entries(session_id)
        memory_parts: list[str] = [
            f"## Memory\n{entry.entry_type}: {entry.content}"
            for entry in memory_entries
        ]

        # 3. Recent/unsummarized turns.
        k = self._recent_turns_k
        summarized_up_to = summary_fm.get("summarized_up_to_turn", -1)
        if not isinstance(summarized_up_to, int):
            summarized_up_to = -1
        if turns:
            summarized_up_to = min(summarized_up_to, len(turns) - 1)
        else:
            summarized_up_to = -1
        unsummarized_floor = max(0, summarized_up_to + 1)
        recent_start = max(max(0, len(turns) - k), unsummarized_floor)
        recent_start = min(recent_start, len(turns))
        allow_before_floor_for_tool_chain = False
        if 0 < recent_start < len(turns) and turns[recent_start].role == "tool":
            cursor = recent_start - 1
            while cursor >= 0 and turns[cursor].role == "tool":
                cursor -= 1
            if cursor >= 0 and turns[cursor].role == "assistant" and turns[cursor].tool_calls:
                # Protocol integrity first: if the window starts from a tool_result,
                # include its matching assistant(tool_use) even if that assistant
                # is in summarized area.
                recent_start = cursor
                allow_before_floor_for_tool_chain = recent_start < unsummarized_floor
            else:
                while recent_start < len(turns) and turns[recent_start].role == "tool":
                    recent_start += 1
        while (
            unsummarized_floor < recent_start < len(turns)
            and turns[recent_start].role not in ("user", "system")
        ):
            recent_start -= 1
        # Second tool-chain integrity check: the backtrack loop above may have
        # moved recent_start onto a tool message whose matching assistant is
        # still outside the window (in the summarized area).  Without the
        # matching assistant(tool_use), Claude API will reject the orphan
        # tool_result with "No tool call found for function call output".
        if 0 < recent_start < len(turns) and turns[recent_start].role == "tool":
            cursor = recent_start - 1
            while cursor >= 0 and turns[cursor].role == "tool":
                cursor -= 1
            if cursor >= 0 and turns[cursor].role == "assistant" and turns[cursor].tool_calls:
                recent_start = cursor
                allow_before_floor_for_tool_chain = True  # allow going before floor
            else:
                # No matching assistant found — skip orphan tool results
                while recent_start < len(turns) and turns[recent_start].role == "tool":
                    recent_start += 1
        if recent_start < unsummarized_floor and not allow_before_floor_for_tool_chain:
            recent_start = unsummarized_floor
        if recent_start < len(turns) and turns[recent_start].role == "system":
            recent_start += 1
        recent_turns = turns[recent_start:]
        turn_msgs = [
            {k: v for k, v in asdict(t).items() if v is not None}
            for t in recent_turns
            if t.role != "system"
        ]

        # 4. Rolling summary (dropped when still over budget after memory).
        summary_part: Optional[str] = None
        if summary_text.strip():
            summary_part = f"## Summary\n{summary_text}"

        # Merge everything into ONE system message (see note above).
        merged_system = self._merge_system_parts(system_parts, summary_part, memory_parts)
        assembled = ([merged_system] if merged_system is not None else []) + turn_msgs + new_messages

        # Apply token budget if set (memory oldest-first, then summary).
        if effective_budget is not None:
            total_tokens = sum(estimate_tokens(str(msg)) for msg in assembled)
            while memory_parts and total_tokens > effective_budget:
                memory_parts.pop(0)
                merged_system = self._merge_system_parts(system_parts, summary_part, memory_parts)
                assembled = ([merged_system] if merged_system is not None else []) + turn_msgs + new_messages
                total_tokens = sum(estimate_tokens(str(msg)) for msg in assembled)
            # If still over budget, remove the summary part.
            if summary_part is not None and total_tokens > effective_budget:
                summary_part = None
                merged_system = self._merge_system_parts(system_parts, summary_part, memory_parts)
                assembled = ([merged_system] if merged_system is not None else []) + turn_msgs + new_messages

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
