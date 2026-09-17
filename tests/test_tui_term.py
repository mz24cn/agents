"""Headless unit tests for :mod:`tui.term`.

No real TTY is ever touched: screens are in-memory and input is scripted.
"""

import io
import os
import sys

import pytest

from tui import term


# ══════════════════════════════════════════════════════════════════════════
# ANSI helpers
# ══════════════════════════════════════════════════════════════════════════

def test_ansi_constants_and_helpers():
    assert term.ALT_SCREEN_ON == "\x1b[?1049h"
    assert term.ALT_SCREEN_OFF == "\x1b[?1049l"
    assert term.cursor_to(0, 0) == "\x1b[1;1H"
    assert term.cursor_to(3, 5) == "\x1b[4;6H"
    assert term.clear_line(2) == "\x1b[3;1H" + term.CLEAR_LINE
    assert term.fg256(75) == "\x1b[38;5;75m"
    assert term.bg256(240) == "\x1b[48;5;240m"
    assert term.sgr(1, 31) == "\x1b[1;31m"


def test_term_module_does_not_import_termios_at_top_level():
    """Importing tui.term on Windows must not explode: termios/tty are lazy."""
    import inspect

    source = inspect.getsource(term)
    for line in source.splitlines():
        if line != line.lstrip():  # indented = inside a function, allowed
            continue
        assert not line.startswith("import termios")
        assert not line.startswith("from termios")
        assert not line.startswith("import tty")


# ══════════════════════════════════════════════════════════════════════════
# KeyDecoder
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("data,expected", [
    (b"a", ["a"]),
    (b"ab", ["a", "b"]),
    (b"\r", [term.KEY_ENTER]),
    (b"\n", [term.KEY_ENTER]),
    (b"\t", [term.KEY_TAB]),
    (b"\x7f", [term.KEY_BACKSPACE]),
    (b"\x08", [term.KEY_BACKSPACE]),
    (b"\x03", [term.KEY_CTRL_C]),
    (b"\x04", ["ctrl+d"]),  # ^D is a plain control byte now, never EOF/exit
    (b"\x0c", [term.KEY_CTRL_L]),
    (b"\x0f", [term.KEY_CTRL_O]),
    (b"\x15", [term.KEY_CTRL_U]),
    (b"\x1b[A", [term.KEY_UP]),
    (b"\x1b[B", [term.KEY_DOWN]),
    (b"\x1b[C", [term.KEY_RIGHT]),
    (b"\x1b[D", [term.KEY_LEFT]),
    (b"\x1b[5~", [term.KEY_PAGEUP]),
    (b"\x1b[6~", [term.KEY_PAGEDOWN]),
    (b"\x1b[3~", [term.KEY_DELETE]),
    (b"\x1b[H", [term.KEY_HOME]),
    (b"\x1b[F", [term.KEY_END]),
    (b"\x1bOA", [term.KEY_UP]),
    ("中".encode("utf-8"), ["中"]),
    ("🛠".encode("utf-8"), ["🛠"]),
])
def test_key_decoder_sequences(data, expected):
    assert term.KeyDecoder().feed(data) == expected


def test_key_decoder_incremental_escape_sequence():
    decoder = term.KeyDecoder()
    assert decoder.feed(b"\x1b") == []          # incomplete: could be an arrow
    assert decoder.has_pending_escape()
    assert decoder.feed(b"[A") == [term.KEY_UP]


def test_key_decoder_lone_esc_requires_flush():
    decoder = term.KeyDecoder()
    assert decoder.feed(b"\x1b") == []
    assert decoder.flush() == [term.KEY_ESC]


def test_key_decoder_multibyte_split():
    decoder = term.KeyDecoder()
    raw = "中".encode("utf-8")
    assert decoder.feed(raw[:1]) == []
    assert decoder.feed(raw[1:]) == ["中"]


# ══════════════════════════════════════════════════════════════════════════
# HeadlessScreen
# ══════════════════════════════════════════════════════════════════════════

def test_headless_screen_write_and_render():
    screen = term.HeadlessScreen(20, 5)
    screen.setup()
    assert screen.active
    screen.write_line(0, "hello")
    screen.write_line(3, "world")
    assert screen.line(0) == "hello"
    assert screen.line(3) == "world"
    assert screen.render() == ["hello", "", "", "world", ""]
    screen.teardown()
    assert not screen.active


def test_headless_screen_clear_and_cursor():
    screen = term.HeadlessScreen(10, 3)
    screen.write_line(1, "x")
    screen.move_cursor(1, 2)
    screen.hide_cursor()
    assert screen.cursor == (1, 2)
    assert screen.cursor_visible is False
    screen.clear()
    assert screen.render() == ["", "", ""]
    assert screen.cursor_visible is False


def test_headless_screen_resize_preserves_rows():
    screen = term.HeadlessScreen(10, 3)
    screen.write_line(0, "a")
    screen.write_line(1, "b")
    screen.resize(20, 4)
    assert screen.size() == (20, 4)
    assert screen.line(0) == "a"
    assert screen.line(1) == "b"
    assert screen.line(3) == ""


def test_headless_screen_records_ops():
    screen = term.HeadlessScreen(5, 3)
    screen.write_line(0, "a")
    screen.write_line(2, "b")
    screen.flush()
    assert screen.count_ops("line") == 2
    assert screen.written_rows() == [0, 2]
    assert screen.count_ops("flush") == 1


# ══════════════════════════════════════════════════════════════════════════
# Input sources / Terminal
# ══════════════════════════════════════════════════════════════════════════

def test_fake_input_scripted():
    source = term.FakeInput(["a", "b"])
    assert source.read() == "a"
    assert source.read() == "b"
    assert source.read() is None
    assert source.exhausted()
    source.push("c")
    assert not source.exhausted()
    assert source.read() == "c"


def test_terminal_headless_lifecycle_and_read():
    screen = term.HeadlessScreen(10, 5)
    source = term.FakeInput(["x", term.KEY_ENTER])
    terminal = term.Terminal(screen=screen, input_source=source)
    terminal.enter()
    assert screen.active
    assert terminal.size() == (10, 5)
    assert terminal.read_key() == "x"
    assert terminal.read_key() == term.KEY_ENTER
    assert terminal.read_key() is None
    terminal.leave()
    assert not screen.active
    assert source.closed


def test_terminal_leave_is_idempotent():
    screen = term.HeadlessScreen(10, 5)
    terminal = term.Terminal(screen=screen, input_source=term.FakeInput([]))
    terminal.enter()
    terminal.leave()
    terminal.leave()  # must not raise
    assert not screen.active


def test_raw_input_unix_reads_scripted_bytes():
    """RawInput parsing itself is exercised through a pipe (no TTY needed)."""
    r, w = os.pipe()
    source = term.RawInput(stream=sys.stdin)
    source._is_windows = False
    source._fd = r
    source._old_termios = None
    try:
        os.write(w, b"\x1b[B")
        assert source.read(timeout=1.0) == term.KEY_DOWN
        os.write(w, b"ab")
        assert source.read(timeout=1.0) == "a"
        assert source.read(timeout=1.0) == "b"
    finally:
        os.close(w)
        os.close(r)
