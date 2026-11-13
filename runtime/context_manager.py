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
    """The current files do not match what the journal says they should be.

    ``checked_workspaces`` names the root(s) the restore was about to write
    to, so a conflict report says *where* it looked -- without it a
    wrong-workspace session's conflict is indistinguishable from a file the
    user edited by hand.
    """

    def __init__(
        self,
        message: str,
        files: list[str],
        checked_workspaces: Optional[list[str]] = None,
    ):
        super().__init__(message)
        self.files = files
        self.checked_workspaces = checked_workspaces or []

    def to_dict(self) -> dict:
        payload = {
            "error": "JournalConflict",
            "message": str(self),
            "files": self.files,
            "can_force": True,
        }
        if self.checked_workspaces:
            payload["checked_workspaces"] = self.checked_workspaces
        return payload


class JournalWorkspaceMismatchError(RuntimeError):
    """A file journal was taken in a workspace root this machine cannot use.

    Raised instead of :class:`JournalConflictError` when the root recorded in
    a journal manifest does not exist here (or is not a directory).  Unlike a
    conflict, forcing this restore would *create* the missing tree and write
    the session's files into an unrelated directory -- the failure mode that
    turned a wrong-workspace revoke into an irreversible mess -- so the client
    is told ``can_force: false``.

    Attributes:
        workspaces: Roots the journal recorded that are unusable here.
        checked_workspaces: Every root this restore would have written to.
        files: Path labels that could not be restored.
    """

    def __init__(
        self,
        message: str,
        workspaces: list[str],
        files: Optional[list[str]] = None,
        checked_workspaces: Optional[list[str]] = None,
    ):
        super().__init__(message)
        self.workspaces = workspaces
        self.checked_workspaces = checked_workspaces or list(workspaces)
        self.files = files or []

    def to_dict(self) -> dict:
        return {
            "error": "JournalWorkspaceMismatch",
            "message": str(self),
            "workspaces": self.workspaces,
            "checked_workspaces": self.checked_workspaces,
            "files": self.files,
            "can_force": False,
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
        meta: Optional per-turn request-context snapshot, recorded on user
            turns only.  A session may switch model / tool set / execution
            environment / agent / workspace between turns, while the
            top-level ``meta`` block of ``conversation.json`` only ever
            holds the latest values.  This dict keeps what each user turn
            was actually sent with: ``model_id``, ``tool_ids``,
            ``agent_ids``, ``workspace``, ``remote_env``.  Persistence-only
            bookkeeping — never sent to the model (see
            :func:`turn_to_message_dict`).
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
    meta: Optional[dict] = None


def turn_to_message_dict(turn: ConversationTurn) -> dict:
    """Serialize a turn into a model-facing message dict.

    ``meta`` is persistence-only bookkeeping: the request context (model,
    tool set, execution environment, agent, workspace) that was in effect
    when this user turn was sent.  It describes how the session was run,
    not what was said in it, so it must never reach the provider payload
    nor inflate token estimates.  Every other field is passed through.
    """
    return {
        k: v
        for k, v in asdict(turn).items()
        if v is not None and k != "meta"
    }


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

from runtime.common import (  # noqa: F401 — re-export
    estimate_tokens,
    estimate_chat_prompt_tokens,
)


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
# "trigger-threshold worth" of new turns can accumulate.  A running tool loop
# is additionally guarded mid-request against the model's own window: when a
# completed round reaches 90 % of ``ModelConfig.max_context`` the session is
# compressed before the next round (see ``compress_context_in_loop``), so a
# single inference cannot grow past the model window between two HTTP requests.
# The delta is therefore sent IN FULL; no artificial prompt budget is applied.
#
# Guards:
#
#   - a single pathological turn (e.g. a multi-MB tool output) is offloaded to
#     a file and replaced by an excerpt plus its path (``spill_reference``), so
#     the detail stays reachable instead of being silently cut; only if that
#     write fails is the turn cut to ``_SUMMARY_TURN_MAX_CHARS`` chars (head
#     3/4 + tail 1/4).  Offloading only happens inside a compression round,
#     never because content is merely long;
#   - the prompt is bounded UP FRONT by the compression model's context window
#     (``ModelConfig.max_context``) so the request succeeds in ONE call: the
#     budget leaves room for the summary/memory output and, once a known window
#     is available, the OLDEST delta turns are dropped (a marker documents the
#     loss) to stay inside it;
#   - when no window is known for either the summary model or the inference
#     model, the delta is sent in full (best effort, as before);
#   - an ACCEPTED request that returns an EMPTY answer using NO completion
#     tokens is a transient provider blip and the SAME (already window-bounded)
#     prompt is resent a few times;
#   - repeated failures enter a backoff window so over-budget sessions do not
#     hammer the summary model with the same doomed request on every turn.

_SUMMARY_TURN_MAX_CHARS: int = 20000
#: Cap for the "previous summary" section when a prompt budget is active; a
#: rolling summary should be compact (quality limit, not a window constraint).
_PREV_SUMMARY_MAX_TOKENS: int = 16384
#: Output tokens reserved inside the compression model's context window for the
#: summary + memory answer, so the (bounded) prompt plus the reply always fit in
#: one request.  The prompt budget is ``window - _COMPRESSION_OUTPUT_RESERVE``.
_COMPRESSION_OUTPUT_RESERVE: int = 16384
#: Maximum number of same-prompt retries after an empty answer that used no
#: completion tokens at all (transient provider behavior).
_MAX_EMPTY_OUTPUT_RETRIES: int = 2
_MEMORY_MAX_ENTRIES: int = 200
#: Hard cap on the memory block that ``memory.md`` contributes to EVERY request.
#: Entries accumulate (each compression round adds more) and the whole block is
#: merged into the system message, so an unbounded file silently taxes every
#: future request of the session: one real session carried 200 entries /
#: 30 344 content characters, injected as 32 743 characters ~= 10 600 estimated
#: tokens on EVERY request (its memory.md itself was 60 282 characters, half of
#: that being JSON keys).  The cap is charged
#: in the RENDERED form (what is injected), oldest entries are dropped first, and
#: the drop is permanent (memory.md is the only store of these entries; the raw
#: turns stay in conversation.json), which is why the same budget is also handed
#: to the extraction prompt (see ``_memory_context_char_limit``) so the model
#: merges what it already has instead of accumulating near-duplicates.
_MEMORY_MAX_CHARS: int = 32_768
#: How much of the ALREADY STORED memory the compression prompt is shown.  Without
#: it the model cannot know what is already recorded and re-emits near-duplicates
#: in other words -- exact de-duplication on ``(entry_type, content)`` cannot see
#: those.  Bounded separately from ``_MEMORY_MAX_CHARS`` because this one is paid
#: once per compression round instead of once per request.
_MEMORY_CONTEXT_MAX_CHARS: int = 16_384
#: One entry longer than this is not a memory, it is a pasted tool output; the
#: extraction prompt asks for a self-contained sentence.  Clamping keeps a single
#: runaway entry from consuming the whole ``_MEMORY_MAX_CHARS`` budget.
_MEMORY_ENTRY_MAX_CHARS: int = 2_000
_COMPRESS_BACKOFF_TOKEN_GROWTH: float = 1.25
_COMPRESS_BACKOFF_SECONDS: float = 1800.0
_COMPRESS_BACKOFF_MIN_FAILURES: int = 3
#: Fraction of a model's ``ModelConfig.max_context`` that triggers a mid-loop
#: compression.  The check runs at the END of a completed model round, so a
#: single round can never quite reach the model's window before the trigger
#: fires; acting at 90 % leaves room for the next request (which re-sends a
#: slightly larger prompt) without waiting for the provider to reject it.
_IN_LOOP_THRESHOLD_RATIO: float = 0.9

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

# Fast CJK-aware token estimate.  The implementation lives in runtime.common
# (a single C-level regex pass); the name is kept here because the compression
# code and its tests refer to it.
from runtime.common import (  # noqa: E402
    estimate_llm_tokens as _estimate_tokens_fast,
    env_int,
    get_prompt_token_calibration,
    record_prompt_token_calibration,
    set_prompt_token_calibration,
    sha256_bytes,
)


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


# ---------------------------------------------------------------------------
# Memory budget (entries accumulate; the whole block rides on every request)
# ---------------------------------------------------------------------------

def _memory_entry_limit() -> int:
    """Entries kept per session (``MEMORY_MAX_ENTRIES`` overrides the default)."""
    return max(1, env_int("MEMORY_MAX_ENTRIES", _MEMORY_MAX_ENTRIES))


def _memory_char_limit() -> int:
    """Characters the memory block may cost in one request (``MEMORY_MAX_CHARS``)."""
    return max(256, env_int("MEMORY_MAX_CHARS", _MEMORY_MAX_CHARS))


def _memory_context_char_limit() -> int:
    """Stored memory shown to the extraction prompt (``MEMORY_CONTEXT_MAX_CHARS``).

    ``0`` disables the echo, which restores the old append-blind behaviour.
    """
    return max(0, env_int("MEMORY_CONTEXT_MAX_CHARS", _MEMORY_CONTEXT_MAX_CHARS))


def _memory_entry_char_limit() -> int:
    """Per-entry character clamp (``MEMORY_ENTRY_MAX_CHARS``)."""
    return max(64, env_int("MEMORY_ENTRY_MAX_CHARS", _MEMORY_ENTRY_MAX_CHARS))


def _clamp_memory_content(content: str) -> str:
    """Cut a runaway entry down to sentence size.

    One 30 000-character entry (a pasted tool output) would otherwise consume the
    whole ``_MEMORY_MAX_CHARS`` budget and drop every real memory with it.  The
    marker is part of the entry so the model can see the fact was cut, and the
    full text remains in ``conversation.json`` / the spillover file.
    """
    text = (content or "").strip()
    limit = _memory_entry_char_limit()
    if len(text) <= limit:
        return text
    marker = " …[entry truncated]"
    return text[: max(0, limit - len(marker))].rstrip() + marker


def _memory_entry_cost(entry: "MemoryEntry") -> int:
    """Characters one entry costs in the injected form.

    Matches ``render_memory_entries`` (``## Memory`` header + ``type: content``),
    because the budget has to be charged in the form that is actually sent, not
    the JSON form stored on disk.
    """
    return len(entry.content or "") + len(entry.entry_type or "") + 12


def _trim_memory_entries(entries: list) -> list:
    """Keep the newest entries that fit both the count and the character budget.

    Oldest-first matches how the block is used (recent facts bear on the current
    turn) and how the count cap already trimmed.  The newest entry is kept even
    when it alone exceeds the budget: a session with one memory is not a reason
    to inject none.
    """
    kept = [e for e in entries if not _is_placeholder_memory_entry(e.content)]
    kept.sort(key=lambda e: (str(e.created_at or ""), int(e.source_turn_index or 0)))
    entry_limit = _memory_entry_limit()
    char_limit = _memory_char_limit()
    out: list = []
    total = 0
    for entry in reversed(kept):
        cost = _memory_entry_cost(entry)
        if out and (len(out) >= entry_limit or total + cost > char_limit):
            break
        out.append(entry)
        total += cost
    out.reverse()
    return out


def render_memory_entries(entries: list) -> list[str]:
    """Render *entries* into the ``## Memory`` parts of the system message.

    The injection point enforces the same budget as :func:`_trim_memory_entries`,
    so a ``memory.md`` written before the budget existed cannot smuggle 60 000
    characters into every request of the session.
    """
    return [
        f"## Memory\n{entry.entry_type}: {entry.content}"
        for entry in _trim_memory_entries(entries)
    ]


#: Public spelling for callers outside this module: group_chat injects the same
#: memory block and must pay the same budget.
trim_memory_entries = _trim_memory_entries


def _compression_prompt_budget(
    max_context: int, model_id: str = ""
) -> Optional[int]:
    """Return the prompt token budget for a compression request, or ``None``.

    Compression is designed to succeed in a SINGLE call, so the prompt is
    bounded by the compression model's context window (``max_context``),
    reserving room for the summary + memory answer.  ``max_context <= 0``
    means the window is unknown, in which case the delta is sent in full
    (best effort) and the budget is ``None``.

    The budget is spent in *estimated* tokens: the prompt builder drops the
    oldest delta turns until :func:`estimate_llm_tokens` says the prompt fits.
    That estimate is a guess, and for some tokenizers it is far too small -- a
    196 608-token session measured 179 454 by it and 242 845 by the provider,
    so the "already bounded" compression request was itself rejected with
    ``request exceeds the context window`` and the session could never be
    compressed again.  The model's MEASURED estimate->actual factor (folded in
    by :func:`record_prompt_token_calibration` from every provider reply, and
    by :meth:`ContextManager._learn_compression_overflow` from an overflow
    rejection) therefore divides the budget here, exactly as
    :func:`estimate_chat_prompt_tokens_calibrated` scales the measurement the
    pre-send guard compares against a window.  A factor below ``1.0`` is
    ignored: like the guard, this may only ever make the bound stricter.
    """
    if not max_context or max_context <= 0:
        return None
    budget = max_context - _COMPRESSION_OUTPUT_RESERVE
    if budget <= 0:
        return 0
    factor = get_prompt_token_calibration(model_id)
    if factor > 1.0:
        budget = max(1, int(budget / factor))
    return budget


#: How providers phrase "your request is larger than my context window".  Used
#: to tell an oversized request apart from every other compression failure: only
#: the former carries a usable measurement of our own estimate.
_CONTEXT_OVERFLOW_ERROR_RE = re.compile(
    r"exceeds the (?:available )?context"
    r"|context window"
    r"|maximum context length"
    r"|leaves no room"
    r"|too many tokens",
    re.IGNORECASE,
)

#: Where in that message the provider states the size IT counted, ordered by
#: specificity (first match wins).  Both shapes below were seen in production:
#: ``prompt (242845 tokens) leaves no room to answer in the context (196608)``
#: and ``request (300000 tokens) exceeds the available context size (131072)``.
_PROVIDER_PROMPT_TOKENS_RES: tuple[re.Pattern, ...] = (
    re.compile(r"prompt\s*\(?\s*(\d+)\s*tokens?\)", re.IGNORECASE),
    re.compile(r"request\s*\(\s*(\d+)\s*tokens?\)", re.IGNORECASE),
    re.compile(r"(?:however )?you requested\s+(\d+)\s*tokens?", re.IGNORECASE),
    re.compile(r"requested\s+(\d+)\s+tokens?", re.IGNORECASE),
)


def _context_overflow_tokens(error: object) -> int:
    """Prompt tokens the provider counted for a rejected request (0 = unknown).

    An overflow rejection is a free measurement: the provider just told us how
    many tokens a request of ours really costs.  Returning ``0`` when the
    message carries no number keeps the caller on the old path instead of
    guessing.
    """
    text = error if isinstance(error, str) else str(error or "")
    for pattern in _PROVIDER_PROMPT_TOKENS_RES:
        match = pattern.search(text)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):  # pragma: no cover - regex-guarded
                return 0
    return 0


# ---------------------------------------------------------------------------
# Oversized-content offload (spill to file)
# ---------------------------------------------------------------------------
#
# A single pathological turn (a multi-megabyte tool output, a huge pasted file
# in a user message) used to be silently cut to ``_SUMMARY_TURN_MAX_CHARS``.
# Instead such content is now written to a file and replaced by an excerpt plus
# the file path: the compression model can record that path in the summary, and
# the agent can read the full content back with its file tools.  Nothing is
# lost, only moved out of the context.
#
# Scope guard (deliberate): offloading happens ONLY inside a compression round
# — the trigger is "the context does not fit, so we are compressing".  Content
# is never spilled merely because it is long.

#: Estimated tokens above which a turn is offloaded instead of truncated.
_CONTEXT_SPILL_MIN_TOKENS: int = 4000
#: Characters kept inline (head / tail) next to the file reference.  The
#: replacement therefore stays bounded (~3 KB) whatever the input size.
_CONTEXT_SPILL_HEAD_CHARS: int = 2000
_CONTEXT_SPILL_TAIL_CHARS: int = 1200
#: Newest N offload files are kept per session; older ones are pruned.
_CONTEXT_SPILL_KEEP_FILES: int = 32
#: Marks an already-offloaded message, so a repeated compression round neither
#: re-writes the file nor treats the offloaded original as new content.
SPILL_MARKER: str = "[output offloaded to file:"


def _spill_root() -> str:
    """Directory oversized content is offloaded to (env overridable).

    ``/tmp/agents-context-spill`` by default: ``/tmp`` is accepted by the file
    tools' path validator regardless of the active workspace
    (``builtin_tools_coding._validate_path``), so a path written into the
    context is guaranteed to be readable back by the agent.
    """
    return os.environ.get("CONTEXT_SPILL_DIR") or "/tmp/agents-context-spill"


def is_spilled_content(content: object) -> bool:
    """Return whether *content* is already an offload reference."""
    return SPILL_MARKER in (content if isinstance(content, str) else "")


def _spill_session_dir(session_id: str) -> str:
    """Per-session offload directory, rejecting anything but a plain ID."""
    if (
        not session_id
        or os.sep in session_id
        or (os.altsep and os.altsep in session_id)
        or ".." in session_id
    ):
        raise ValueError(f"Invalid session_id for offload: {session_id!r}")
    return os.path.join(_spill_root(), session_id)


def _prune_spill_dir(directory: str) -> None:
    """Keep only the newest ``_CONTEXT_SPILL_KEEP_FILES`` offload files."""
    if _CONTEXT_SPILL_KEEP_FILES <= 0:
        return
    try:
        names = os.listdir(directory)
    except OSError:
        return
    dated: list[tuple[float, str]] = []
    for name in names:
        path = os.path.join(directory, name)
        try:
            dated.append((os.path.getmtime(path), path))
        except OSError:
            continue
    for _, path in sorted(dated)[:-_CONTEXT_SPILL_KEEP_FILES]:
        try:
            os.unlink(path)
        except OSError:  # pragma: no cover - best effort housekeeping
            logger.warning("context offload: could not prune %s", path)


def spill_content_to_file(
    session_id: str, content: str, label: str = "content"
) -> Optional[str]:
    """Write *content* into the session's offload directory; return its path.

    The file name embeds a digest of the content, so the same content always
    maps to the same file (idempotent across compression rounds).  ``None`` is
    returned when the write fails; the caller then falls back to truncation.
    """
    if not session_id or not content:
        return None
    try:
        directory = _spill_session_dir(session_id)
        digest = sha256_bytes(content.encode("utf-8", errors="replace"))[:16]
        safe_label = "".join(
            ch if ch.isalnum() or ch in "-_" else "_" for ch in (label or "content")
        )[:32]
        path = os.path.join(directory, f"{safe_label}-{digest}.txt")
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(content)
            os.replace(tmp_path, path)
            tmp_path = None
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        _prune_spill_dir(directory)
        return path
    except (OSError, ValueError) as exc:
        logger.warning(
            "context offload: cannot write %s content for session %s: %s",
            label, session_id, exc,
        )
        return None


def spill_reference(session_id: str, content: str, label: str = "content") -> Optional[str]:
    """Bounded replacement for an oversized *content*: excerpt + file path.

    Returns ``None`` when the content is too small to be worth offloading or
    the file could not be written, so the caller keeps its own fallback (head +
    tail truncation).  The returned text is itself bounded, which is what makes
    it useful inside a budget-constrained compression prompt.
    """
    if not content or is_spilled_content(content):
        return content or None
    tokens = _estimate_tokens_fast(content)
    if tokens < _CONTEXT_SPILL_MIN_TOKENS:
        return None
    path = spill_content_to_file(session_id, content, label)
    if not path:
        return None
    head = content[:_CONTEXT_SPILL_HEAD_CHARS]
    tail = content[-_CONTEXT_SPILL_TAIL_CHARS:] if _CONTEXT_SPILL_TAIL_CHARS > 0 else ""
    omitted = len(content) - len(head) - len(tail)
    reference = (
        f"{SPILL_MARKER} {path} ({len(content):,} chars, ~{tokens:,} tokens; "
        f"the full text is still on disk, read it with a file tool)]\n{head}"
    )
    if omitted > 0:
        reference += f"\n[... {omitted:,} characters omitted from the middle ...]\n{tail}"
    logger.info(
        "context offload: session %s %s content of %d chars moved to %s",
        session_id, label, len(content), path,
    )
    return reference


def _truncate_turn_content(
    content: str,
    max_chars: int = _SUMMARY_TURN_MAX_CHARS,
    session_id: str = "",
    label: str = "turn",
) -> str:
    """Cap a single turn's content for the compression prompt.

    An over-long turn is offloaded to a file and replaced by an excerpt plus
    its path (lossless, and the agent can read it back).  Only when that is
    impossible (no session to file under, unwritable directory) is the content
    cut in place, keeping the head (3/4) and the tail (1/4) with a marker that
    documents the omission.
    """
    if max_chars <= 0 or len(content) <= max_chars:
        return content
    if session_id:
        spilled = spill_reference(session_id, content, label)
        if spilled:
            return spilled
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

**Already recorded (de-duplicate here, not later).** When the history above
contains an "Existing memory" list, those entries are ALREADY stored for this
session. Do NOT emit an entry that repeats, rewords or trivially re-derives one
of them - emit only facts that list does not cover. If a listed fact has been
superseded by the new turns, emit the current value once and leave the outdated
one alone. Stored memory is kept under a fixed character budget and the OLDEST
entries are dropped first, so every duplicate costs the session a real fact.

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


def _manifest_workspace(manifest: dict, fallback: str) -> str:
    """Workspace root a journal turn was taken in.

    The manifest is authoritative: it was written by the process that made the
    change, bound to the workspace that request was running in.  *fallback*
    (session meta, or the process default) is only consulted for manifests
    written before the field existed -- reading it first is what sent an
    unrelated-workspace session's revoke into the process default root.
    """
    recorded = str((manifest or {}).get("workspace") or "").strip()
    if not recorded:
        return str(fallback or "")
    return os.path.realpath(recorded)


def _recorded_workspace_in_data(data: dict, timestamp: Optional[str] = None) -> str:
    """Workspace recorded inside a parsed ``conversation.json`` payload.

    Prefers the request-context snapshot on the user turn *timestamp* (a
    session can switch workspace between turns), then the session-level
    ``meta.workspace``.  Returns "" when neither records one.
    """
    if not isinstance(data, dict):
        return ""
    if timestamp:
        for msg in data.get("messages") or []:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            if msg.get("timestamp") != timestamp:
                continue
            turn_meta = msg.get("meta")
            if isinstance(turn_meta, dict) and turn_meta.get("workspace"):
                return str(turn_meta["workspace"])
    meta = data.get("meta")
    if isinstance(meta, dict) and meta.get("workspace"):
        return str(meta["workspace"])
    return ""


def session_recorded_workspace(session_dir: str, timestamp: Optional[str] = None) -> str:
    """Workspace a session recorded for itself, read from its own session dir.

    Used by API paths that never bind a workspace to the request thread (a
    revoke carries no workspace of its own), so the session's on-disk record
    beats the process default.  Returns "" when the session records none.
    """
    conv_path = os.path.join(session_dir, "conversation.json")
    try:
        with open(conv_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    return _recorded_workspace_in_data(data, timestamp)


def _restore_file_plan(
    workspace: str,
    restore_plan: "dict[tuple[str, str], dict]",
    force: bool = False,
) -> dict:
    """Restore every entry of *restore_plan*, which may span several roots.

    *restore_plan* is keyed by ``(workspace_root, path_label)``: one revoke can
    cover turns taken in different workspaces (a session may switch workspace
    mid-session), and the same label under two roots names two different files.
    Each entry's ``"workspace"`` is the root to write to; *workspace* is only
    the fallback for entries built from legacy manifests without one.

    Refuses with ``JournalWorkspaceMismatch`` -- regardless of *force* -- when a
    root the plan needs is not a directory here.  Forcing that would create the
    missing tree and drop the session's files into an unrelated location, and
    the very next revoke would no longer find them.
    """
    needed_roots: list[str] = []
    for (root, rel_path), item in restore_plan.items():
        item_root = str(item.get("workspace") or root or workspace or "")
        if os.path.isabs(_journal_normalize_label(rel_path)):
            # Absolute labels point at the /tmp scratch area, not the root.
            continue
        if item_root and item_root not in needed_roots:
            needed_roots.append(item_root)
    missing_roots = [root for root in needed_roots if not os.path.isdir(root)]
    if missing_roots:
        unreadable = [
            rel_path
            for (root, rel_path), item in restore_plan.items()
            if str(item.get("workspace") or root or workspace or "") in missing_roots
        ]
        return {
            "error": "JournalWorkspaceMismatch",
            "message": (
                "Journal workspace is not available on this server: "
                + ", ".join(missing_roots)
            ),
            "workspaces": missing_roots,
            "checked_workspaces": needed_roots,
            "files": unreadable,
            "can_force": False,
        }

    conflicts: list[str] = []
    for (root, rel_path), item in restore_plan.items():
        item_root = str(item.get("workspace") or root or workspace or "")
        target_path = _journal_resolve_workspace_path(item_root, rel_path)
        if not _current_file_matches(target_path, item["expected_current"]):
            if rel_path not in conflicts:
                conflicts.append(rel_path)
    if conflicts and not force:
        return {
            "error": "JournalConflict",
            "message": "Current files do not match journal after-state",
            "files": conflicts,
            "checked_workspaces": needed_roots,
        }

    restored_files: list[str] = []
    for (root, rel_path), item in restore_plan.items():
        item_root = str(item.get("workspace") or root or workspace or "")
        target_path = _journal_resolve_workspace_path(item_root, rel_path)
        _restore_journal_baseline(target_path, item["baseline"], item_root, item["journal_dir"])
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
    # Anchor to the workspace this turn was journaled in; the caller's
    # *workspace* only backs up manifests that predate the manifest field.
    manifest_ws = _manifest_workspace(manifest, workspace)
    if manifest_ws and workspace and manifest_ws != os.path.realpath(workspace or ""):
        logger.info(
            "undo: journal turn %s was taken in %s, not the caller's %s -- restoring there",
            manifest.get("turn_key"), manifest_ws, workspace,
        )
    restore_plan: "dict[tuple[str, str], dict]" = {}
    for rel_path, entry in manifest.get("files", {}).items():
        if not isinstance(entry, dict) or "baseline" not in entry or "after" not in entry:
            continue
        safe_rel = _journal_normalize_label(entry.get("path") or rel_path)
        restore_plan[(manifest_ws, safe_rel)] = {
            "baseline": entry["baseline"],
            "expected_current": entry["after"],
            "journal_dir": journal_dir,
            "workspace": manifest_ws,
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

    restore_plan: "dict[tuple[str, str], dict]" = {}
    for manifest_path, manifest in selected:
        journal_dir = os.path.dirname(manifest_path)
        # Each turn restores to the workspace it was taken in.  A session can
        # switch workspace mid-session, so one revoke legitimately spans
        # several roots -- and the process default is none of them.
        manifest_ws = _manifest_workspace(manifest, workspace)
        if manifest_ws and workspace and manifest_ws != os.path.realpath(workspace or ""):
            logger.info(
                "revoke: journal turn %s was taken in %s, not the caller's %s -- restoring there",
                manifest.get("turn_key"), manifest_ws, workspace,
            )
        for rel_path, entry in manifest.get("files", {}).items():
            if not isinstance(entry, dict) or "baseline" not in entry or "after" not in entry:
                continue
            safe_rel = _journal_normalize_label(entry.get("path") or rel_path)
            key = (manifest_ws, safe_rel)
            if key not in restore_plan:
                restore_plan[key] = {
                    "baseline": entry["baseline"],
                    "expected_current": entry["after"],
                    "journal_dir": journal_dir,
                    "workspace": manifest_ws,
                }
            else:
                restore_plan[key]["expected_current"] = entry["after"]

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

    #: Fraction of ``MAX_TOKENS_IN_CONTEXT`` at which a fork's copied history is
    #: already considered too large.  Same 90 % point the mid-loop compression
    #: hook leaves for the reply (:meth:`compress_context`), because the guard
    #: measures a history that still has a whole inference in front of it.
    _FORK_GUARD_RATIO: float = 0.9

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
    # Pause sidecar (resume_state.json)
    # ------------------------------------------------------------------
    # A soft pause ends the inference generation at a completed round
    # boundary. conversation.json is already complete at that point, but the
    # loop also carries transient context that is never persisted as such:
    # skill-disclosure frames, the in-flight list rebuilt by in-loop context
    # compression, user messages injected at a boundary. The sidecar snapshots
    # that list so a paused session survives an agentsvc restart with full
    # fidelity: its mere existence is also the durable "paused" marker the
    # server re-seeds into _session_statuses at startup. A resume restores the
    # snapshot and deletes the file immediately; any other new generation (or
    # ending the pause) discards it as stale.

    PAUSE_STATE_FILENAME = "resume_state.json"
    PAUSE_STATE_VERSION = 1

    def pause_state_path(self, session_id: str) -> str:
        return os.path.join(self._chats_dir, session_id, self.PAUSE_STATE_FILENAME)

    def write_pause_state(self, session_id: str, state: dict) -> None:
        """Atomically persist the pause sidecar for *session_id*."""
        path = self.pause_state_path(session_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
            f.flush()
        os.replace(tmp, path)

    def read_pause_state(self, session_id: str) -> Optional[dict]:
        """Return the validated pause sidecar, or None when absent/corrupt.

        A corrupt or mismatched file is removed: it can only come from a
        crashed write or a session-id mix-up, and keeping it would pin the
        session to a "paused" marker whose snapshot cannot be trusted.
        """
        path = self.pause_state_path(session_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            self.clear_pause_state(session_id)
            return None
        if (
            not isinstance(state, dict)
            or state.get("version") != self.PAUSE_STATE_VERSION
            or state.get("session_id") != session_id
            or not isinstance(state.get("context"), list)
        ):
            logging.getLogger(__name__).warning(
                "discarding invalid pause sidecar for session %s", session_id,
            )
            self.clear_pause_state(session_id)
            return None
        return state

    def clear_pause_state(self, session_id: str) -> bool:
        """Delete the pause sidecar if present. Returns True when one was removed."""
        try:
            os.remove(self.pause_state_path(session_id))
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

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
                meta=msg.get("meta") if isinstance(msg.get("meta"), dict) else None,
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
        workspace_hint: str = "",
    ) -> dict:
        """Revoke messages from the conversation starting from the message with the given timestamp.

        Args:
            session_id: Target session.
            timestamp: The timestamp of the user message to revoke from.
            force: Restore even when the current files no longer match the
                journal's after-state (a conflict).  Never overrides a
                workspace that is unavailable here.
            keep_files: Revoke the history without restoring file changes.
            workspace_hint: Optional workspace the client believes the session
                ran in.  Only a fallback: the journal manifest decides, and the
                session's own record outranks a client-supplied value.

        Returns:
            A result dict containing the number of removed messages and the
            git rewrite result for workspace commits associated with the same
            session/timestamp.

        Raises:
            FileNotFoundError: When the conversation file does not exist.
            ValueError: When the file content is malformed JSON or no message found with the given timestamp.
            JournalConflictError: When files conflict with the journal after-state.
            JournalWorkspaceMismatchError: When a journal's workspace root is
                not usable on this server (see :class:`_restore_file_plan`).
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

        # A revoke request carries no workspace and nothing on this path binds
        # one to the thread, so get_workspace() resolves to the *process*
        # default -- which for a session created against another workspace is a
        # different tree, and restoring there writes the session's files into
        # an unrelated project.  Resolve the session's own record first; the
        # journal manifests still win per turn (see revoke_session_file_changes),
        # so this only backs up journals that predate the manifest field.
        session_dir = os.path.dirname(conv_path)
        workspace = (
            _recorded_workspace_in_data(data, timestamp)
            or str(workspace_hint or "").strip()
            or get_workspace()
        )
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
                    checked_workspaces=list(journal_result.get("checked_workspaces") or []),
                )
            if journal_result.get("error") == "JournalWorkspaceMismatch":
                raise JournalWorkspaceMismatchError(
                    journal_result.get("message") or "Journal workspace is not available on this server",
                    list(journal_result.get("workspaces") or []),
                    files=list(journal_result.get("files") or []),
                    checked_workspaces=list(journal_result.get("checked_workspaces") or []),
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
    # Session fork (derive a new session from one assistant message)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        """Reject empty or path-traversal session IDs (mirrors ``SessionManager``)."""
        if (
            not session_id
            or os.sep in session_id
            or (os.altsep and os.altsep in session_id)
            or ".." in session_id
        ):
            raise ValueError(f"Invalid session_id: {session_id}")

    @staticmethod
    def _resolve_fork_cut(messages: list, message_index: object, timestamp: str) -> int:
        """Return the inclusive index of the message a fork is taken from.

        A fork ends on an assistant turn that carries no tool calls — the
        closing message of a reasoning loop — so the new session inherits a
        conversation that can be continued without dangling tool calls.

        The client sends the index it displays the message at (authoritative
        when the client loaded this very ``conversation.json``) together with
        the message timestamp.  Timestamps are only second-precise and repeat
        freely inside one turn, so the timestamp acts as a cross-check first
        and as a fallback only when the index no longer lines up with the file
        (a client list that drifted away from what was persisted).
        """
        def is_loop_end(msg: object) -> bool:
            return (
                isinstance(msg, dict)
                and msg.get("role") == "assistant"
                and not msg.get("tool_calls")
            )

        # ``bool`` is an ``int`` subclass; a JSON ``true`` must not read as 1.
        if isinstance(message_index, int) and not isinstance(message_index, bool):
            if 0 <= message_index < len(messages) and is_loop_end(messages[message_index]):
                candidate = messages[message_index]
                if not timestamp or candidate.get("timestamp") == timestamp:
                    return message_index

        if timestamp:
            # Last match wins: the loop-closing message is the newest one that
            # shares the timestamp, which is what the user clicked.
            for index in range(len(messages) - 1, -1, -1):
                msg = messages[index]
                if is_loop_end(msg) and msg.get("timestamp") == timestamp:
                    return index

        raise ValueError(
            "No forkable assistant message found "
            f"(message_index={message_index!r}, timestamp={timestamp!r})"
        )

    def _allocate_session_id(self) -> str:
        """Return a session ID whose directory does not exist yet.

        ``session_timestamp()`` has one-second resolution, so two forks created
        in the same second would collide; a numeric suffix keeps both.
        """
        base = session_timestamp()
        candidate = base
        attempt = 1
        while os.path.exists(os.path.join(self._chats_dir, candidate)):
            attempt += 1
            if attempt > 100:
                raise OSError(f"Cannot allocate a session id for a fork of {base}")
            candidate = f"{base}_{attempt}"
        return candidate

    def _fork_compression_files(
        self, parent_session_id: str, new_session_id: str, new_turn_count: int
    ) -> None:
        """Copy ``summary.md`` / ``memory.md`` into a fork, dropping the excess.

        Same reconciliation as :meth:`_reconcile_compression_after_revoke`, but
        written to the fork instead of rewriting the parent: a rolling summary
        that covers turns the fork left out would inject context the fork never
        had, so it is dropped altogether rather than trusted.
        """
        summary_text, summary_fm = self.get_summary(parent_session_id)
        summarized_up_to = summary_fm.get("summarized_up_to_turn", -1)
        if not isinstance(summarized_up_to, int):
            summarized_up_to = -1

        if summary_text.strip() and 0 <= summarized_up_to < new_turn_count:
            front_matter = dict(summary_fm)
            front_matter["session_id"] = new_session_id
            front_matter["summarized_up_to_turn"] = min(
                summarized_up_to, new_turn_count - 1
            )
            front_matter["updated_at"] = now_iso()
            self._atomic_write(
                self._summary_path(new_session_id),
                serialize_summary(front_matter, summary_text),
            )
        # Otherwise the fork starts without ``summary.md``: there is none to
        # copy, or the parent's summary describes turns the fork left out.  A
        # rolling summary of history the fork never had would inject context the
        # model cannot ground, so the next compression round rebuilds it from the
        # fork's own turns.

        # Memory is a per-turn record, not a rolling text: entries recorded
        # inside the copied prefix stay valid, entries recorded after the cut
        # are dropped.  Independent of the summary decision above.
        entries = [
            entry
            for entry in self.load_memory(parent_session_id)
            if entry.source_turn_index < new_turn_count
        ]
        if entries:
            self.save_memory(new_session_id, entries)

    def _last_reported_prompt_tokens(self, session_id: str) -> int:
        """Return the provider-reported prompt size of the last stored message.

        ``0`` when no message carries usage stats.  Deliberately reads the last
        *copied* message rather than ``meta.last_total_tokens``: the meta value
        belongs to the parent's newest turn, which a fork may not have copied.

        Args:
            session_id: Session to inspect (a fork, right after it was written).

        Returns:
            The prompt-token count of the newest message that has a ``stat``,
            or ``0`` when none does.
        """
        try:
            turns = self.load_conversation(session_id)
        except (FileNotFoundError, ValueError):
            return 0
        for turn in reversed(turns):
            stat = getattr(turn, "stat", None)
            if not isinstance(stat, dict):
                continue
            for key in ("prompt_tokens", "total_tokens"):
                value = stat.get(key)
                if isinstance(value, int) and value > 0:
                    return value
        return 0

    def _fork_context_guard(self, new_session_id: str) -> dict:
        """Rebuild a fork's ``summary.md`` when its copied history cannot fit.

        :meth:`_fork_compression_files` drops a rolling summary that describes
        turns the cut removed.  With no summary, :meth:`assemble_context` sends
        the WHOLE copied history — and when the cut point sits behind the
        parent's compression point that history is larger than anything the
        parent ever sent, because the parent sent ``summary + recent turns``
        instead.  The fork's very first request then fails with a
        context-length error, and the mid-loop hook (:meth:`compress_context`)
        cannot save it: that only measures a round which already completed.

        So measure what the first request would carry and, if it does not fit,
        generate the summary here — the same call
        ``POST /v1/sessions/{id}/regenerate-summary`` uses.

        Args:
            new_session_id: The fork, already written to disk.

        Returns:
            A JSON-serialisable report: ``estimated_tokens`` / ``budget`` /
            ``compressed`` / ``warning`` (empty when nothing is wrong).  Never
            raises: a fork that could not be compressed is still a valid fork,
            it just carries a warning for the caller to surface.
        """
        budget = int(self._max_tokens_in_context * self._FORK_GUARD_RATIO)
        if budget <= 0:
            return {
                "estimated_tokens": 0,
                "budget": 0,
                "compressed": False,
                "warning": "",
            }

        def _history_tokens(with_reported_floor: bool) -> int:
            # What the first request of the fork would carry, summary included.
            # The local estimate cannot see the runtime system prompt or the
            # tool schemas; the provider-reported size of the last copied
            # message does, so it raises the floor while deciding whether the
            # history is worth compressing.  After compressing, that figure
            # describes the PARENT's request at the cut point, not the fork's
            # new one, so only the local estimate is compared any more.
            estimated = estimate_chat_prompt_tokens(
                self.assemble_context(new_session_id, [])
            )
            if not with_reported_floor:
                return estimated
            return max(estimated, self._last_reported_prompt_tokens(new_session_id))

        measured = _history_tokens(with_reported_floor=True)
        if measured <= budget:
            return {
                "estimated_tokens": measured,
                "budget": budget,
                "compressed": False,
                "warning": "",
            }

        if not self._summary_model_id:
            warning = (
                f"forked history is about {measured} prompt tokens, over the "
                f"{budget}-token budget, and no summary model is configured "
                "(SUMMARY_MODEL_ID): the first inference may be rejected"
            )
            logger.warning("fork_session guard: %s (session=%s)", warning, new_session_id)
            return {
                "estimated_tokens": measured,
                "budget": budget,
                "compressed": False,
                "warning": warning,
            }

        try:
            self.compress_context_forced(new_session_id)
        except Exception as exc:  # the fork stays usable; the warning explains
            logger.exception(
                "fork_session guard: compression of the forked history failed "
                "(session=%s, ~%d tokens)",
                new_session_id, measured,
            )
            warning = (
                f"forked history is about {measured} prompt tokens, over the "
                f"{budget}-token budget, and compressing it failed: {exc}"
            )
            return {
                "estimated_tokens": measured,
                "budget": budget,
                "compressed": False,
                "warning": warning,
            }

        after = _history_tokens(with_reported_floor=False)
        summary_text = self.get_summary(new_session_id)[0]
        # A placeholder summary is the house definition of "the model produced
        # nothing": it is the compression prompt's own format example leaking
        # back into summary.md.
        if not summary_text.strip() or _is_placeholder_summary(summary_text):
            warning = (
                f"forked history is about {measured} prompt tokens, over the "
                f"{budget}-token budget, but the summary model returned nothing"
            )
            logger.warning(
                "fork_session guard: %s (session=%s)", warning, new_session_id
            )
            return {
                "estimated_tokens": after,
                "budget": budget,
                "compressed": False,
                "warning": warning,
            }

        warning = ""
        if after > budget:
            # Compression ran, but the fork is still heavy: the summary covers
            # fewer turns than the history holds (recent turns stay verbatim).
            # The real ceiling is the model's window, which ContextManager does
            # not know, so this is reported rather than treated as a failure.
            warning = (
                f"the fork was compressed but still carries about {after} "
                f"prompt tokens, over the {budget}-token budget; its first "
                "inference may still be rejected"
            )
            logger.warning("fork_session guard: %s", warning)

        logger.info(
            "fork_session guard: %s history ~%d tokens > %d budget; rebuilt "
            "summary.md, now ~%d tokens",
            new_session_id, measured, budget, after,
        )
        return {
            "estimated_tokens": after,
            "budget": budget,
            "compressed": True,
            "warning": warning,
        }

    def fork_session(
        self,
        session_id: str,
        message_index: Optional[int] = None,
        timestamp: str = "",
    ) -> dict:
        """Create a new session from this conversation's first *message_index* turns.

        Copies ``conversation.json`` truncated to include the anchored message,
        plus the reconciled ``summary.md`` / ``memory.md`` (the files that make
        the copied history usable on its own).  Request dumps, file journals and
        sub-session directories stay with the parent: a fork is a new branch of
        the conversation, not a duplicate of everything the parent produced.

        When the copied history does not fit the context the fork will run in —
        possible because a summary covering turns the cut removed is dropped —
        :meth:`_fork_context_guard` rebuilds ``summary.md`` before the fork is
        handed back, so its first inference is not the one that discovers the
        ceiling.

        Args:
            session_id: Parent session to fork.
            message_index: Index of the anchoring assistant message in
                ``conversation.json``'s ``messages`` array (inclusive).
            timestamp: Timestamp of that message; used to cross-check the index
                and as a fallback when the index cannot be verified.

        Returns:
            ``{"session_id", "parent_session_id", "cut_index",
            "message_count", "first_user_message", "context_guard"}``.
            ``context_guard`` reports what :meth:`_fork_context_guard` measured
            and whether it had to rebuild ``summary.md`` for the fork.

        Raises:
            FileNotFoundError: The parent conversation file does not exist.
            ValueError: Malformed input, malformed conversation, or no
                forkable assistant message at the given anchor.
            OSError: The fork directory could not be created or written.
        """
        import shutil

        self._validate_session_id(session_id)
        conv_path = self._conversation_path(session_id)
        if not os.path.isfile(conv_path):
            raise FileNotFoundError(f"Conversation file not found: {conv_path}")

        try:
            with open(conv_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"conversation.json is malformed: {exc}") from exc
        except OSError as exc:
            raise ValueError(f"Cannot read conversation.json: {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError(
                "conversation.json must contain a JSON object, got "
                f"{type(data).__name__}"
            )
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError(f"Session has no messages to fork: {session_id}")

        cut_index = self._resolve_fork_cut(messages, message_index, timestamp)
        new_messages = messages[: cut_index + 1]

        new_session_id = self._allocate_session_id()
        new_dir = os.path.join(self._chats_dir, new_session_id)
        os.makedirs(new_dir)

        try:
            now = now_iso()
            meta = data.get("meta")
            meta = dict(meta) if isinstance(meta, dict) else {}
            # ``last_total_tokens`` and the session bindings (``model_id``,
            # ``tool_ids``, ``agent_ids``, ``remote_env``, ``workspace``) are
            # inherited on purpose: the fork continues under the same setup.
            meta.update({
                "session_id": new_session_id,
                "created_at": now,
                "updated_at": now,
                "turn_count": len(new_messages),
                "parent_session_id": session_id,
                "forked_from_message_index": cut_index,
            })
            self._atomic_write(
                os.path.join(new_dir, "conversation.json"),
                json.dumps(
                    {"meta": meta, "messages": new_messages},
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            self._fork_compression_files(session_id, new_session_id, len(new_messages))
        except Exception:
            # Never leave a half-written session behind: the caller reports the
            # failure and the client keeps using the parent session.
            shutil.rmtree(new_dir, ignore_errors=True)
            raise

        first_user_message = ""
        for msg in new_messages:
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    first_user_message = content.strip()
                break

        logger.info(
            "fork_session: %s -> %s (kept %d/%d messages, cut at %d)",
            session_id, new_session_id, len(new_messages), len(messages), cut_index,
        )
        # Outside the rollback block on purpose: a fork whose history needs
        # compressing is still a fork, even if that compression fails.
        context_guard = self._fork_context_guard(new_session_id)
        return {
            "session_id": new_session_id,
            "parent_session_id": session_id,
            "cut_index": cut_index,
            "message_count": len(new_messages),
            "first_user_message": first_user_message,
            "context_guard": context_guard,
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

        The stored list is put through :func:`_trim_memory_entries` first, so the
        file on disk cannot outgrow the budget that is charged on every later
        request no matter which caller writes it (compression, import, a manual
        edit through the API).
        """
        now = now_iso()
        entries = _trim_memory_entries(entries)
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

    def _learn_compression_overflow(
        self, session_id: str, model_id: str, window: int, prompt: str,
        error: object,
    ) -> bool:
        """Fold an overflow rejection back into the model's estimate factor.

        A compression request that the provider rejects as oversized is a
        measurement, not just a failure: the provider names the number of prompt
        tokens it counted for a request whose size WE measured with the same
        estimator the prompt builder spends its budget in.  Recording that ratio
        is what makes the NEXT compression attempt (and every pre-send guard
        measurement of this model) bounded by the real window instead of the
        guess -- see :meth:`_compression_prompt_budget`.

        Only overflow rejections are treated this way; any other failure says
        nothing about tokenisation, and a rejection whose message carries no
        number is logged as such rather than guessed at.

        Returns True when a factor was recorded.
        """
        if not model_id or not _CONTEXT_OVERFLOW_ERROR_RE.search(
            error if isinstance(error, str) else str(error or "")
        ):
            return False
        actual = _context_overflow_tokens(error)
        measured = _estimate_tokens_fast(prompt)
        if actual <= 0 or measured <= 0:
            logging.warning(
                "compress_context: session %s was rejected as oversized by model "
                "%s, but the rejection names no token count (~%d estimated here); "
                "the next attempt repeats the current budget",
                session_id, model_id, measured,
            )
            return False
        factor = record_prompt_token_calibration(model_id, measured, actual)
        if factor is None:
            logging.warning(
                "compress_context: session %s overflow measurement for model %s "
                "(~%d estimated here vs %d counted) is outside the credible band; "
                "not folding it in",
                session_id, model_id, measured, actual,
            )
            return False
        # This sample is not a report about some other request: the provider
        # counted the very prompt we just built and measured here.  Smoothing it
        # into an EWMA would take several doomed attempts to converge (0.4 weight
        # on a 1.35 ratio needs four), so the floor is raised to the measurement
        # itself -- which can only ever make a later "may I send this?" answer
        # stricter, the one direction calibration is allowed to move in.
        ratio = actual / measured
        if ratio > factor:
            set_prompt_token_calibration(model_id, ratio)
            factor = ratio
        logging.warning(
            "compress_context: session %s compression request rejected as "
            "oversized (model=%s: we estimated ~%d, the provider counted %d -> "
            "factor %.2f); its next attempt is bounded to about %d estimated "
            "tokens",
            session_id, model_id, measured, actual, factor,
            _compression_prompt_budget(window, model_id) or 0,
        )
        return True

    def _merge_memory_entries(
        self, session_id: str, new_entries: list[MemoryEntry]
    ) -> list[MemoryEntry]:
        """Merge *new_entries* with the session's existing memory entries.

        Persisting the new entries alone would wipe everything extracted
        earlier, so they are merged into what is on disk.  Entries are
        de-duplicated by ``(entry_type, content)``, and the result is capped to
        ``_memory_entry_limit()`` entries AND ``_memory_char_limit()`` rendered
        characters (oldest first) -- see :func:`_trim_memory_entries`.

        Exact de-duplication cannot catch the same fact reworded, which is why
        the compression prompt is also shown the existing entries
        (:meth:`_existing_memory_block`); the cap here is the backstop.
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
        merged = _trim_memory_entries(existing)
        if len(merged) < len(existing):
            logging.info(
                "compress_context: session %s memory trimmed %d -> %d entries "
                "(limits: %d entries / %d chars); the dropped facts remain in "
                "conversation.json",
                session_id, len(existing), len(merged),
                _memory_entry_limit(), _memory_char_limit(),
            )
        return merged

    def _existing_memory_block(
        self, session_id: str, budget_tokens: Optional[int] = None
    ) -> str:
        """Render the session's stored memory for the extraction prompt.

        Without this the model has no idea what is already in ``memory.md``, so
        every compression round re-emits the same facts in other words -- exact
        de-duplication in :meth:`_merge_memory_entries` cannot see those, and the
        file (and every request that carries it) grows.  Showing it is also what
        lets the model *supersede* instead of contradict: an entry the model can
        see is an entry it can refine.

        Compact one-line form, newest suffix: the same entries that survive into
        a request are the ones the model is shown, so the echo stays useful even
        when the file is longer than the echo budget.
        """
        limit = _memory_context_char_limit()
        if not session_id or limit <= 0:
            return ""
        entries = _trim_memory_entries(self.get_memory_entries(session_id))
        if not entries:
            return ""
        if budget_tokens is not None:
            # A token share expressed in characters: 2 chars/token is the safe
            # side for the mixed CJK/ASCII text these sessions contain, and the
            # echo must not crowd the delta out of the compression prompt.
            limit = min(limit, max(512, int(budget_tokens) * 2))
        lines = [f"- [{e.entry_type}] {e.content}" for e in entries]
        kept: list[str] = []
        total = 0
        omitted = 0
        for line in reversed(lines):
            cost = len(line) + 1
            if kept and total + cost > limit:
                omitted = len(lines) - len(kept)
                break
            kept.append(line)
            total += cost
        kept.reverse()
        if omitted:
            kept.insert(
                0,
                f"- [... {omitted} older entr(ies) not shown: echo budget of "
                f"{limit} characters ...]",
            )
        return "\n".join(kept)

    def _build_compression_prompt(
        self,
        delta: list[ConversationTurn],
        delta_start: int,
        previous_summary: str,
        budget_tokens: Optional[int],
        current_ts: str,
        session_id: str = "",
        previous_memory: str = "",
    ) -> str:
        """Build the compression prompt for the delta.

        ``delta`` holds the turns NOT yet covered by ``previous_summary``;
        ``delta[i]`` is global turn index ``delta_start + i``.

        ``session_id`` enables offloading: a turn too large for the prompt is
        written to a file and replaced by an excerpt plus its path, so the
        summary can point at the full text instead of summarising a silent
        truncation.  Without it (or when the write fails) an over-long turn is
        truncated head/tail as before.

        ``budget_tokens=None`` (the normal case): the FULL delta is sent —
        only pathological single turns are capped at
        ``_SUMMARY_TURN_MAX_CHARS`` chars and the previous summary at
        ``_PREV_SUMMARY_MAX_TOKENS`` tokens.  ``budget_tokens`` as an int
        (overflow-retry case): the whole prompt is additionally bounded to
        that many tokens — the previous summary is capped at 1/4 of the
        budget, then the OLDEST turns are dropped (a marker documents the
        loss), and as a last resort the newest turn is offloaded or, failing
        that, hard-truncated.
        """
        overhead = (
            _estimate_tokens_fast(_COMPRESSION_PROMPT_HEAD)
            + _estimate_tokens_fast(_COMPRESSION_PROMPT_TAIL)
            + 128  # history section headers + omission marker
        )
        if previous_memory:
            # The memory echo competes for the same budget as the turns.  It is
            # bought deliberately: without it the model re-emits facts already on
            # disk in other words, and every later request pays for both copies.
            overhead += _estimate_tokens_fast(previous_memory) + 24

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
            content = _truncate_turn_content(
                t.content, session_id=session_id, label=(t.name or t.role or "turn"),
            )
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
                # offloaded to a file when it is oversized and only then
                # hard-truncated to what is left of the budget.
                if turns_budget > 0:
                    newest_index = delta_start + len(delta) - 1
                    newest = delta[-1].content
                    if session_id:
                        newest = (
                            spill_reference(
                                session_id, newest,
                                label=(delta[-1].name or delta[-1].role or "turn"),
                            )
                            or newest
                        )
                    rendered = [
                        f"Turn {newest_index} "
                        f"{_turn_compression_label(delta[-1])}: "
                        + _truncate_to_token_budget(newest, turns_budget)
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
        leads: list[str] = []
        if prev:
            leads.append(f"## Previous summary\n{prev}")
        if previous_memory:
            leads.append(
                "## Existing memory (ALREADY STORED - emit only facts that are "
                f"NOT already covered by this list)\n{previous_memory}"
            )
        if leads:
            history_section = (
                "\n\n".join(leads)
                + f"\n\n## New turns to incorporate\n{turns_text}"
            )
        else:
            history_section = f"## Conversation turns\n{turns_text}"

        prompt = _COMPRESSION_PROMPT_HEAD + history_section + _COMPRESSION_PROMPT_TAIL
        return prompt.replace("{current_ts}", current_ts)

    def _select_compression_model(
        self,
        inference_model_id: str = "",
        inference_max_context: int = 0,
    ) -> tuple[str, int]:
        """Choose the model + effective window for a compression call.

        Returns ``(model_id, window)`` where ``window`` is the context window to
        bound the compression prompt to (``0`` = unknown, so the delta is sent in
        full).  Selection rules:

        - The configured summary model (``SUMMARY_MODEL_ID``; default
          ``"summary"``) is used when it is available and its window is at
          least the inference model's.
        - The compression switches to the INFERENCE model when the summary
          model is unset/unregistered, OR when both windows are known and the
          summary model's is smaller than the inference model's.  The inference
          model's window is provably sufficient (the mid-loop trigger only fires
          at 90 % of it), so a prompt bounded by it always fits — the
          one-shot-success guarantee.
        - When neither a summary model nor an inference model is available,
          compression stays disabled: ``("", 0)`` is returned.

        The ``window`` is the smallest known window among the two candidates, so
        the prompt never exceeds a window the endpoint is known to honour.
        """
        summary_raw = self._summary_model_id  # resolved (or "" when disabled)
        summary_cfg = None
        if summary_raw and self._model_registry is not None:
            summary_cfg = self._model_registry.get(summary_raw)
        summary_win = int((summary_cfg.max_context or 0) if summary_cfg else 0)

        inference_cfg = None
        if inference_model_id and self._model_registry is not None:
            inference_cfg = self._model_registry.get(inference_model_id)
        inference_win = int(inference_max_context or 0)

        has_summary = summary_cfg is not None or (
            summary_raw is not None and summary_raw and self._model_registry is None
        )
        has_inference = inference_cfg is not None

        # Pick the compression model:
        #  - summary model, when it is available and NOT provably smaller than
        #    the inference model;
        #  - otherwise the inference model (unset summary, or summary smaller).
        switch_to_inference = (
            has_inference
            and (
                not has_summary
                or (summary_win > 0 and inference_win > 0 and summary_win < inference_win)
            )
        )
        if switch_to_inference:
            model_id = inference_cfg.model_id
            model_win = inference_win
        elif has_summary:
            model_id = summary_cfg.model_id if summary_cfg is not None else summary_raw
            model_win = summary_win
        elif has_inference:
            model_id = inference_cfg.model_id
            model_win = inference_win
        else:
            return ("", 0)

        # Bound the prompt by the smallest known window among the two
        # candidates (a 0 window means "unknown" and does not constrain).
        known = [w for w in (model_win, inference_win) if w > 0]
        window = min(known) if known else 0
        return (model_id, window)

    def compress_context(
        self,
        session_id: str,
        turns: list[ConversationTurn],
        last_total_tokens: Optional[int] = None,
        forced: bool = False,
        trigger_threshold: Optional[int] = None,
        inference_model_id: str = "",
        inference_max_context: int = 0,
        deep: bool = False,
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

        The compression is designed to succeed in a SINGLE call.  The prompt is
        therefore bounded UP FRONT by the compression model's context window
        (``ModelConfig.max_context``): the budget is the model's window minus a
        reserve for the summary + memory answer, and the OLDEST delta turns are
        dropped (a marker documents the loss) to stay inside it.  Pathological
        single turns are still capped at ``_SUMMARY_TURN_MAX_CHARS`` chars.
        When no window is known for any candidate model, the delta is sent in
        full (best effort, as before).

        Model selection (see :meth:`_select_compression_model`): the configured
        summary model is used when it is available and its window is at least
        the inference model's; otherwise the compression runs on the inference
        model itself, whose window is provably sufficient because the mid-loop
        trigger only fires at 90 % of it.  If neither a summary model nor an
        inference model is known, compression is disabled.

        An ACCEPTED request that still comes back with an EMPTY answer having
        used NO completion tokens is a transient provider blip and the SAME
        (already window-bounded) prompt is resent up to
        ``_MAX_EMPTY_OUTPUT_RETRIES`` times.

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
            trigger_threshold: Optional override for the token threshold used by
                the trigger check above.  The mid-loop trigger
                (:meth:`compress_context_in_loop`) passes 90 % of the model's
                ``ModelConfig.max_context`` so a running inference is compressed
                even when ``MAX_TOKENS_IN_CONTEXT`` is configured larger.
            inference_model_id: ``model_id`` of the model running the current
                inference.  Used as the compression model when the summary model
                is unset or has a smaller context window (see
                :meth:`_select_compression_model`).
            inference_max_context: Context window (``ModelConfig.max_context``)
                of the inference model in tokens; ``0`` means unknown.  When the
                inference model is chosen for compression it bounds the prompt,
                and it also caps the prompt of a (smaller) summary model so the
                request never exceeds a window the endpoint is known to honour.
            deep: Summarize everything except the last turn instead of keeping
                the most recent K turns verbatim.  Used by the pre-send guard
                when the unsummarized recent-K window itself no longer fits the
                model's window: the alternative is to drop those turns without
                any summary, so summarising them is the lossless-first option
                (oversized turns among them are offloaded to a file, and the
                in-flight round is pulled back verbatim by
                :meth:`assemble_context`).
        """
        # Choose which model performs the compression and the window to bound
        # the prompt to, so the request succeeds in ONE call (see the docstring
        # and :meth:`_select_compression_model`).  Compression is disabled only
        # when neither a summary model nor an inference model is available.
        model_id, window = self._select_compression_model(
            inference_model_id, inference_max_context
        )
        if not model_id:
            return

        # Resolve last_total_tokens
        effective_tokens = last_total_tokens
        if effective_tokens is None:
            effective_tokens = self.get_last_total_tokens(session_id)
        if effective_tokens is None:
            return  # no token data available yet
        threshold = (
            self._max_tokens_in_context
            if trigger_threshold is None
            else trigger_threshold
        )
        if not forced and effective_tokens <= threshold:
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
        if deep:
            # The pre-send guard reports that even the unsummarized recent-K
            # window does not fit the model's window.  Compress deeper: every
            # turn except the last one goes into the summary.  The in-flight
            # round is not orphaned by this: assemble_context's tool-chain
            # backtracking pulls the pending assistant(tool_calls) turn back in
            # verbatim (see its tool-chain integrity checks).
            summarized_up_to = len(turns) - 2
        elif len(turns) > k:
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
        # Bound the prompt by the compression model's context window so the
        # request succeeds in ONE call.  ``window == 0`` means no window is
        # known for any candidate, in which case the budget is ``None`` and the
        # delta is sent in full (best effort, as before).
        budget = _compression_prompt_budget(window, model_id)
        # Show the model what is already recorded, so Task 2 emits facts instead
        # of restatements.  Echoed BEFORE the turns are budgeted so its share is
        # accounted for in the same pass (see _build_compression_prompt).
        existing_memory = self._existing_memory_block(session_id, budget)
        prompt = self._build_compression_prompt(
            delta, delta_start, previous_summary, budget, current_ts,
            session_id=session_id,
            previous_memory=existing_memory,
        )
        if budget is not None:
            factor = get_prompt_token_calibration(model_id)
            logging.info(
                "compress_context: session %s compressing with model=%s window=%d "
                "prompt budget=%d delta turns=%d memory echo=%d chars%s",
                session_id, model_id, window, budget, len(delta),
                len(existing_memory),
                (
                    f" (prompt estimate calibrated x{factor:.2f} for this model)"
                    if factor > 1.0 else ""
                ),
            )

        error = ""
        raw_output = ""
        success = False
        empty_retries = 0
        while True:
            result = None
            try:
                from runtime.models import InferenceRequest, Message as _Message  # local import to avoid circular deps
                infer_request = InferenceRequest(
                    model_id=model_id,
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
                # The provider ACCEPTED the request but returned an empty answer.
                # An empty answer that used NO completion tokens is a transient
                # provider blip — resend the SAME (already window-bounded)
                # prompt a couple of times.  (Because the prompt is bounded to
                # leave room for the answer, the "thinking model exhausted the
                # completion budget" case no longer needs a shrink-retry.)
                stat = getattr(result, "stat", None) if result is not None else None
                used_completion = int(getattr(stat, "completion_tokens", 0) or 0)
                if used_completion == 0 and empty_retries < _MAX_EMPTY_OUTPUT_RETRIES:
                    empty_retries += 1
                    logging.warning(
                        "compress_context: empty model output for session %s "
                        "(no completion tokens); resending the same prompt "
                        "(%d/%d)",
                        session_id, empty_retries, _MAX_EMPTY_OUTPUT_RETRIES,
                    )
                    continue
                error = "empty model output"

            break

        if not success:
            # An oversized rejection is worth more than a warning: it carries
            # the provider's own count of the request we just built, which is
            # the only trustworthy measurement of how this model tokenises the
            # compression prompt.  Fold it in so the NEXT attempt is bounded by
            # the real window (see _learn_compression_overflow).
            self._learn_compression_overflow(
                session_id, model_id, window, prompt, error
            )
            logging.warning(
                "compress_context: LLM call failed for session %s (model=%s; %s); "
                "summary.md and memory.md left unchanged",
                session_id, model_id, error or "unknown error",
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
                    content = _clamp_memory_content(str(item.get("content") or ""))
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

    def compress_context_in_loop(
        self,
        session_id: str,
        turns: list[ConversationTurn],
        round_total_tokens: Optional[int],
        max_context: int = 0,
        inference_model_id: str = "",
    ) -> bool:
        """Mid-loop compression trigger driven by the model's context window.

        The inference tool loop re-sends the entire message list on every round,
        so a single HTTP inference can grow past the model's context window long
        before the session-completed trigger (``MAX_TOKENS_IN_CONTEXT``) runs.
        ``Runtime.infer_stream`` therefore calls this after every completed tool
        round with that round's input + output tokens and the
        ``ModelConfig.max_context`` of the model running the loop.

        When the round reached ``max_context * 0.9`` the regular incremental
        compression runs — same prompt, same retry/backoff/failure semantics as
        the session-completed trigger — and the caller is expected to rebuild the
        in-flight context from the freshly written summary/memory (see
        :class:`runtime.server_state.InLoopContextCompressor`).

        Args:
            session_id: Target session.
            turns: Current full list of conversation turns (as persisted).
            round_total_tokens: Prompt + completion tokens of the completed
                round.
            max_context: Context window of the model used by this loop, in
                tokens (``ModelConfig.max_context``).  ``0`` means the window is
                unknown, which SKIPS the in-loop check entirely — the round is
                never rejected client-side, and a wrong guess (too small) would
                compress healthy sessions needlessly.

        Returns:
            ``True`` when the round reached the threshold, ``False`` when the
            check is disabled or the round is still below it (nothing is sent to
            the summary model in that case).  ``True`` tells the caller to
            rebuild the context from the current summary/memory: a session in
            failure backoff still returns ``True`` while skipping the LLM call,
            and the rebuild then simply re-derives the previous state.
        """
        if not max_context or max_context <= 0:
            return False  # model window unknown: no in-loop check
        threshold = int(max_context * _IN_LOOP_THRESHOLD_RATIO)
        if round_total_tokens is None or round_total_tokens <= threshold:
            return False
        logging.info(
            "compress_context_in_loop: session %s round used %d tokens, at or "
            "above %d (%d%% of max_context=%d); compressing mid-loop",
            session_id,
            round_total_tokens,
            threshold,
            int(_IN_LOOP_THRESHOLD_RATIO * 100),
            max_context,
        )
        # The threshold is passed through explicitly so a larger
        # MAX_TOKENS_IN_CONTEXT can never suppress the in-loop trigger.  The
        # failure backoff still applies: a repeatedly failing compression is not
        # retried on every round.
        self.compress_context(
            session_id,
            turns,
            last_total_tokens=round_total_tokens,
            trigger_threshold=threshold,
            inference_model_id=inference_model_id,
            inference_max_context=max_context,
        )
        return True

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

    @staticmethod
    def _turn_role(turn: object) -> str:
        """Role of a turn that may be a ``Turn`` or a plain dict."""
        if isinstance(turn, dict):
            return str(turn.get("role") or "")
        return str(getattr(turn, "role", "") or "")

    @staticmethod
    def _turn_tool_calls(turn: object) -> list:
        """``tool_calls`` of a turn that may be a ``Turn`` or a plain dict."""
        if isinstance(turn, dict):
            return turn.get("tool_calls") or []
        return getattr(turn, "tool_calls", None) or []

    def select_unsummarized_turns(self, turns: list, summary_fm: Optional[dict]) -> list:
        """The verbatim tail of *turns* a compressed session still sends.

        This is the single authority for the "recent K turns" window, used by
        :meth:`assemble_context` (one session, one model) AND by group chat (N
        agents over one session).  Group chat used to carry its own copy of the
        arithmetic, and that copy caused a real
        ``request exceeds the context window: prompt (471777 tokens) leaves no
        room to answer``: the agent had run 94 tool rounds with no new user turn,
        so its "walk back to the start of this turn" loop walked from turn 1371
        to turn 1285 -- 96 turns / 711 996 characters, 84 of them already inside
        the summary it was also sending.

        Rules, in order:

        1. ``floor = summarized_up_to_turn + 1``: every turn at or below the
           floor is covered by the summary and must never be replayed verbatim;
        2. the window starts at ``max(len(turns) - K, floor)``;
        3. protocol integrity: a window may never *begin* with a tool result
           whose ``assistant(tool_use)`` sits outside it, so either pull that
           assistant in (the only sanctioned exception to rule 1) or drop the
           orphan tool results;
        4. walking back to the turn's first ``user``/``system`` frame stops at the
           floor, and rule 3 is re-checked after the walk;
        5. a system turn at the head of the window is skipped -- it is already
           merged into the single system message the caller builds.

        Args:
            turns: Conversation turns (``Turn`` objects, or dicts of the same
                shape -- group chat hands over round-2+ turns it built itself).
            summary_fm: Front-matter of ``summary.md`` (:meth:`get_summary`).

        Returns:
            The slice of *turns* that stays verbatim.  Never longer than the
            turns list.
        """
        if not turns:
            return []

        fm = summary_fm or {}
        summarized_up_to = fm.get("summarized_up_to_turn", -1)
        if not isinstance(summarized_up_to, int) or isinstance(summarized_up_to, bool):
            summarized_up_to = -1
        summarized_up_to = min(summarized_up_to, len(turns) - 1)
        floor = max(0, summarized_up_to + 1)

        k = self._recent_turns_k
        start = max(max(0, len(turns) - k), floor)
        start = min(start, len(turns))

        def _matching_assistant(cursor: int) -> int:
            """Index of the assistant(tool_use) that owns the tool run at *cursor*."""
            cursor -= 1
            while cursor >= 0 and self._turn_role(turns[cursor]) == "tool":
                cursor -= 1
            if cursor >= 0 and self._turn_role(turns[cursor]) == "assistant" \
                    and self._turn_tool_calls(turns[cursor]):
                return cursor
            return -1

        # Rule 3 (before the walk back): start of the window is a tool result.
        may_precede_floor = False
        if 0 < start < len(turns) and self._turn_role(turns[start]) == "tool":
            owner = _matching_assistant(start)
            if owner >= 0:
                start = owner
                may_precede_floor = start < floor
            else:
                while start < len(turns) and self._turn_role(turns[start]) == "tool":
                    start += 1

        # Rule 4: take the whole turn, but never past what the summary covers.
        while (floor < start < len(turns)
               and self._turn_role(turns[start]) not in ("user", "system")):
            start -= 1

        # Rule 3 again: the walk may have landed on a tool result of a round
        # whose assistant is still outside the window -- an orphan tool_result is
        # rejected outright by providers such as Anthropic ("No tool call found
        # for function call output").
        if 0 < start < len(turns) and self._turn_role(turns[start]) == "tool":
            owner = _matching_assistant(start)
            if owner >= 0:
                start = owner
                may_precede_floor = True
            else:
                while start < len(turns) and self._turn_role(turns[start]) == "tool":
                    start += 1

        if start < floor and not may_precede_floor:
            start = floor
        if start < len(turns) and self._turn_role(turns[start]) == "system":
            start += 1                       # rule 5

        return turns[start:]


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
            turn_msgs: list[dict] = [turn_to_message_dict(t) for t in turns]
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
                msg = turn_to_message_dict(t)
                text = self._system_message_text(msg)
                if text.strip():
                    system_parts.append(text.strip())

        # 2. Structured memory entries (dropped oldest-first when over budget).
        #    The budget is charged here as well as at write time, so a memory.md
        #    written before the budget existed cannot keep riding on every
        #    request (one real session injected 32 743 characters ~= 10 600
        #    estimated tokens of memory per request).
        memory_entries = self.get_memory_entries(session_id)
        memory_parts: list[str] = render_memory_entries(memory_entries)

        # 3. Recent/unsummarized turns.
        recent_turns = self.select_unsummarized_turns(turns, summary_fm)
        turn_msgs = [
            turn_to_message_dict(t)
            for t in recent_turns
            if t.role != "system"
        ]


        # 3b. User-anchor guarantee.  Several providers (Ollama and some
        #     OpenAI-compatible servers) reject a request whose messages contain
        #     no user message at all ("no user query found in messages").  A
        #     normal new turn appends the fresh user message, but a mid-loop
        #     rebuild (``assemble_context(session_id, [])``) or a Continue
        #     re-send can be left with a window that is nothing but the tail of
        #     a long tool loop: the user message of the in-progress round sits
        #     far outside the K-message window.  In that case prepend the last
        #     real user turn so the tool chain stays anchored to the question
        #     it is answering.
        if not any(
            m.get("role") == "user" for m in list(turn_msgs) + list(new_messages)
        ):
            last_user_idx = max(
                (i for i, t in enumerate(turns) if t.role == "user"),
                default=-1,
            )
            if last_user_idx >= 0:
                turn_msgs = [turn_to_message_dict(turns[last_user_idx])] + turn_msgs
                logger.info(
                    "assemble_context(session=%s): window has no user message; "
                    "anchored the context to the last user turn (index %d)",
                    session_id,
                    last_user_idx,
                )

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
