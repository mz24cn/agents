"""ANSI terminal layer for the Lianhua TUI.

This module owns everything that touches a real terminal:

* the alternate screen buffer (``\\x1b[?1049h``), cursor control and 256-color
  SGR sequences;
* terminal size discovery and ``SIGWINCH`` resize notification (on Windows the
  equivalent is a console-size poll);
* raw key input: ``termios`` + ``tty`` on Unix, ``msvcrt`` + ``ctypes`` /
  ``kernel32`` on Windows.

Design constraints (frozen by ``docs/tui-contract.md``):

* **Standard library only.**
* ``termios`` / ``tty`` are imported lazily *inside* the Unix code paths so that
  ``import tui.term`` never raises on Windows.
* A **headless injection point** is mandatory: :class:`HeadlessScreen` (an
  in-memory screen) and :class:`FakeInput` (a scripted key source) let the unit
  tests drive the whole UI without a real TTY.

The public surface used by :mod:`tui.ui` and :mod:`tui.app` is small:

    screen.size() / screen.write_line(row, text) / screen.move_cursor(row, col)
    screen.hide_cursor() / screen.show_cursor() / screen.clear() / screen.flush()
    terminal.read_key(timeout=None) -> key token or None
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import time
from typing import Callable, List, Optional, Tuple

__all__ = [
    "ESC", "CSI", "ALT_SCREEN_ON", "ALT_SCREEN_OFF", "CURSOR_HIDE",
    "CURSOR_SHOW", "CLEAR_SCREEN", "CLEAR_LINE", "RESET_SGR",
    "sgr", "fg256", "bg256", "cursor_to", "clear_line",
    "KEY_ENTER", "KEY_ESC", "KEY_TAB", "KEY_BACKSPACE", "KEY_DELETE",
    "KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT", "KEY_HOME", "KEY_END",
    "KEY_PAGEUP", "KEY_PAGEDOWN", "KEY_CTRL_C", "KEY_CTRL_L", "KEY_CTRL_O",
    "KEY_CTRL_U",
    "KeyDecoder", "Screen", "AnsiScreen", "HeadlessScreen",
    "InputSource", "RawInput", "FakeInput", "Terminal",
]


# ══════════════════════════════════════════════════════════════════════════
# ANSI escape helpers
# ══════════════════════════════════════════════════════════════════════════

ESC = "\x1b"
CSI = "\x1b["
ALT_SCREEN_ON = "\x1b[?1049h"
ALT_SCREEN_OFF = "\x1b[?1049l"
CURSOR_HIDE = "\x1b[?25l"
CURSOR_SHOW = "\x1b[?25h"
CLEAR_SCREEN = "\x1b[2J"
CLEAR_LINE = "\x1b[2K"
RESET_SGR = "\x1b[0m"


def sgr(*codes) -> str:
    """Build an SGR sequence (``\\x1b[<codes>m``)."""
    return "\x1b[" + ";".join(str(c) for c in codes) + "m"


def fg256(n: int) -> str:
    """256-color foreground SGR."""
    return "\x1b[38;5;%dm" % int(n)


def bg256(n: int) -> str:
    """256-color background SGR."""
    return "\x1b[48;5;%dm" % int(n)


def cursor_to(row: int, col: int) -> str:
    """Absolute cursor placement. ``row``/``col`` are 0-indexed internally,
    the emitted sequence is 1-indexed as terminals expect."""
    return "\x1b[%d;%dH" % (row + 1, col + 1)


def clear_line(row: int) -> str:
    """Move to ``row`` and erase the whole line."""
    return cursor_to(row, 0) + CLEAR_LINE


# ══════════════════════════════════════════════════════════════════════════
# Key tokens
# ══════════════════════════════════════════════════════════════════════════

KEY_ENTER = "enter"
KEY_ESC = "esc"
KEY_TAB = "tab"
KEY_BACKSPACE = "backspace"
KEY_DELETE = "delete"
KEY_UP = "up"
KEY_DOWN = "down"
KEY_LEFT = "left"
KEY_RIGHT = "right"
KEY_HOME = "home"
KEY_END = "end"
KEY_PAGEUP = "pageup"
KEY_PAGEDOWN = "pagedown"
KEY_INSERT = "insert"
KEY_CTRL_C = "ctrl+c"
KEY_CTRL_L = "ctrl+l"
KEY_CTRL_O = "ctrl+o"
KEY_CTRL_U = "ctrl+u"

# Final byte (after ESC [ params) → key token.
_CSI_FINAL = {
    "A": KEY_UP,
    "B": KEY_DOWN,
    "C": KEY_RIGHT,
    "D": KEY_LEFT,
    "H": KEY_HOME,
    "F": KEY_END,
    "P": KEY_DELETE,   # some terminals
}
# Full parameter+final sequences.
_CSI_SEQ = {
    "A": KEY_UP, "B": KEY_DOWN, "C": KEY_RIGHT, "D": KEY_LEFT,
    "H": KEY_HOME, "F": KEY_END,
    "1~": KEY_HOME, "2~": KEY_INSERT, "3~": KEY_DELETE,
    "4~": KEY_END, "5~": KEY_PAGEUP, "6~": KEY_PAGEDOWN,
    "7~": KEY_HOME, "8~": KEY_END,
}

_NEED_MORE = object()

# Windows ``msvcrt`` secondary codes (after a \x00 / \xe0 prefix).
_WIN_SPECIAL = {
    "H": KEY_UP, "P": KEY_DOWN, "K": KEY_LEFT, "M": KEY_RIGHT,
    "G": KEY_HOME, "O": KEY_END, "I": KEY_PAGEUP, "Q": KEY_PAGEDOWN,
    "R": KEY_INSERT, "S": KEY_DELETE,
}


def _utf8_len(b0: int) -> int:
    if b0 < 0x80:
        return 1
    if 0xC0 <= b0 < 0xE0:
        return 2
    if 0xE0 <= b0 < 0xF0:
        return 3
    if 0xF0 <= b0 < 0xF8:
        return 4
    return 0


def _control_key(b0: int) -> str:
    """Map a raw control byte to a ``ctrl+x`` token."""
    if b0 == 0:
        return "ctrl+@"
    if 1 <= b0 <= 26:
        return "ctrl+" + chr(ord("a") + b0 - 1)
    if b0 == 28:
        return "ctrl+\\"
    if b0 == 29:
        return "ctrl+]"
    if b0 == 30:
        return "ctrl+^"
    if b0 == 31:
        return "ctrl+_"
    return "ctrl+?"


class KeyDecoder:
    """Incremental byte-stream → key-token decoder.

    Bytes are pushed with :meth:`feed`; complete tokens are returned.  A lone
    trailing ``ESC`` is *not* emitted by ``feed`` (it might be the start of a
    longer sequence) — call :meth:`flush` to resolve it.  This split is what
    lets :class:`RawInput` distinguish a bare ``Esc`` keypress from an arrow
    key without a blocking read.
    """

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> List[str]:
        if data:
            self._buf += data
        out: List[str] = []
        while True:
            token = self._decode_one()
            if token is None or token is _NEED_MORE:
                break
            out.append(token)
        return out

    def flush(self) -> List[str]:
        out: List[str] = []
        if self._buf == b"\x1b":
            self._buf = b""
            out.append(KEY_ESC)
        return out

    def has_pending_escape(self) -> bool:
        return self._buf == b"\x1b"

    def clear_pending(self) -> None:
        self._buf = b""

    # ── internal ──
    def _decode_one(self):
        buf = self._buf
        if not buf:
            return None
        b0 = buf[0]

        if b0 == 0x1B:  # ESC
            if len(buf) == 1:
                return _NEED_MORE
            nxt = buf[1]
            if nxt == ord("["):
                j = 2
                while j < len(buf) and not (0x40 <= buf[j] <= 0x7E):
                    j += 1
                if j >= len(buf):
                    return _NEED_MORE
                params = buf[2:j].decode("latin1")
                final = chr(buf[j])
                self._buf = buf[j + 1:]
                token = _CSI_SEQ.get(params + final)
                if token is None:
                    token = _CSI_SEQ.get(final) or _CSI_FINAL.get(final)
                return token or KEY_ESC
            if nxt == ord("O"):  # SS3 (application cursor keys)
                if len(buf) < 3:
                    return _NEED_MORE
                final = chr(buf[2])
                self._buf = buf[3:]
                return _CSI_SEQ.get(final) or _CSI_FINAL.get(final) or KEY_ESC
            # ESC followed by something else: emit Esc, reprocess the rest.
            self._buf = buf[1:]
            return KEY_ESC

        if b0 in (10, 13):  # \n / \r
            self._buf = buf[1:]
            return KEY_ENTER
        if b0 == 9:  # \t
            self._buf = buf[1:]
            return KEY_TAB
        if b0 in (8, 127):  # ^H / DEL
            self._buf = buf[1:]
            return KEY_BACKSPACE
        if b0 < 32:
            self._buf = buf[1:]
            return _control_key(b0)
        if b0 < 0x80:
            self._buf = buf[1:]
            return chr(b0)

        ln = _utf8_len(b0)
        if ln == 0:
            self._buf = buf[1:]
            return None
        if len(buf) < ln:
            return _NEED_MORE
        try:
            ch = buf[:ln].decode("utf-8")
        except UnicodeDecodeError:
            self._buf = buf[1:]
            return None
        self._buf = buf[ln:]
        return ch


# ══════════════════════════════════════════════════════════════════════════
# Screens
# ══════════════════════════════════════════════════════════════════════════

class Screen:
    """Abstract screen surface.

    The UI only ever needs to replace whole rows, so the low-level contract is
    deliberately tiny.  Subclasses: :class:`AnsiScreen` (real terminal) and
    :class:`HeadlessScreen` (in-memory, for tests).
    """

    #: True for a real terminal (enables signal handling in :class:`Terminal`).
    is_real = False

    def __init__(self, columns: int = 80, rows: int = 24) -> None:
        self.columns = int(columns)
        self.rows = int(rows)

    def size(self) -> Tuple[int, int]:
        return (self.columns, self.rows)

    def setup(self) -> None:  # pragma: no cover - overridden
        pass

    def teardown(self) -> None:  # pragma: no cover - overridden
        pass

    def refresh_size(self) -> Tuple[int, int]:
        return self.size()

    def clear(self) -> None:  # pragma: no cover - overridden
        pass

    def write_line(self, row: int, text: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def move_cursor(self, row: int, col: int) -> None:  # pragma: no cover
        pass

    def hide_cursor(self) -> None:  # pragma: no cover
        pass

    def show_cursor(self) -> None:  # pragma: no cover
        pass

    def flush(self) -> None:  # pragma: no cover
        pass


def _query_size() -> Tuple[int, int]:
    try:
        sz = os.get_terminal_size(sys.stdout.fileno())
        return (sz.columns, sz.lines)
    except (OSError, ValueError, AttributeError):
        sz = shutil.get_terminal_size((80, 24))
        return (sz.columns, sz.lines)


def _enable_vt_processing() -> None:
    """Turn on ``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` for stdout/stderr.

    Needed on Windows 10+ conhost so ANSI escapes actually move the cursor
    instead of printing garbage.  Silently no-ops on non-Windows or when the
    API is unavailable (very old Windows)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        enable_processed = 0x0001
        enable_vt = 0x0004
        for std_handle in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(std_handle)
            if handle in (0, -1):
                continue
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(
                    handle, mode.value | enable_processed | enable_vt
                )
    except Exception:
        return


class AnsiScreen(Screen):
    """Real-terminal screen backed by ``sys.stdout``."""

    is_real = True

    def __init__(self, stream=None) -> None:
        cols, rows = _query_size()
        super().__init__(cols, rows)
        self._stream = stream if stream is not None else sys.stdout

    # ── size ──
    def refresh_size(self) -> Tuple[int, int]:
        self.columns, self.rows = _query_size()
        return (self.columns, self.rows)

    # ── lifecycle ──
    def setup(self) -> None:
        _enable_vt_processing()
        self.refresh_size()
        self._write(ALT_SCREEN_ON + CLEAR_SCREEN + CURSOR_HIDE + cursor_to(0, 0))
        self.flush()

    def teardown(self) -> None:
        self._write(RESET_SGR + CURSOR_SHOW + ALT_SCREEN_OFF)
        self.flush()

    # ── drawing ──
    def clear(self) -> None:
        self._write(CLEAR_SCREEN + cursor_to(0, 0))

    def write_line(self, row: int, text: str) -> None:
        self._write(clear_line(row) + text)

    def move_cursor(self, row: int, col: int) -> None:
        self._write(cursor_to(row, col))

    def hide_cursor(self) -> None:
        self._write(CURSOR_HIDE)

    def show_cursor(self) -> None:
        self._write(CURSOR_SHOW)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except (OSError, ValueError):
            pass

    def _write(self, text: str) -> None:
        try:
            self._stream.write(text)
        except (OSError, ValueError):
            pass


class HeadlessScreen(Screen):
    """In-memory screen for tests.

    Interprets exactly the operations the UI emits (:meth:`write_line`,
    :meth:`move_cursor`, :meth:`clear`) so tests can assert on rendered rows and
    on *how many* rows a frame actually rewrote (differential redraw).

    Every operation is also recorded in :attr:`ops` as a tuple so assertions
    like ``screen.count_ops("line", frame=…)`` are possible.
    """

    def __init__(self, columns: int = 80, rows: int = 24) -> None:
        super().__init__(columns, rows)
        self._grid: List[str] = [""] * self.rows
        self.ops: List[tuple] = []
        self.cursor: Tuple[int, int] = (0, 0)
        self.cursor_visible = True
        self.active = False

    # ── lifecycle ──
    def setup(self) -> None:
        self.active = True
        self.ops.append(("setup",))

    def teardown(self) -> None:
        self.active = False
        self.ops.append(("teardown",))

    # ── drawing ──
    def clear(self) -> None:
        self._grid = [""] * self.rows
        self.ops.append(("clear",))

    def write_line(self, row: int, text: str) -> None:
        if 0 <= row < self.rows:
            self._grid[row] = text
        self.ops.append(("line", row, text))

    def move_cursor(self, row: int, col: int) -> None:
        self.cursor = (row, col)
        self.ops.append(("cursor", row, col))

    def hide_cursor(self) -> None:
        self.cursor_visible = False
        self.ops.append(("hide",))

    def show_cursor(self) -> None:
        self.cursor_visible = True
        self.ops.append(("show",))

    def flush(self) -> None:
        self.ops.append(("flush",))

    # ── test helpers ──
    def resize(self, columns: int, rows: int) -> None:
        old = self._grid
        self.columns = int(columns)
        self.rows = int(rows)
        self._grid = [""] * self.rows
        for i in range(min(len(old), self.rows)):
            self._grid[i] = old[i]
        self.ops.append(("resize", self.columns, self.rows))

    def line(self, row: int) -> str:
        if 0 <= row < self.rows:
            return self._grid[row]
        return ""

    def render(self) -> List[str]:
        """Current grid as a list of rows (ANSI codes still present)."""
        return list(self._grid)

    def text(self) -> str:
        return "\n".join(self._grid)

    def clear_ops(self) -> None:
        self.ops = []

    def count_ops(self, kind: str) -> int:
        return sum(1 for op in self.ops if op[0] == kind)

    def written_rows(self) -> List[int]:
        return [op[1] for op in self.ops if op[0] == "line"]


# ══════════════════════════════════════════════════════════════════════════
# Input sources
# ══════════════════════════════════════════════════════════════════════════

class InputSource:
    """Abstract key source.  ``read`` returns a key token or ``None`` on
    timeout / exhaustion."""

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def read(self, timeout: Optional[float] = None) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError

    def push(self, key: str) -> None:  # pragma: no cover
        raise NotImplementedError


class FakeInput(InputSource):
    """Scripted key source used by tests (the headless injection point)."""

    def __init__(self, keys=None) -> None:
        self._keys: List[str] = list(keys or [])
        self.closed = False
        self.reads: List[str] = []

    def open(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def read(self, timeout: Optional[float] = None) -> Optional[str]:
        if self._keys:
            key = self._keys.pop(0)
            self.reads.append(key)
            return key
        return None

    def push(self, key: str) -> None:
        self._keys.append(key)

    def exhausted(self) -> bool:
        return not self._keys


class RawInput(InputSource):
    """Real keyboard input.

    Unix: raw mode via ``termios`` + ``tty``, non-blocking reads via
    ``select``.  Windows: ``msvcrt`` with processed-input disabled so that
    ``Ctrl+C`` is delivered as a key rather than killing the process.
    """

    def __init__(self, stream=None) -> None:
        self._stream = stream if stream is not None else sys.stdin
        self._decoder = KeyDecoder()
        self._pending: List[str] = []
        self._fd: Optional[int] = None
        self._old_termios = None
        self._eof = False
        self._is_windows = os.name == "nt"

    # ── lifecycle ──
    def open(self) -> None:
        if self._is_windows:
            self._win_enable_raw_input()
            return
        try:
            import termios  # noqa: F401 (imported lazily: Windows import safety)

            import tty

            self._fd = self._stream.fileno()
            self._old_termios = termios.tcgetattr(self._fd)
            tty.setraw(self._fd)
        except Exception:
            # Not a TTY (e.g. piped input).  Fall back to line-ish reads.
            self._fd = None
            self._old_termios = None
            try:
                self._fd = self._stream.fileno()
            except Exception:
                self._fd = None

    def close(self) -> None:
        if self._is_windows:
            return
        if self._old_termios is not None:
            try:
                import termios

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_termios)
            except Exception:
                pass
            self._old_termios = None

    # ── reading ──
    def read(self, timeout: Optional[float] = None) -> Optional[str]:
        if self._is_windows:
            return self._read_windows(timeout)
        return self._read_unix(timeout)

    def push(self, key: str) -> None:
        # RawInput has no queue; expose for interface compatibility.
        raise NotImplementedError("RawInput does not support push()")

    # ── Unix ──
    def _read_unix(self, timeout: Optional[float]) -> Optional[str]:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._pending:
                return self._pending.pop(0)
            token = self._decoder._decode_one()
            if token is not None and token is not _NEED_MORE:
                return token

            need_more = token is _NEED_MORE
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                return self._resolve_pending()

            if need_more:
                wait = 0.02
                if deadline is not None:
                    wait = min(wait, max(0.0, deadline - now))
            else:
                wait = None if deadline is None else max(0.0, deadline - now)

            if not self._select_read(wait):
                if self._eof:
                    return self._resolve_pending()
                if deadline is not None and time.monotonic() >= deadline:
                    return self._resolve_pending()
                if need_more and self._decoder.has_pending_escape():
                    # A bare ESC that never grew into a sequence.
                    self._decoder.clear_pending()
                    return KEY_ESC
                continue

    def _resolve_pending(self) -> Optional[str]:
        if self._decoder.has_pending_escape():
            self._decoder.clear_pending()
            return KEY_ESC
        return None

    def _select_read(self, wait: Optional[float]) -> bool:
        if self._fd is None:
            # No fileno: best-effort blocking single char.
            try:
                ch = self._stream.read(1)
            except Exception:
                return False
            if not ch:
                self._eof = True
                return False
            self._pending.extend(self._decoder.feed(ch.encode("utf-8", "ignore")))
            return True
        try:
            import select

            ready, _, _ = select.select([self._fd], [], [], wait)
        except (OSError, ValueError):
            self._eof = True
            return False
        if not ready:
            return False
        try:
            data = os.read(self._fd, 64)
        except OSError:
            self._eof = True
            return False
        if not data:
            self._eof = True
            return False
        self._pending.extend(self._decoder.feed(data))
        return True

    # ── Windows ──
    def _win_enable_raw_input(self) -> None:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                enable_processed_input = 0x0001
                kernel32.SetConsoleMode(handle, mode.value & ~enable_processed_input)
        except Exception:
            pass

    def _read_windows(self, timeout: Optional[float]) -> Optional[str]:
        try:
            import msvcrt
        except Exception:
            return None
        if timeout is None:
            ch = msvcrt.getwch()
        else:
            deadline = time.monotonic() + timeout
            while not msvcrt.kbhit():
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.01)
            ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            return _WIN_SPECIAL.get(code, KEY_ESC)
        if ch == "\x1b":
            return KEY_ESC
        if ch == "\r":
            return KEY_ENTER
        if ch == "\t":
            return KEY_TAB
        if ch == "\b":
            return KEY_BACKSPACE
        if ch == "\x03":
            return KEY_CTRL_C
        if len(ch) == 1 and ord(ch) < 32:
            return _control_key(ord(ch))
        return ch


# ══════════════════════════════════════════════════════════════════════════
# Terminal facade
# ══════════════════════════════════════════════════════════════════════════

class Terminal:
    """Bundles a :class:`Screen` and an :class:`InputSource` plus resize
    signalling.  Headless tests build one with :class:`HeadlessScreen` +
    :class:`FakeInput`; the app builds the default (real) one."""

    def __init__(self, screen: Optional[Screen] = None,
                 input_source: Optional[InputSource] = None) -> None:
        self.screen = screen if screen is not None else AnsiScreen()
        self.input = input_source if input_source is not None else RawInput()
        self._entered = False
        self._resized = False
        self._resize_cb: Optional[Callable[[], None]] = None
        self._prev_winch = None

    # ── lifecycle ──
    def enter(self) -> None:
        if self._entered:
            return
        self._entered = True
        self.screen.setup()
        self.input.open()
        if self.screen.is_real and os.name != "nt":
            self._install_resize()

    def leave(self) -> None:
        if not self._entered:
            return
        self._entered = False
        if self.screen.is_real and os.name != "nt":
            self._restore_resize()
        self.input.close()
        self.screen.teardown()

    # ── input ──
    def read_key(self, timeout: Optional[float] = None) -> Optional[str]:
        return self.input.read(timeout)

    # ── size / resize ──
    def size(self) -> Tuple[int, int]:
        return self.screen.size()

    def on_resize(self, callback: Callable[[], None]) -> None:
        self._resize_cb = callback
        if self.screen.is_real and os.name != "nt" and self._entered:
            self._install_resize()

    def resize_pending(self) -> bool:
        flag = self._resized
        self._resized = False
        return flag

    def _install_resize(self) -> None:
        sigwinch = getattr(signal, "SIGWINCH", None)
        if sigwinch is None:
            return
        try:
            self._prev_winch = signal.signal(sigwinch, self._handle_winch)
        except (ValueError, OSError):
            self._prev_winch = None

    def _restore_resize(self) -> None:
        sigwinch = getattr(signal, "SIGWINCH", None)
        if sigwinch is None or self._prev_winch is None:
            return
        try:
            signal.signal(sigwinch, self._prev_winch)
        except (ValueError, OSError):
            pass
        self._prev_winch = None

    def _handle_winch(self, signum, frame) -> None:  # pragma: no cover - signal
        self._resized = True
        try:
            self.screen.refresh_size()
        except Exception:
            pass
        cb = self._resize_cb
        if cb is not None:
            try:
                cb()
            except Exception:
                pass
