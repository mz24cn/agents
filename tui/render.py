"""Transcript / Block rendering model for the TUI (contract §4.2 / §5).

Turns SSE frames (and persisted conversations) into display blocks, and
implements the compact tool badge.

The badge logic (``compact_tool_display`` and its helpers) is a line-for-line
port of ``web/src/lib/compact-tool.js`` — the acceptance baseline is the
pytest port of ``web/src/lib/compact-tool.test.js`` (see
``tests/test_tui_render.py``).

All ``Block.lines`` are **unwrapped** logical lines: wrapping to the terminal
width is the ui layer's job (contract §4.2).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "Block",
    "Transcript",
    "compact_tool_display",
    "parse_tool_args",
]


# ===========================================================================
# Compact tool badge (port of web/src/lib/compact-tool.js)
# ===========================================================================

DEFAULT_ICON = "\U0001f6e0\ufe0f"  # 🛠️

TOOL_ICONS = {
    "read_file": "\U0001f440",    # 👁
    "write_file": "\U0001f4be",   # ✉
    "edit_file": "\u270d\ufe0f",  # ✍️
    "search_code": "\U0001f50e",  # 🔎
    "exec_shell": "\U0001f527",   # 🔧
    "exec_cli": "\U0001f4bb",     # 💻
}

FILE_TOOLS = frozenset(["read_file", "write_file", "edit_file"])

# Statements that only adjust shell state or print literals carry no
# intuitive meaning on their own; they are skipped while hunting for the
# first substantive statement of a compound command.
NOISE_COMMANDS = frozenset([
    "cd", "echo", "pwd", "set", "export", "unset", "source", "true", "clear", ":",
])

MAX_LABEL_LENGTH = 40

# A `VAR=value` token, possibly prefixed before the real command.
_ASSIGNMENT_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# JS /\w/u is ASCII-only — keep the summary stop test identical.
_WORD_RE = re.compile(r"[A-Za-z0-9_]")


def parse_tool_args(tc: "dict | None") -> dict:
    """Tool call arguments arrive either as a JSON string (OpenAI-style stream
    deltas) or as a plain object (persisted conversations).

    Returns a dict; missing / partial / invalid / non-object arguments yield
    ``{}``.
    """
    raw = tc.get("arguments") if isinstance(tc, dict) else None
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    text = raw.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        # Partial arguments are common while a call is still streaming.
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _collapse_whitespace(text) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def _truncate_label(text) -> str:
    value = _collapse_whitespace(text)
    if len(value) <= MAX_LABEL_LENGTH:
        return value
    return value[: MAX_LABEL_LENGTH - 1] + "\u2026"


def _basename_of_path(path) -> str:
    """File name of a POSIX or Windows path, without its directory part."""
    if path is None:
        return ""
    text = str(path).strip()
    if not text:
        return ""
    segments = re.split(r"[\\/]+", text)
    last = segments[-1].strip()
    return last or text


def _first_query_keyword(query) -> str:
    """First keyword of a search query: the first non-empty ``|`` alternative,
    then the first whitespace-delimited token (leading ``^`` removed)."""
    if query is None:
        return ""
    text = str(query).strip()
    if not text:
        return ""
    alternatives = [part.strip() for part in text.split("|")]
    first = next((part for part in alternatives if part), "")
    keyword = ""
    if first:
        keyword = re.sub(r"^\^+", "", first).strip().split()[0].strip()
    return keyword or first or text


def _tokenize(statement: str) -> "list[str]":
    """Split a statement into shell words, honoring single/double quotes so a
    quoted argument containing spaces stays one token.  Quote characters are
    kept on the token; :func:`_unquote` strips them for display.

    Inside double quotes ``\\x`` is an escaped literal character (e.g. ``\\"``).
    """
    tokens: "list[str]" = []
    current: "list[str]" = []
    quote: "str | None" = None
    i = 0
    n = len(statement)
    while i < n:
        ch = statement[i]
        if quote is not None:
            if ch == "\\" and quote == '"' and i + 1 < n:
                current.append(ch + statement[i + 1])
                i += 1
            else:
                if ch == quote:
                    quote = None
                current.append(ch)
        elif ch == '"' or ch == "'":
            quote = ch
            current.append(ch)
        elif ch.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
        i += 1
    if current:
        tokens.append("".join(current))
    return tokens


def _is_noise_statement(statement: str) -> bool:
    tokens = _tokenize(statement)
    index = 0
    while index < len(tokens) and _ASSIGNMENT_TOKEN.match(tokens[index]):
        index += 1
    head = tokens[index] if index < len(tokens) else ""
    if not head:
        return True
    return head.lower() in NOISE_COMMANDS


def _unquote(token: str) -> str:
    """Strip one pair of matching surrounding quotes for display."""
    if len(token) >= 2:
        first = token[0]
        if (first == '"' or first == "'") and token.endswith(first):
            return token[1:-1]
    return token


def _summarize_command(statement: str) -> str:
    """Shorten a statement to its command name plus the first substantive
    argument.  Switches (-n, --verbose), shell punctuation (|, >, ...) and
    leading ``VAR=value`` prefixes are dropped, and one pair of surrounding
    quotes is stripped from the kept argument: ``sed -i '1520,1600p' x.py``
    summarizes to ``sed 1520,1600p``."""
    tokens = _tokenize(statement)
    start = 0
    while start < len(tokens) and _ASSIGNMENT_TOKEN.match(tokens[start]):
        start += 1
    if start >= len(tokens):
        return ""
    command = tokens[start]
    for i in range(start + 1, len(tokens)):
        arg = tokens[i]
        if arg.startswith("-"):
            continue  # switches: -n, -la, --verbose
        # A bare shell operator (|, >, <) starts a new command or redirection:
        # stop here so `ls -la | grep py` summarizes to `ls`, not `ls grep`.
        if not _WORD_RE.search(arg):
            break
        return f"{command} {_unquote(arg)}"
    return command


def _first_substantive_command(command) -> str:
    """First statement of a compound command that does real work.  Noise
    statements (cd, echo, bare assignments, ...) are skipped so the badge
    shows e.g. ``python run.py`` for ``cd /tmp && echo hi && python run.py``."""
    if command is None:
        return ""
    text = str(command).strip()
    if not text:
        return ""
    segments = [seg.strip() for seg in re.split(r"&&|\|\||;|\n", text)]
    segments = [seg for seg in segments if seg]
    for segment in segments:
        if not _is_noise_statement(segment):
            return _summarize_command(segment)
    # Every statement is noise (e.g. a bare `cd x`); still surface something.
    return _summarize_command(segments[0]) if segments else ""


def compact_tool_display(tc: "dict | None", fallback_name: str = "") -> "tuple[str, str]":
    """Return ``(icon, label)`` for a tool call's compact badge.

    Port of ``web/src/lib/compact-tool.js`` ``compactToolDisplay``:
    file tools → basename of ``args.path``; search_code → first query keyword;
    exec_shell/exec_cli → first substantive command summary; everything else
    → the tool name (or *fallback_name*).  Whitespace is collapsed and labels
    longer than 40 characters are truncated with an ellipsis.
    """
    name = tc.get("name") if isinstance(tc, dict) else None
    icon = TOOL_ICONS.get(name) if name else None
    if not icon:
        icon = DEFAULT_ICON
    args = parse_tool_args(tc)

    label = ""
    if name in FILE_TOOLS:
        label = _basename_of_path(args.get("path"))
    elif name == "search_code":
        label = _first_query_keyword(args.get("query"))
    elif name in ("exec_shell", "exec_cli"):
        label = _first_substantive_command(args.get("command"))

    if not label:
        label = name or fallback_name
    return icon, _truncate_label(label)


# ===========================================================================
# Blocks / Transcript
# ===========================================================================

@dataclass
class Block:
    """One display unit of the transcript (contract §4.2).

    ``lines`` are *unwrapped* logical lines; the ui layer wraps/indents/
    colors them.  ``full_lines`` is the Ctrl+O expansion content (None = not
    expandable).  ``badge`` is tool-only: the compact single-line text.
    """

    kind: str  # "user"|"assistant"|"tool"|"usage"|"error"|"system"
    lines: "list[str]"
    full_lines: "Optional[list[str]]" = None
    badge: "Optional[str]" = None
    detail: str = ""


_STATUS_PENDING = "\u2026"   # …
_STATUS_OK = "\u2713"        # ✓
_STATUS_FAIL = "\u2717"      # ✗


def _is_error_content(content: "str | None") -> bool:
    if not content:
        return False
    return content.lstrip().startswith("Error:")


def _error_text(content: str) -> str:
    stripped = content.lstrip()
    if stripped.startswith("Error:"):
        stripped = stripped[len("Error:"):]
    text = stripped.strip()
    return text if text else "Error"


def _split_lines(text: "str | None") -> "list[str]":
    if not text:
        return []
    return text.splitlines()


def _usage_text(stat: dict) -> str:
    """Contract §4.2 single-line usage text.

    ``⌁ ↑{prompt_tokens} ↓{completion_tokens} {total_tokens} tok ·
    {overall_ms/1000:.1f}s`` plus `` Σ↑{total_prompt_tokens}
    Σ↓{total_completion_tokens}`` when cumulative values exist.
    """
    prompt = stat.get("prompt_tokens") or 0
    completion = stat.get("completion_tokens") or 0
    total = stat.get("total_tokens")
    if total is None:
        total = prompt + completion
    overall_ms = stat.get("overall_ms")
    if overall_ms is None:
        overall_ms = stat.get("total_ms")
    try:
        seconds = float(overall_ms or 0) / 1000.0
    except (TypeError, ValueError):
        seconds = 0.0
    text = (f"\u2301 \u2191{prompt} \u2193{completion} {total} tok"
            f" \u00b7 {seconds:.1f}s")
    total_prompt = stat.get("total_prompt_tokens")
    total_completion = stat.get("total_completion_tokens")
    if total_prompt is not None or total_completion is not None:
        text += f" \u03a3\u2191{total_prompt} \u03a3\u2193{total_completion}"
    return text


def _merge_tool_calls(existing: "list[dict]", incoming: "list[dict]") -> "list[dict]":
    """Merge incoming tool calls into *existing* **in place** (so pending
    tool blocks keep pointing at live dicts) and return the list.

    Port of ``web/src/lib/stream-messages.js`` ``mergeToolCallDeltas``:
    calls carrying an explicit ``_index`` are protocol deltas (name and
    string arguments concatenate); non-indexed calls are complete values
    (assignment, idempotent against replay).  Matching prefers the delta
    index, then ``id`` / ``tool_use_id``.
    """
    for inc in incoming or []:
        if not isinstance(inc, dict):
            continue
        has_index = inc.get("_index") is not None
        inc_index = inc.get("_index")
        inc_id = inc.get("id") or inc.get("tool_use_id")
        pos = -1
        if has_index:
            for i, tc in enumerate(existing):
                if (isinstance(tc, dict) and tc.get("_index") is not None
                        and tc.get("_index") == inc_index):
                    pos = i
                    break
        if pos < 0 and inc_id:
            for i, tc in enumerate(existing):
                if isinstance(tc, dict) and (
                        tc.get("id") == inc_id
                        or tc.get("tool_use_id") == inc_id):
                    pos = i
                    break
        if pos < 0:
            existing.append(dict(inc))
            continue
        cur = existing[pos]
        if inc.get("id"):
            cur["id"] = inc["id"]
        if inc.get("tool_use_id"):
            cur["tool_use_id"] = inc["tool_use_id"]
        if has_index:
            # Indexed calls are protocol deltas.
            if inc.get("_index") is not None:
                cur["_index"] = inc_index
            if inc.get("name"):
                cur["name"] = (cur.get("name") or "") + inc["name"]
            if inc.get("arguments") is not None:
                if isinstance(inc["arguments"], str):
                    cur["arguments"] = (cur.get("arguments") or "") + inc["arguments"]
                else:
                    cur["arguments"] = inc["arguments"]
        else:
            # Non-indexed calls are complete values (idempotent replay).
            if inc.get("name") is not None:
                cur["name"] = inc["name"]
            if inc.get("arguments") is not None:
                cur["arguments"] = inc["arguments"]
            for key, value in inc.items():
                if key in ("id", "tool_use_id", "name", "arguments"):
                    continue
                if value is not None:
                    cur[key] = value
    return existing


class Transcript:
    """Accumulates SSE frames / a persisted conversation into display blocks.

    Frame semantics (contract §4.2):
      - ``init``     → record session_id / title (no block);
      - ``message``  → merged by role: user / assistant (consecutive
                       assistant frames merge into one block, content
                       concatenates like the web client) / tool (paired with
                       the declaring assistant ``tool_calls`` by
                       ``tool_use_id`` ↔ ``tc.id``, name+order fallback) /
                       ``Error:``-prefixed assistant content → error block;
      - ``usage``    → keep only the last one (in-place update);
      - ``error``    → error block;
      - ``done``     → no-op.
    """

    def __init__(self) -> None:
        self._blocks: "list[Block]" = []
        self._session_id: "Optional[str]" = None
        self._title = ""
        self._last_usage: "Optional[dict]" = None
        # Unpaired assistant tool calls, in declaration order:
        # {"tc": dict (live), "block": Block, "paired": bool}
        self._pending: "list[dict]" = []
        # The assistant block that consecutive assistant frames merge into
        # (closed by user / tool-result / usage / error frames).
        self._current_assistant: "Optional[Block]" = None
        # Current round's accumulated tool calls (merged in place) + the
        # dedup keys for the badges already declared from them.
        self._new_round()

    # -- accessors ----------------------------------------------------------

    def blocks(self) -> "list[Block]":
        return list(self._blocks)

    def session_id(self) -> "Optional[str]":
        return self._session_id

    def title(self) -> str:
        return self._title

    def last_usage(self) -> "Optional[dict]":
        return self._last_usage

    def reset(self) -> None:
        self._blocks = []
        self._pending = []
        self._last_usage = None
        self._session_id = None
        self._title = ""
        self._current_assistant = None
        self._new_round()

    def _new_round(self) -> None:
        """Start a fresh assistant/tool round (accumulators reset)."""
        self._current_assistant = None
        self._round_tcs: "list[dict]" = []
        self._round_tcs_keys: dict = {"objects": set(), "ids": set()}

    # -- frame intake --------------------------------------------------------

    def append_frame(self, event: str, data: dict) -> None:
        data = data if isinstance(data, dict) else {}
        if event == "init":
            sid = data.get("session_id")
            if sid:
                self._session_id = str(sid)
            if data.get("title"):
                self._title = str(data["title"])
            return
        if event == "message":
            self._append_message(data)
            return
        if event == "usage":
            self._usage_frame(data)
            return
        if event == "error":
            message = str(data.get("message") or "unknown error")
            self._blocks.append(Block("error", [message]))
            return
        # "done" and anything else: nothing to render.

    def load_conversation(self, data: dict) -> None:
        """Load a persisted conversation (``--session`` history).

        Envelope (verified against ``runtime/context_manager.py::
        save_conversation``): ``{"meta": {...}, "messages": [...]}`` where the
        messages match the SSE Message dicts, assistant turns may carry a
        ``stat`` dict (the round's usage) and ``tool_calls`` arguments are
        already objects.  A bare message list is tolerated.
        """
        self.reset()
        meta: dict = {}
        messages: "list[dict]" = []
        if isinstance(data, dict):
            meta = data.get("meta") or {}
            if not isinstance(meta, dict):
                meta = {}
            messages = data.get("messages") or []
            if not isinstance(messages, list):
                messages = []
        elif isinstance(data, list):
            messages = data
        sid = meta.get("session_id")
        if sid:
            self._session_id = str(sid)
        title = meta.get("title")
        if title:
            self._title = str(title)
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            # Persisted turns are atomic: never merge across turns (unlike
            # live token-delta frames), so each one starts a fresh round.
            self._new_round()
            role = msg.get("role")
            if role == "usage":
                # Raw (unmerged) persistence: content holds the TokenStat JSON.
                stat = self._maybe_json(msg.get("content"))
                if stat:
                    self._usage_frame(stat)
            elif role in ("user", "assistant", "tool"):
                self._append_message(msg)
            # "system" and unknown roles are skipped.

    # -- message dispatch ------------------------------------------------------

    def _maybe_json(self, text) -> "Optional[dict]":
        if not isinstance(text, str) or not text.strip():
            return None
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return None
        return obj if isinstance(obj, dict) else None

    def _append_message(self, msg: dict) -> None:
        role = msg.get("role")
        if role == "user":
            self._append_user(msg)
        elif role == "assistant":
            self._append_assistant(msg)
        elif role == "tool":
            self._append_tool_result(msg)
        elif role == "usage":
            stat = self._maybe_json(msg.get("content"))
            if stat:
                self._usage_frame(stat)
        elif role == "system":
            return
        elif msg.get("type") == "remove_trailing_assistant":
            self._remove_trailing_assistant()
        # Unknown frames are ignored.

    def _append_user(self, msg: dict) -> None:
        self._current_assistant = None
        self._blocks.append(Block("user", _split_lines(msg.get("content"))))

    def _assistant_block(self) -> Block:
        """Return the current assistant block or create a new one.

        Consecutive assistant frames (token deltas / tool-call deltas) merge
        into the block opened for the current round; the block is closed by a
        tool-result / user / usage / error frame.
        """
        block = self._current_assistant
        if not (isinstance(block, Block) and block.kind == "assistant"):
            block = Block("assistant", [])
            self._blocks.append(block)
        self._current_assistant = block
        acc = getattr(block, "_acc", None)
        if not isinstance(acc, dict):
            acc = {"content": "", "thinking": "", "tool_calls": [],
                   "images_count": 0}
            block._acc = acc  # type: ignore[attr-defined]
        return block

    def _rebuild_assistant_block(self, block: Block) -> None:
        acc = block._acc  # type: ignore[attr-defined]
        lines = acc["content"].splitlines()
        if acc.get("images_count"):
            lines = lines + [
                f"[\u56fe\u7247] {acc['images_count']} \u5f20"
                f"\uff08\u5b58\u4e8e\u4f1a\u8bdd artifact\uff09"
            ]
        block.lines = lines
        if acc["thinking"]:
            block.full_lines = [
                "\u601d\u8003: " + line for line in acc["thinking"].splitlines()
            ]
        else:
            block.full_lines = None

    def _append_assistant(self, msg: dict) -> None:
        content = msg.get("content") or ""
        if _is_error_content(content):
            # Error round: dedicated error block, prefix removed.
            self._new_round()
            self._blocks.append(Block("error", [_error_text(content)]))
            return

        thinking = msg.get("thinking") or ""
        tool_calls = msg.get("tool_calls") or []
        images = msg.get("images") or []
        if not content and not thinking and not tool_calls and not images:
            return  # metadata-only frame: nothing to display

        # A tool-calls-only frame (no content/thinking/images) must not open
        # an empty assistant block — only the badges are displayed.
        block = None
        if content or thinking or images:
            block = self._assistant_block()
            acc = block._acc  # type: ignore[attr-defined]
            if content:
                acc["content"] += content
            if thinking:
                acc["thinking"] += thinking
            if images and not acc.get("images_count"):
                acc["images_count"] = len(images)
            self._rebuild_assistant_block(block)
        if tool_calls:
            _merge_tool_calls(self._round_tcs, tool_calls)
            self._declare_tool_blocks()
            # Deltas may have completed a name/arguments; keep the pending
            # badges in sync (e.g. "read" → "read_file" → "a.txt").
            self._refresh_unpaired_badges()
        # Persisted (merged) assistant turns carry the round usage in "stat";
        # route it through the same usage frame path, after the assistant
        # block so the usage block stays at the tail (last one wins).
        stat = msg.get("stat")
        if isinstance(stat, dict) and stat:
            self._usage_frame(stat)

    def _declare_tool_blocks(self) -> None:
        keys = self._round_tcs_keys
        for tc in self._round_tcs:
            if not isinstance(tc, dict):
                continue
            tc_id = tc.get("id")
            # Object identity is stable because _merge_tool_calls mutates the
            # stored dicts in place; ids are tracked too so a late-arriving
            # id (or a replayed call) never spawns a second badge.
            already_by_id = bool(tc_id) and tc_id in keys["ids"]
            if tc_id:
                keys["ids"].add(tc_id)
            if id(tc) in keys["objects"] or already_by_id:
                continue
            if not tc_id and not tc.get("name"):
                continue  # still streaming: id/name not arrived yet
            keys["objects"].add(id(tc))
            icon, label = compact_tool_display(tc, "")
            badge = f"\u25b8 {icon} {label} {_STATUS_PENDING}"
            tblock = Block("tool", [badge], badge=badge)
            self._blocks.append(tblock)
            self._pending.append({"tc": tc, "block": tblock, "paired": False})

    def _refresh_unpaired_badges(self) -> None:
        for entry in self._pending:
            if entry["paired"]:
                continue
            icon, label = compact_tool_display(entry["tc"], "")
            badge = f"\u25b8 {icon} {label} {_STATUS_PENDING}"
            entry["block"].badge = badge
            entry["block"].lines = [badge]

    def _append_tool_result(self, msg: dict) -> None:
        # A tool result closes the assistant round that declared the call:
        # the next assistant frame starts a fresh block.
        self._new_round()
        content = msg.get("content") or ""
        tool_use_id = msg.get("tool_use_id")
        name = msg.get("name") or msg.get("tool_id") or ""

        entry = None
        # 1) explicit pairing: tool frame tool_use_id ↔ assistant tc id
        if tool_use_id:
            for cand in self._pending:
                if not cand["paired"] and cand["tc"].get("id") == tool_use_id:
                    entry = cand
                    break
        # 2) fallback: name + order
        if entry is None:
            for cand in self._pending:
                if not cand["paired"] and name and cand["tc"].get("name") == name:
                    entry = cand
                    break
        # 3) last resort: first unpaired declaration, in order
        if entry is None:
            for cand in self._pending:
                if not cand["paired"]:
                    entry = cand
                    break

        status = _STATUS_FAIL if _is_error_content(content) else _STATUS_OK
        full_lines = _split_lines(content)

        if entry is not None:
            entry["paired"] = True
            tc = entry["tc"]
            block = entry["block"]
            icon, label = compact_tool_display(tc, "")
        else:
            # Result without a visible declaration (e.g. loaded history gap):
            # synthesize the badge from the tool name.
            icon, label = compact_tool_display(
                {"name": name} if name else None, name or "tool"
            )
            block = Block("tool", [])
            self._blocks.append(block)
        badge = f"\u25b8 {icon} {label} {status}"
        block.badge = badge
        block.lines = [badge]
        block.full_lines = full_lines
        block.detail = f"{len(full_lines)} \u884c"

    def _usage_frame(self, stat: dict) -> None:
        """Keep only the last usage (contract §4.2): any earlier usage block
        is dropped and the new one sits at the tail.  Consecutive usage
        frames at the tail therefore update in place.  A usage frame closes
        the round, so the next assistant frame starts a fresh block."""
        if not isinstance(stat, dict) or not stat:
            return
        self._current_assistant = None
        self._last_usage = stat
        self._blocks = [b for b in self._blocks if b.kind != "usage"]
        self._blocks.append(Block("usage", [_usage_text(stat)]))

    def _remove_trailing_assistant(self) -> None:
        """Continue flow: the backend removed the persisted final assistant
        turn; mirror that by dropping the trailing assistant block (plus any
        tool blocks that are still awaiting their result)."""
        if not self._blocks or self._blocks[-1].kind != "assistant":
            return
        self._blocks.pop()
        self._new_round()
        pending_keys = {id(e["block"]) for e in self._pending if not e["paired"]}
        kept: "list[Block]" = []
        for block in self._blocks:
            if (block.kind == "tool" and id(block) in pending_keys
                    and block.badge and block.badge.endswith(_STATUS_PENDING)):
                continue
            kept.append(block)
        self._blocks = kept
        self._pending = [e for e in self._pending if id(e["block"]) not in pending_keys]
