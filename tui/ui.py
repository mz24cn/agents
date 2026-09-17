"""Mini UI framework for the Lianhua TUI.

Responsibilities (frozen by ``docs/tui-contract.md`` §6):

* an in-memory screen buffer with **differential redraw** — only rows whose
  rendered text changed are written back to the terminal;
* widgets: transcript view (scrollback, block rendering, single-line tool
  badges with ``Ctrl+O`` expand), input line (cursor editing, history,
  ``/``-command mode, Tab completion), modal selector, title bar, status bar;
* a **plain-mode backend** that emits log-style text without any ANSI.

Wrapping is always computed from *display width*, never ``len()``:
CJK/emoji advance two columns (``unicodedata.east_asian_width`` in ``W``/``F``),
everything else one.

This module knows nothing about the concrete ``Block``/``Transcript`` types
from :mod:`tui.render`; it consumes any object exposing ``kind``, ``lines``,
``full_lines``, ``badge`` and ``detail`` attributes.  That keeps the widget
layer independently testable with lightweight stubs.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

__all__ = [
    "char_width", "display_width", "visible_width", "strip_ansi",
    "wrap_line", "wrap_text", "truncate_to_width", "truncate_left_to_width",
    "pad_right", "paint",
    "COLOR_256", "tunnel_badge",
    "FrameWriter", "TitleBar", "StatusBar", "InputLine", "TranscriptView",
    "SelectorItem", "Selector", "TextPage", "TuiUI", "PlainUI",
]

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


# ══════════════════════════════════════════════════════════════════════════
# Display-width aware text helpers
# ══════════════════════════════════════════════════════════════════════════

def char_width(ch: str) -> int:
    """Display width of a single character.

    ``W``/``F`` East-Asian-width characters (CJK, most emoji) count as 2,
    everything else as 1 — exactly as the contract mandates.  Wrapping must
    never use ``len()``.
    """
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def visible_width(text: str) -> int:
    """Display width ignoring embedded ANSI SGR sequences."""
    return display_width(strip_ansi(text))


def wrap_line(line: str, width: int) -> List[str]:
    """Hard-wrap one logical line to ``width`` display columns.

    A wide character that would straddle the boundary is moved whole to the
    next row (it is never split); this can overflow ``width`` by one column
    only for a degenerate ``width == 1`` with a double-width character.
    """
    if width <= 0:
        return [line]
    if line == "":
        return [""]
    out: List[str] = []
    cur: List[str] = []
    used = 0
    for ch in line:
        cw = char_width(ch)
        if cur and used + cw > width:
            out.append("".join(cur))
            cur = []
            used = 0
        cur.append(ch)
        used += cw
    out.append("".join(cur))
    return out


def wrap_text(text: str, width: int) -> List[str]:
    """Wrap a multi-line string; ``\\n`` starts a new logical line."""
    if text is None:
        return [""]
    lines: List[str] = []
    for raw in str(text).split("\n"):
        lines.extend(wrap_line(raw, width))
    return lines or [""]


def truncate_to_width(text: str, width: int, ellipsis: str = "…") -> str:
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    ew = display_width(ellipsis)
    if ew >= width:
        return ellipsis[:width]
    limit = width - ew
    out: List[str] = []
    used = 0
    for ch in text:
        cw = char_width(ch)
        if used + cw > limit:
            break
        out.append(ch)
        used += cw
    return "".join(out) + ellipsis


def truncate_left_to_width(text: str, width: int, ellipsis: str = "...") -> str:
    """Keep the *tail* of ``text`` within ``width`` display columns.

    Used by the title bar for the workspace path, where the trailing path
    component is far more useful than the root.  The kept tail is prefixed
    with an ASCII ``...`` (never the East-Asian Ambiguous ``\u2026``) whenever
    truncation actually happens.
    """
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    ew = display_width(ellipsis)
    if ew >= width:
        # Not enough room for the full prefix: degrade to its leading columns.
        out: List[str] = []
        used = 0
        for ch in ellipsis:
            cw = char_width(ch)
            if used + cw > width:
                break
            out.append(ch)
            used += cw
        return "".join(out)
    budget = width - ew
    out = []
    used = 0
    for ch in reversed(text):
        cw = char_width(ch)
        if used + cw > budget:
            break
        out.append(ch)
        used += cw
    return ellipsis + "".join(reversed(out))


def pad_right(text: str, width: int) -> str:
    """Pad ``text`` with spaces up to ``width`` display columns (never cuts)."""
    pad = width - visible_width(text)
    if pad <= 0:
        return text
    return text + " " * pad


# ══════════════════════════════════════════════════════════════════════════
# Color semantics (contract §6)
# ══════════════════════════════════════════════════════════════════════════

#: 256-color SGR prefixes per block kind.  ``assistant`` intentionally has no
#: color (terminal default).
COLOR_256 = {
    "user": "\x1b[38;5;75m",     # blue
    "tool": "\x1b[38;5;245m",    # gray
    "usage": "\x1b[38;5;240m",   # dark gray
    "error": "\x1b[38;5;203m",   # red
    "system": "\x1b[38;5;30m",   # dark cyan
}

_RESET = "\x1b[0m"


def paint(text: str, kind: str) -> str:
    """Color ``text`` according to the block ``kind`` (default = untouched)."""
    prefix = COLOR_256.get(kind)
    if not prefix:
        return text
    return prefix + text + _RESET


#: States that clearly indicate a live tunnel.  The concrete backend
#: enumeration is *not* relied upon (contract §3.5); anything not recognised
#: falls back to "configured but not connected".
_TUNNEL_ONLINE_STATES = {
    "online", "connected", "ready", "active", "open", "up", "established",
}
_TUNNEL_OFFLINE_MARKERS = (
    "connect",  # connecting / reconnecting / disconnected (all "not yet online")
    "disconnect",
    "offline",
    "inactive",
    "error",
    "idle",
    "unregistered",
    "registering",
)


def tunnel_badge(status: Optional[dict]) -> str:
    """Compact tunnel badge text for the title bar.

    A badge is shown **only** while the tunnel is actually enabled *and* a
    parent address is configured; in every other case the result is the empty
    string (no standalone fallback — a non-tunnel child cannot know anything
    about its parent, so it must stay silent).  When both flags are set the
    backend ``state`` only decides between the online and offline variants.
    """
    if not status or not status.get("enabled") or not status.get("configured"):
        return ""
    state = str(status.get("state") or "").strip().lower()
    online = state in _TUNNEL_ONLINE_STATES or state.endswith("online")
    if any(marker in state for marker in _TUNNEL_OFFLINE_MARKERS) and state not in (
        _TUNNEL_ONLINE_STATES
    ):
        online = False
    return "已注册->母端(在线)" if online else "已注册->母端(离线)"


# ══════════════════════════════════════════════════════════════════════════
# Differential redraw core
# ══════════════════════════════════════════════════════════════════════════

class FrameWriter:
    """Writes a frame (list of rows) to a screen, touching only changed rows.

    ``draw`` returns the number of rows actually written.  ``invalidate``
    forces the next ``draw`` to rewrite everything (Ctrl+L / resize / mode
    switch).
    """

    def __init__(self, screen) -> None:
        self.screen = screen
        self._prev: Optional[List[str]] = None

    def invalidate(self) -> None:
        self._prev = None

    def draw(self, lines: Sequence[str]) -> int:
        prev = self._prev
        forced = prev is None or len(prev) != len(lines)
        written = 0
        for row, text in enumerate(lines):
            if forced or prev is None or row >= len(prev) or prev[row] != text:
                self.screen.write_line(row, text)
                written += 1
        self._prev = list(lines)
        return written


# ══════════════════════════════════════════════════════════════════════════
# Bars
# ══════════════════════════════════════════════════════════════════════════

class TitleBar:
    """``{model} {工具:n} {workspace} {title}`` ... ``{badge} {agent}``.

    Mirrors the web ``ChatPage`` selection bar: the model sits left-most, the
    agent is right-aligned to the final column (same right-align rule as the
    status bar's ``推理:`` segment), and the selected-tool count / workspace /
    session title fill the middle in that order.  Shrinking the terminal
    drops the middle segments first (``title`` -> ``workspace`` -> ``工具:n``),
    then the badge, and only right-truncates the model as a last resort, so the
    rendered row is always ``<= width``.  Every fixed string here avoids
    East-Asian *Ambiguous* glyphs; truncation ellipses are ASCII ``...``.
    """

    def render(self, width: int, model: str, agent: str, badge: str,
               title: str, tool_count: Optional[int],
               workspace: str) -> str:
        width = max(0, width)
        if width <= 0:
            return ""
        model = model or ""
        agent = agent or ""
        badge = badge or ""
        title = title or ""
        workspace = workspace or ""

        middle: List[tuple] = []
        if tool_count is not None:
            middle.append(("tools", f"工具:{tool_count}"))
        if workspace:
            middle.append(("workspace", workspace))
        if title:
            middle.append(("title", title))

        def _blocks(items: List[tuple], use_badge: bool, model_text: str):
            left = " ".join(([model_text] if model_text else [])
                            + [seg for _, seg in items])
            right = " ".join(
                p for p in ((badge if use_badge else ""), agent) if p
            )
            return left, right

        def _fits(left: str, right: str) -> bool:
            lw, rw = display_width(left), display_width(right)
            if left and right:
                return lw + 1 + rw <= width
            return lw <= width and rw <= width

        def _assemble(left: str, right: str) -> str:
            lw, rw = display_width(left), display_width(right)
            if left and right:
                return left + " " * (width - lw - rw) + right
            if left:
                return left
            if right:
                return " " * (width - rw) + right
            return ""

        def _done(left: str, right: str) -> str:
            text = _assemble(left, right)
            return ("\x1b[1m" + text + _RESET) if text else ""

        left, right = _blocks(middle, True, model)
        if _fits(left, right):
            return _done(left, right)

        # 1. Drop the session title.
        middle = [it for it in middle if it[0] != "title"]
        left, right = _blocks(middle, True, model)
        if _fits(left, right):
            return _done(left, right)

        # 2. Left-truncate the workspace (keep the path tail); drop it entirely
        #    when not even the ``...`` prefix fits.
        wi = next((i for i, it in enumerate(middle) if it[0] == "workspace"),
                  None)
        if wi is not None:
            rest = middle[:wi] + middle[wi + 1:]
            rest_left = ([model] if model else []) + [seg for _, seg in rest]
            rest_w = display_width(" ".join(rest_left))
            sep = 1 if rest_left else 0
            right_w = display_width(" ".join(p for p in (badge, agent) if p))
            gap = 1 if right_w else 0
            budget = width - rest_w - sep - gap - right_w
            if budget >= display_width("..."):
                middle = (middle[:wi]
                          + [("workspace",
                              truncate_left_to_width(workspace, budget))]
                          + middle[wi + 1:])
                left, right = _blocks(middle, True, model)
                if _fits(left, right):
                    return _done(left, right)
            middle = [it for it in middle if it[0] != "workspace"]
            left, right = _blocks(middle, True, model)
            if _fits(left, right):
                return _done(left, right)

        # 3. Drop the selected-tool count.
        middle = [it for it in middle if it[0] != "tools"]
        left, right = _blocks(middle, True, model)
        if _fits(left, right):
            return _done(left, right)

        # 4. Drop the badge; the agent is never dropped.
        left, right = _blocks(middle, False, model)
        if _fits(left, right):
            return _done(left, right)

        # 5. Last resort: right-truncate the model (ASCII ellipsis).
        agent_w = display_width(agent)
        if agent_w > width:
            agent = truncate_to_width(agent, width, ellipsis="...")
            model = ""
        else:
            model = truncate_to_width(
                model, width - (1 if agent else 0) - agent_w, ellipsis="..."
            )
        return _done(model, agent)


class StatusBar:
    """Left: adaptive key hints.  Right: streaming state (or a transient hint).

    Every glyph here is unambiguously one column wide on a CJK terminal: the
    bar never uses East-Asian *Ambiguous* characters (``↑`` ``↓`` ``…`` ``→``
    ``⚓``), which some fonts render double-width and which would push the last
    column of the row onto the next line.
    """

    #: Hint segments in fixed display order (joined by single spaces).  The
    #: banner's third line ("回车发送；/help …；Ctrl+C …") now lives here.
    HINTS = ("回车发送 [/]命令 [上下]回看 [/help]帮助 "
             "[Ctrl+C]中断/退出 [Ctrl+L]重绘")

    #: ``(segment, priority)`` — higher priority is kept longer when the bar is
    #: too narrow.  Priority is independent of the fixed display order.
    _LEFT_SEGMENTS = (
        ("回车发送", 6),
        ("[/]命令", 2),
        ("[上下]回看", 3),
        ("[/help]帮助", 5),
        ("[Ctrl+C]中断/退出", 1),
        ("[Ctrl+L]重绘", 4),
    )

    def _fit_left(self, width: int, right_w: int) -> str:
        """Greedily keep the highest-priority hint segments that still fit.

        Segments are *chosen* by priority but always *joined* in the fixed
        display order declared by :attr:`_LEFT_SEGMENTS`.
        """
        kept = set()
        for segment, _priority in sorted(self._LEFT_SEGMENTS,
                                         key=lambda item: item[1]):
            trial = [seg for seg, _ in self._LEFT_SEGMENTS
                     if seg == segment or seg in kept]
            if display_width(" ".join(trial)) + 1 + right_w <= width:
                kept.add(segment)
        return " ".join(seg for seg, _ in self._LEFT_SEGMENTS if seg in kept)

    def render(self, width: int, streaming: bool, hint: str = "") -> str:
        if width <= 0:
            return paint("", "system")
        right = hint if hint else ("推理:进行中..." if streaming else "推理:空闲")
        right_w = display_width(right)
        if right_w > width:
            # Not even the state text fits: truncate it to the row.
            return paint(truncate_to_width(right, width), "system")
        left = self._fit_left(width, right_w)
        if not left:
            # No hint segment fits: right-align the state text.
            return paint(" " * (width - right_w) + right, "system")
        pad = " " * (width - display_width(left) - right_w)
        return paint(left + pad + right, "system")


# ══════════════════════════════════════════════════════════════════════════
# Input line
# ══════════════════════════════════════════════════════════════════════════

class InputLine:
    """Editable single-line buffer with history and ``/`` command detection."""

    MAX_HISTORY = 200

    def __init__(self) -> None:
        self.buffer = ""
        self.cursor = 0
        self.history: List[str] = []
        self._hist_idx: Optional[int] = None

    # ── content ──
    def text(self) -> str:
        return self.buffer

    def set_text(self, text: str) -> None:
        self.buffer = text
        self.cursor = len(text)
        self._hist_idx = None

    def clear(self) -> None:
        self.buffer = ""
        self.cursor = 0
        self._hist_idx = None

    def is_command(self) -> bool:
        return self.buffer.startswith("/")

    # ── editing ──
    def insert(self, text: str) -> None:
        self.buffer = self.buffer[:self.cursor] + text + self.buffer[self.cursor:]
        self.cursor += len(text)

    def backspace(self) -> None:
        if self.cursor > 0:
            self.buffer = self.buffer[:self.cursor - 1] + self.buffer[self.cursor:]
            self.cursor -= 1

    def delete(self) -> None:
        if self.cursor < len(self.buffer):
            self.buffer = self.buffer[:self.cursor] + self.buffer[self.cursor + 1:]

    def left(self) -> None:
        self.cursor = max(0, self.cursor - 1)

    def right(self) -> None:
        self.cursor = min(len(self.buffer), self.cursor + 1)

    def home(self) -> None:
        self.cursor = 0

    def end(self) -> None:
        self.cursor = len(self.buffer)

    # ── history ──
    def push_history(self, text: str) -> None:
        if not text:
            return
        if not self.history or self.history[-1] != text:
            self.history.append(text)
            if len(self.history) > self.MAX_HISTORY:
                self.history = self.history[-self.MAX_HISTORY:]
        self._hist_idx = None

    def history_prev(self) -> None:
        if not self.history:
            return
        if self._hist_idx is None:
            self._hist_idx = len(self.history) - 1
        else:
            self._hist_idx = max(0, self._hist_idx - 1)
        self.buffer = self.history[self._hist_idx]
        self.cursor = len(self.buffer)

    def history_next(self) -> None:
        if not self.history or self._hist_idx is None:
            return
        if self._hist_idx >= len(self.history) - 1:
            self._hist_idx = None
            self.buffer = ""
            self.cursor = 0
            return
        self._hist_idx += 1
        self.buffer = self.history[self._hist_idx]
        self.cursor = len(self.buffer)

    # ── rendering ──
    def render(self, width: int, prompt: str = "› ") -> Tuple[str, int]:
        """Return ``(display_text, cursor_column)``.

        The buffer is horizontally scrolled so the caret stays visible when the
        text is wider than the available columns.
        """
        width = max(1, width)
        prompt_w = display_width(prompt)
        avail = max(1, width - prompt_w)

        cursor_w = display_width(self.buffer[:self.cursor])
        # Drop leading characters until the caret fits inside the window.
        start = 0
        while start < self.cursor and (cursor_w - display_width(self.buffer[:start])) >= avail:
            start += 1
        end = start
        used = 0
        while end < len(self.buffer) and used + char_width(self.buffer[end]) <= avail:
            used += char_width(self.buffer[end])
            end += 1
        visible = self.buffer[start:end]
        cursor_col = prompt_w + (cursor_w - display_width(self.buffer[:start]))
        body = paint(visible, "system") if self.is_command() else visible
        return prompt + body, cursor_col


# ══════════════════════════════════════════════════════════════════════════
# Transcript view
# ══════════════════════════════════════════════════════════════════════════

def _attr(obj, name, default=None):
    return getattr(obj, name, default)


class TranscriptView:
    """Renders a stream of blocks with scrollback and tool-block expansion.

    ``scroll`` counts rows scrolled *up* from the bottom (0 == pinned to the
    newest output).  ``Ctrl+O`` toggles the most recent tool block.
    """

    TOOL_INDENT = "  "

    def __init__(self, provider: Optional[Callable[[], List]] = None) -> None:
        self._provider = provider or (lambda: [])
        self.scroll = 0
        self.expanded: set = set()

    def set_provider(self, provider: Callable[[], List]) -> None:
        self._provider = provider

    def reset(self) -> None:
        self.scroll = 0
        self.expanded = set()

    def blocks(self) -> List:
        return list(self._provider())

    def scroll_by(self, delta: int) -> None:
        self.scroll = max(0, self.scroll + delta)

    def scroll_to_bottom(self) -> None:
        self.scroll = 0

    def toggle_last_tool(self) -> bool:
        indices = [i for i, b in enumerate(self.blocks())
                   if _attr(b, "kind", "") == "tool"]
        if not indices:
            return False
        idx = indices[-1]
        if idx in self.expanded:
            self.expanded.discard(idx)
        else:
            self.expanded.add(idx)
        return True

    # ── rendering ──
    def rendered_lines(self, width: int) -> List[str]:
        lines: List[str] = []
        for idx, block in enumerate(self.blocks()):
            lines.extend(self._render_block(idx, block, max(1, width)))
        return lines

    def _render_block(self, idx: int, block, width: int) -> List[str]:
        kind = _attr(block, "kind", "assistant") or "assistant"
        if kind == "tool":
            return self._render_tool(idx, block, width)

        text = "\n".join(_attr(block, "lines", []) or [])
        out: List[str] = []
        for wrapped in wrap_text(text, width):
            out.append(paint(wrapped, kind))
        if not text:
            out.append("")
        return out

    def _render_tool(self, idx: int, block, width: int) -> List[str]:
        badge = _attr(block, "badge", None)
        if not badge:
            lines = _attr(block, "lines", []) or [""]
            badge = lines[0]
        out: List[str] = []
        for wrapped in wrap_text(badge, width):
            out.append(paint(wrapped, "tool"))
        if idx in self.expanded:
            indent = self.TOOL_INDENT
            inner = max(1, width - len(indent))
            for raw in _attr(block, "full_lines", None) or []:
                for wrapped in wrap_text(raw, inner):
                    out.append(indent + paint(wrapped, "tool"))
            detail = _attr(block, "detail", "")
            if detail:
                out.append(indent + paint(str(detail), "usage"))
        return out

    def render(self, width: int, height: int) -> List[str]:
        height = max(0, height)
        lines = self.rendered_lines(width)
        total = len(lines)
        max_scroll = max(0, total - height)
        if self.scroll > max_scroll:
            self.scroll = max_scroll
        end = total - self.scroll
        start = max(0, end - height)
        window = lines[start:end]
        if len(window) < height:
            window = window + [""] * (height - len(window))
        return window


# ══════════════════════════════════════════════════════════════════════════
# Modals
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class SelectorItem:
    name: str
    secondary: str = ""
    value: object = None


class Selector:
    """Modal list selector (``/sessions``, ``/agent``, ``/model``)."""

    def __init__(self, title: str, items: Sequence[SelectorItem],
                 on_select: Optional[Callable[[SelectorItem], None]] = None) -> None:
        self.title = title
        self.items: List[SelectorItem] = list(items)
        self.index = 0
        self.on_select = on_select
        self.scroll = 0

    def move(self, delta: int) -> None:
        if not self.items:
            return
        self.index = (self.index + delta) % len(self.items)

    def selected(self) -> Optional[SelectorItem]:
        if not self.items:
            return None
        return self.items[self.index]

    def render(self, width: int, height: int) -> List[str]:
        lines: List[str] = [paint(truncate_to_width(self.title, width), "system")]
        body_h = max(0, height - 1)
        if self.index < self.scroll:
            self.scroll = self.index
        if self.index >= self.scroll + body_h and body_h > 0:
            self.scroll = self.index - body_h + 1
        visible = self.items[self.scroll:self.scroll + body_h]
        for offset, item in enumerate(visible):
            i = self.scroll + offset
            marker = "> " if i == self.index else "  "
            name = truncate_to_width(item.name, max(1, width - 2))
            text = marker + name
            if item.secondary:
                room = width - visible_width(text) - 2
                if room > 1:
                    text += "  " + truncate_to_width(item.secondary, room)
            if i == self.index:
                text = "\x1b[7m" + text + _RESET
            lines.append(text)
        while len(lines) < height:
            lines.append("")
        return lines[:height]


class TextPage:
    """Scrollable modal text page (``/help``); ``q``/``Esc`` close it."""

    def __init__(self, title: str, lines: Sequence[str]) -> None:
        self.title = title
        self.lines: List[str] = list(lines)
        self.scroll = 0

    def move(self, delta: int) -> None:
        self.scroll = max(0, self.scroll + delta)

    def page(self, direction: int, height: int = 10) -> None:
        self.scroll = max(0, self.scroll + direction * max(1, height - 1))

    def render(self, width: int, height: int) -> List[str]:
        lines = [paint(truncate_to_width(self.title, width), "system")]
        body_h = max(0, height - 1)
        max_scroll = max(0, len(self.lines) - body_h)
        self.scroll = min(self.scroll, max_scroll)
        visible = self.lines[self.scroll:self.scroll + body_h]
        for raw in visible:
            lines.append(paint(truncate_to_width(raw, width), "system"))
        while len(lines) < height:
            lines.append("")
        return lines[:height]


# ══════════════════════════════════════════════════════════════════════════
# TUI orchestrator
# ══════════════════════════════════════════════════════════════════════════

class TuiUI:
    """Composes the frame and drives differential redraw + cursor placement."""

    def __init__(self, screen) -> None:
        self.screen = screen
        self.writer = FrameWriter(screen)
        self.transcript = TranscriptView()
        self.input = InputLine()
        self.title_bar = TitleBar()
        self.status_bar = StatusBar()
        self.modal = None  # Optional[Selector | TextPage]
        self.streaming = False
        self.hint = ""
        self.title = ""
        self.agent_name = ""
        self.model_name = ""
        self.badge = ""
        self.tool_count: Optional[int] = None
        self.workspace_path = ""
        self._last_size: Optional[Tuple[int, int]] = None

    def invalidate(self) -> None:
        self.writer.invalidate()

    def full_text(self, cols: Optional[int] = None, rows: Optional[int] = None) -> List[str]:
        if cols is None or rows is None:
            cols, rows = self.screen.size()
        return self.compose(cols, max(4, rows))

    def compose(self, cols: int, rows: int) -> List[str]:
        rows = max(4, rows)
        body_h = rows - 3  # title + input + status
        top = self.title_bar.render(cols, self.model_name, self.agent_name,
                                    self.badge, self.title, self.tool_count,
                                    self.workspace_path)
        if self.modal is not None:
            mid = self.modal.render(cols, body_h)
        else:
            mid = self.transcript.render(cols, body_h)
        mid = (mid + [""] * body_h)[:body_h]
        input_text, _ = self.input.render(cols)
        status = self.status_bar.render(cols, self.streaming, self.hint)
        return [top] + mid + [input_text, status]

    def draw(self) -> int:
        cols, rows = self.screen.size()
        size = (cols, rows)
        if size != self._last_size:
            self.writer.invalidate()
            self._last_size = size
        rows = max(4, rows)
        body_h = rows - 3
        written = self.writer.draw(self.compose(cols, rows))
        if self.modal is not None:
            self.screen.hide_cursor()
        else:
            _, cursor_col = self.input.render(cols)
            self.screen.move_cursor(1 + body_h, cursor_col)
            self.screen.show_cursor()
        self.screen.flush()
        return written


# ══════════════════════════════════════════════════════════════════════════
# Plain-mode backend
# ══════════════════════════════════════════════════════════════════════════

class PlainUI:
    """No-ANSI, log-style output for ``--plain``.

    ``sync(blocks)`` prints every block that is new *or* whose content changed
    (so an in-place tool-badge update still produces a log line).
    """

    USER_PREFIX = "你> "

    def __init__(self, out=None, err=None,
                 readline: Optional[Callable[[str], Optional[str]]] = None) -> None:
        self.out = out if out is not None else sys.stdout
        self.err = err if err is not None else sys.stderr
        self._readline = readline
        # Identity-keyed so that blocks keep their "already printed" state even
        # when their index shifts (app-side system blocks are appended after the
        # transcript, so transcript growth renumbers them).
        self._seen: dict = {}
        self._keepalive: dict = {}

    # ── output ──
    def _write(self, text: str) -> None:
        try:
            self.out.write(text + "\n")
            self.out.flush()
        except (OSError, ValueError):
            pass

    def system(self, text: str) -> None:
        self._write(strip_ansi(text))

    def error(self, text: str) -> None:
        self._write(strip_ansi(text))

    def banner(self, text: str) -> None:
        self._write(strip_ansi(text))

    # ── block sync ──
    @staticmethod
    def _signature(block) -> tuple:
        return (
            _attr(block, "kind", ""),
            tuple(_attr(block, "lines", []) or []),
            tuple(_attr(block, "full_lines", None) or []),
            _attr(block, "badge", None),
            _attr(block, "detail", ""),
        )

    def sync(self, blocks: Sequence) -> None:
        blocks = list(blocks)
        present = set()
        for block in blocks:
            key = id(block)
            present.add(key)
            sig = self._signature(block)
            if self._seen.get(key) == sig:
                continue
            self._seen[key] = sig
            # Keep a strong reference so the id cannot be recycled while we
            # still remember its signature.
            self._keepalive[key] = block
            self._print_block(block)
        for key in list(self._seen):
            if key not in present:
                del self._seen[key]
                self._keepalive.pop(key, None)

    def _print_block(self, block) -> None:
        kind = _attr(block, "kind", "assistant")
        if kind == "tool":
            badge = _attr(block, "badge", None)
            if not badge:
                lines = _attr(block, "lines", []) or [""]
                badge = lines[0]
            self._write(strip_ansi(str(badge)))
            return
        lines = list(_attr(block, "lines", []) or [])
        if not lines:
            # usage/error blocks may live in full_lines only.
            lines = list(_attr(block, "full_lines", None) or [])
        text = "\n".join(strip_ansi(str(l)) for l in lines)
        if kind == "user":
            self._write(self.USER_PREFIX + text)
        else:
            self._write(text)

    # ── input ──
    def set_readline(self, fn: Callable[[str], Optional[str]]) -> None:
        self._readline = fn

    def readline(self, prompt: str = "› ") -> Optional[str]:
        if self._readline is not None:
            return self._readline(prompt)
        try:
            return input(prompt)
        except EOFError:
            return None
        except KeyboardInterrupt:
            raise
