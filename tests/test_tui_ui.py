"""Headless unit tests for :mod:`tui.ui` and the :mod:`tui.app` main loop.

Everything runs against in-memory screens and fake input — no TTY, no network.
"""

import io
import unicodedata
from dataclasses import dataclass
from typing import List, Optional

import pytest

from tui import app as app_mod
from tui import term as term_mod
from tui import ui as ui_mod


# ══════════════════════════════════════════════════════════════════════════
# Width-aware text helpers
# ══════════════════════════════════════════════════════════════════════════

def test_char_width_cjk_and_ascii():
    assert ui_mod.char_width("a") == 1
    assert ui_mod.char_width("中") == 2
    assert ui_mod.char_width("Ｗ") == 2  # fullwidth
    assert ui_mod.display_width("a中b") == 4
    assert ui_mod.display_width("中文") == 4


def test_east_asian_width_emoji():
    # Width follows the contract rule (W/F -> 2, else 1).  Note: per the
    # Unicode version bundled with CPython, some pictographs (e.g. U+1F6E0
    # 🛠) report 'N' and therefore count as width 1 — the rule is applied
    # verbatim, not "whatever looks wide".
    assert ui_mod.display_width("🔧") == 2   # U+1F527, 'W'
    assert ui_mod.display_width("✅") == 2   # U+2705, 'W'
    assert ui_mod.display_width("🛠") == 1   # U+1F6E0, 'N' in this unicodedata


def test_wrap_text_by_display_width_not_len():
    assert ui_mod.wrap_text("中中中中中", 4) == ["中中", "中中", "中"]
    assert ui_mod.wrap_line("abcdef", 4) == ["abcd", "ef"]
    assert ui_mod.wrap_text("ab\ncd", 10) == ["ab", "cd"]
    # A double-width char never straddles the boundary.
    assert ui_mod.wrap_line("a中", 2) == ["a", "中"]


def test_truncate_and_pad():
    assert ui_mod.truncate_to_width("中文字", 5) == "中文…"
    assert ui_mod.truncate_to_width("abc", 5) == "abc"
    assert ui_mod.visible_width(ui_mod.pad_right("中", 4)) == 4
    assert ui_mod.strip_ansi("\x1b[31mred\x1b[0m") == "red"


def test_tunnel_badge_variants():
    # No status at all -> no badge (the old "独立" fallback is gone).
    assert ui_mod.tunnel_badge(None) == ""
    assert ui_mod.tunnel_badge({}) == ""
    # configured but the dial flag is off (the user's real-world case): silent.
    assert ui_mod.tunnel_badge(
        {"configured": True, "enabled": False, "state": "idle"}
    ) == ""
    # enabled but no parent address configured: silent too.
    assert ui_mod.tunnel_badge(
        {"configured": False, "enabled": True, "state": "online"}
    ) == ""
    # enabled + configured: the state decides online vs offline.
    online_states = sorted(ui_mod._TUNNEL_ONLINE_STATES) or ["online"]
    for state in online_states:
        assert ui_mod.tunnel_badge(
            {"configured": True, "enabled": True, "state": state}
        ) == "已注册->母端(在线)"
    assert ui_mod.tunnel_badge(
        {"configured": True, "enabled": True, "state": "idle"}
    ) == "已注册->母端(离线)"
    assert ui_mod.tunnel_badge(
        {"configured": True, "enabled": True, "state": "connecting"}
    ) == "已注册->母端(离线)"


# ══════════════════════════════════════════════════════════════════════════
# Differential redraw
# ══════════════════════════════════════════════════════════════════════════

def test_frame_writer_only_rewrites_changed_rows():
    screen = term_mod.HeadlessScreen(20, 4)
    writer = ui_mod.FrameWriter(screen)

    assert writer.draw(["a", "b", "c", "d"]) == 4
    screen.clear_ops()
    assert writer.draw(["a", "b", "c", "d"]) == 0
    screen.clear_ops()
    assert writer.draw(["a", "X", "c", "d"]) == 1
    screen.clear_ops()
    writer.invalidate()
    assert writer.draw(["a", "X", "c", "d"]) == 4


def test_frame_writer_rewrites_all_when_row_count_changes():
    screen = term_mod.HeadlessScreen(20, 4)
    writer = ui_mod.FrameWriter(screen)
    writer.draw(["a", "b", "c"])
    screen.clear_ops()
    assert writer.draw(["a", "b", "c", "d"]) == 4


# ══════════════════════════════════════════════════════════════════════════
# Transcript view
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Block:
    kind: str
    lines: List[str]
    full_lines: Optional[List[str]] = None
    badge: Optional[str] = None
    detail: str = ""


def _plain(lines):
    return [ui_mod.strip_ansi(l) for l in lines]


def test_transcript_colors_and_renders_kinds():
    blocks = [
        Block("user", ["你好"]),
        Block("assistant", ["世界"]),
        Block("error", ["炸了"]),
    ]
    view = ui_mod.TranscriptView(lambda: blocks)
    lines = view.rendered_lines(40)
    assert _plain(lines) == ["你好", "世界", "炸了"]
    assert lines[0].startswith(ui_mod.COLOR_256["user"])
    assert not lines[1].startswith("\x1b[38;5;")  # assistant = default
    assert lines[2].startswith(ui_mod.COLOR_256["error"])


def test_transcript_wraps_cjk_by_width():
    view = ui_mod.TranscriptView(lambda: [Block("assistant", ["中文中文中文"])])
    assert _plain(view.rendered_lines(4)) == ["中文", "中文", "中文"]


def test_tool_block_single_line_and_expand_collapse():
    tool = Block("tool", ["▸ 🛠 df / ✓"], full_lines=["line1", "line2"],
                 badge="▸ 🛠 df / ✓", detail="2 行")
    view = ui_mod.TranscriptView(lambda: [tool])
    assert _plain(view.rendered_lines(40)) == ["▸ 🛠 df / ✓"]

    assert view.toggle_last_tool() is True
    expanded = _plain(view.rendered_lines(40))
    assert expanded[0] == "▸ 🛠 df / ✓"
    assert "line1" in expanded[1]
    assert "line2" in expanded[2]
    assert "2 行" in expanded[3]

    assert view.toggle_last_tool() is True
    assert _plain(view.rendered_lines(40)) == ["▸ 🛠 df / ✓"]


def test_toggle_last_tool_without_tool_returns_false():
    view = ui_mod.TranscriptView(lambda: [Block("assistant", ["hi"])])
    assert view.toggle_last_tool() is False


def test_transcript_scrollback_clamps_and_pins():
    blocks = [Block("assistant", [f"line{i}"]) for i in range(10)]
    view = ui_mod.TranscriptView(lambda: blocks)
    window = view.render(40, 3)
    assert _plain(window) == ["line7", "line8", "line9"]
    view.scroll_by(2)
    assert _plain(view.render(40, 3)) == ["line5", "line6", "line7"]
    view.scroll_by(999)
    assert _plain(view.render(40, 3)) == ["line0", "line1", "line2"]
    view.scroll_to_bottom()
    assert _plain(view.render(40, 3)) == ["line7", "line8", "line9"]


def test_transcript_top_aligns_short_content():
    view = ui_mod.TranscriptView(lambda: [Block("assistant", ["only"])])
    assert _plain(view.render(40, 3)) == ["only", "", ""]


# ══════════════════════════════════════════════════════════════════════════
# Input line
# ══════════════════════════════════════════════════════════════════════════

def test_input_line_editing():
    line = ui_mod.InputLine()
    line.insert("abc")
    assert line.text() == "abc"
    line.left()
    line.backspace()
    assert line.text() == "ac"
    line.end()
    line.insert("d")
    assert line.text() == "acd"
    line.home()
    line.delete()
    assert line.text() == "cd"
    line.clear()
    assert line.text() == ""
    assert not line.is_command()


def test_input_line_command_detection_and_cursor_column():
    line = ui_mod.InputLine()
    line.set_text("中文")
    line.home()
    line.insert("你")
    assert line.is_command() is False
    text, col = line.render(20)
    assert ui_mod.strip_ansi(text) == "› 你中文"
    assert col == 2 + ui_mod.display_width("你")

    line.set_text("/help")
    assert line.is_command() is True
    assert ui_mod.strip_ansi(line.render(20)[0]) == "› /help"


def test_input_line_history():
    line = ui_mod.InputLine()
    line.push_history("one")
    line.push_history("two")
    line.history_prev()
    assert line.text() == "two"
    line.history_prev()
    assert line.text() == "one"
    line.history_prev()
    assert line.text() == "one"
    line.history_next()
    assert line.text() == "two"
    line.history_next()
    assert line.text() == ""


def test_input_line_horizontal_scroll_keeps_cursor_visible():
    line = ui_mod.InputLine()
    line.set_text("abcdefghij")
    text, col = line.render(6, prompt="› ")
    assert col <= 6
    assert "j" in ui_mod.strip_ansi(text)


# ══════════════════════════════════════════════════════════════════════════
# Modal components
# ══════════════════════════════════════════════════════════════════════════

def test_selector_navigation_wraps_and_renders():
    items = [ui_mod.SelectorItem("a", "1"), ui_mod.SelectorItem("b", "2"),
             ui_mod.SelectorItem("c", "3")]
    sel = ui_mod.Selector("标题", items)
    assert sel.selected().name == "a"
    sel.move(1)
    assert sel.selected().name == "b"
    sel.move(-1)
    sel.move(-1)
    assert sel.selected().name == "c"  # wrapped
    lines = _plain(sel.render(20, 5))
    assert "标题" in lines[0]
    assert any(line.startswith("> c") for line in lines)


def test_text_page_scroll():
    page = ui_mod.TextPage("帮助", [f"l{i}" for i in range(10)])
    lines = _plain(page.render(20, 4))
    assert lines[0] == "帮助"
    assert lines[1] == "l0"
    page.page(1, 4)
    lines = _plain(page.render(20, 4))
    assert lines[1] == "l3"


# ══════════════════════════════════════════════════════════════════════════
# TuiUI composition
# ══════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════
# Title bar — web ChatPage selection-bar order (model … agent), ASCII ellipses
# ══════════════════════════════════════════════════════════════════════════

_TITLEBAR_WIDTHS = [40, 60, 80, 120, 200]

_LONG_MODEL = "gpt-5-super-long-model-name"
_LONG_WS = "/home/agent/workspace/very/long/nested/project"

_TITLEBAR_COMBOS = [
    dict(model=_LONG_MODEL, agent="小莲", badge="已注册->母端(在线)",
         title="一次很长的会话标题内容", tool_count=3, workspace=_LONG_WS),
    dict(model="gpt", agent="小莲", badge="独立", title="会话",
         tool_count=1, workspace="/ws"),
    dict(model="", agent="小莲", badge="", title="", tool_count=None,
         workspace=""),
    dict(model="gpt", agent="", badge="独立", title="会话",
         tool_count=2, workspace="/ws"),
    dict(model="gpt", agent="小莲", badge="", title="",
         tool_count=None, workspace=_LONG_WS),
    dict(model=_LONG_MODEL, agent="", badge="", title="t",
         tool_count=0, workspace=""),
]


@pytest.mark.parametrize("width", _TITLEBAR_WIDTHS)
@pytest.mark.parametrize("combo", _TITLEBAR_COMBOS)
def test_title_bar_never_exceeds_width(width, combo):
    rendered = ui_mod.TitleBar().render(width, **combo)
    assert ui_mod.visible_width(rendered) <= width
    assert "\n" not in ui_mod.strip_ansi(rendered)


def test_title_bar_model_left_agent_right_and_middle_order():
    rendered = ui_mod.strip_ansi(ui_mod.TitleBar().render(
        200, model="gpt-5", agent="小莲", badge="独立", title="会话标题",
        tool_count=3, workspace="/home/agent/ws",
    ))
    assert rendered.startswith("gpt-5 ")
    assert rendered.endswith("独立 小莲")             # badge then agent, right-aligned
    assert ui_mod.display_width(rendered) == 200
    # middle order: 工具 -> workspace -> title
    assert (rendered.index("工具:3")
            < rendered.index("/home/agent/ws")
            < rendered.index("会话标题"))


def test_title_bar_shrink_drops_middle_then_badge_then_truncates_model():
    bar = ui_mod.TitleBar()
    combo = dict(model="MODEL", agent="AGENT", badge="BADGE", title="TITLE",
                 tool_count=7, workspace="WS")
    full = ui_mod.strip_ansi(bar.render(40, **combo))
    assert "TITLE" in full and "WS" in full and "工具:7" in full

    no_title = ui_mod.strip_ansi(bar.render(30, **combo))
    assert "TITLE" not in no_title
    assert "WS" in no_title and "工具:7" in no_title

    no_ws = ui_mod.strip_ansi(bar.render(26, **combo))
    assert "TITLE" not in no_ws and "WS" not in no_ws
    assert "工具:7" in no_ws

    no_tools = ui_mod.strip_ansi(bar.render(23, **combo))
    assert "工具:7" not in no_tools
    assert no_tools.startswith("MODEL")
    assert "BADGE" in no_tools and no_tools.endswith("AGENT")

    no_badge = ui_mod.strip_ansi(bar.render(16, **combo))
    assert "BADGE" not in no_badge
    assert no_badge.startswith("MODEL") and no_badge.endswith("AGENT")

    truncated = ui_mod.strip_ansi(bar.render(10, **combo))
    assert truncated == "M... AGENT"                  # model gives ground last
    assert "\u2026" not in truncated                  # ASCII ellipsis only


def test_title_bar_left_truncates_workspace_with_ascii_prefix():
    rendered = ui_mod.strip_ansi(ui_mod.TitleBar().render(
        12, model="M", agent="A", badge="", title="", tool_count=None,
        workspace="abcdefghijklmnop",
    ))
    assert rendered == "M ...lmnop A"                 # tail kept, ASCII prefix
    assert "\u2026" not in rendered


def test_title_bar_empty_content_is_empty_string():
    assert ui_mod.TitleBar().render(40, "", "", "", "", None, "") == ""


def test_tuiui_compose_layout_and_differential():
    screen = term_mod.HeadlessScreen(30, 6)
    tui = ui_mod.TuiUI(screen)
    blocks = [Block("user", ["a"])]
    tui.transcript.set_provider(lambda: blocks)
    tui.title = "会话"
    tui.agent_name = "小莲"
    tui.model_name = "gpt"

    assert tui.draw() == 6
    assert "会话" in ui_mod.strip_ansi(screen.line(0))
    assert "小莲" in ui_mod.strip_ansi(screen.line(0))
    # At width 30 the adaptive status bar keeps the highest-priority hint
    # segment ([Ctrl+C]) and the state text, dropping the rest.
    status = ui_mod.strip_ansi(screen.line(5))
    assert "中断/退出" in status
    assert status.endswith("推理:空闲")

    screen.clear_ops()
    assert tui.draw() == 0  # nothing changed

    blocks.append(Block("assistant", ["b"]))
    screen.clear_ops()
    assert tui.draw() == 1  # only the transcript row changed

    # cursor placed on the input row
    assert screen.cursor[0] == 4


def test_tuiui_modal_replaces_transcript_area():
    screen = term_mod.HeadlessScreen(30, 8)
    tui = ui_mod.TuiUI(screen)
    tui.transcript.set_provider(lambda: [Block("assistant", ["body"])])
    tui.modal = ui_mod.TextPage("帮助", ["x", "y"])
    tui.draw()
    rendered = "\n".join(_plain(screen.render()))
    assert "帮助" in rendered
    assert "x" in rendered
    assert "body" not in rendered
    assert screen.cursor_visible is False


# ══════════════════════════════════════════════════════════════════════════
# Plain backend
# ══════════════════════════════════════════════════════════════════════════

def test_plain_backend_prints_log_style_without_ansi():
    out = io.StringIO()
    plain = ui_mod.PlainUI(out=out)
    plain.sync([
        Block("user", ["hi"]),
        Block("assistant", ["你好，世界"]),
        Block("tool", ["▸ 🛠 df ✓"], badge="▸ 🛠 df ✓"),
        Block("error", ["连接失败"]),
    ])
    text = out.getvalue()
    assert "你> hi" in text
    assert "你好，世界" in text
    assert "▸ 🛠 df ✓" in text
    assert "连接失败" in text
    assert "\x1b" not in text


def test_plain_backend_reprints_updated_block():
    out = io.StringIO()
    plain = ui_mod.PlainUI(out=out)
    blocks = [Block("tool", ["▸ 🛠 df …"], badge="▸ 🛠 df …")]
    plain.sync(blocks)
    assert out.getvalue().count("df") == 1
    blocks[0] = Block("tool", ["▸ 🛠 df ✓"], badge="▸ 🛠 df ✓")
    plain.sync(blocks)
    assert out.getvalue().count("df") == 2


# ══════════════════════════════════════════════════════════════════════════
# App main loop — stubs
# ══════════════════════════════════════════════════════════════════════════

class StubTranscript:
    """Minimal stand-in for tui.render.Transcript."""

    def __init__(self):
        self._blocks: List[Block] = []
        self._session = None
        self._usage = None

    def append_frame(self, event, data):
        data = data or {}
        if event == "init":
            self._session = data.get("session_id")
        elif event == "message":
            role = data.get("role")
            content = data.get("content", "")
            if role == "user":
                self._blocks.append(Block("user", content.split("\n")))
            elif role == "assistant":
                self._blocks.append(Block("assistant", content.split("\n")))
            elif role == "tool":
                badge = f"▸ 🛠 {data.get('tool_id', 'tool')} ✓"
                self._blocks.append(Block("tool", [badge],
                                          full_lines=content.split("\n"),
                                          badge=badge))
            elif role == "system":
                self._blocks.append(Block("system", content.split("\n")))
        elif event == "usage":
            self._usage = data
            self._blocks.append(Block("usage", ["⌁ ↑1 ↓2 3 tok · 0.1s"]))
        elif event == "error":
            self._blocks.append(Block("error", [data.get("message", "error")]))

    def load_conversation(self, data):
        self._blocks = []

    def blocks(self):
        return list(self._blocks)

    def session_id(self):
        return self._session

    def last_usage(self):
        return self._usage


class StubHandle:
    def __init__(self):
        self.aborted = False

    def wait(self, timeout=None):
        return True

    def done(self):
        return True

    def abort(self):
        self.aborted = True


class StubClient:
    base_url = "http://127.0.0.1:18988"

    def __init__(self):
        self.last_body = None
        self.posts: List[tuple] = []

    def agents(self):
        return [{"agent_id": "a1", "nickname": "小莲", "model_id": "m1",
                 "tool_ids": ["read_file"]}]

    def models(self):
        return [{"model_id": "m1", "model_name": "gpt-5"}]

    def tools(self):
        return [{"tool_id": "read_file", "name": "read_file",
                 "description": "读取文件内容"}]

    def env(self):
        return {"env": {"AGENTS_URL": "http://0.0.0.0:18988",
                        "AGENTS_VERSION": "1.2.3"}}

    def auth_status(self):
        return None

    def tunnel_status(self):
        return {"configured": False, "parent": "", "tunnel_id": "",
                "enabled": False, "state": "idle", "env_id": "", "detail": ""}

    def sessions(self, limit=20):
        return []

    def conversation(self, session_id):
        return {"messages": []}

    def post(self, path, body=None):
        # Record every POST so tests can assert path + body.
        self.posts.append((path, body))
        if path.endswith("/generate-title"):
            sid = path.split("/")[3]
            return {"status": "success", "session_id": sid,
                    "title": (body or {}).get("title", ""), "title_given": True}
        raise RuntimeError("no route")

    def tunnel_register(self, url):
        return {"ok": True}

    def tunnel_unregister(self):
        return {"ok": True}

    def infer_stream(self, body, on_event):
        self.last_body = body
        on_event("init", {"session_id": "s1", "title": "问候",
                          "agent_nickname": "小莲"})
        on_event("message", {"role": "assistant", "content": "你好，世界"})
        on_event("usage", {"prompt_tokens": 1, "completion_tokens": 2,
                           "total_tokens": 3, "overall_ms": 100})
        on_event("done", {})
        return StubHandle()


def test_app_main_loop_send_stream_render_exit():
    screen = term_mod.HeadlessScreen(60, 12)
    terminal = term_mod.Terminal(screen=screen,
                                 input_source=term_mod.FakeInput(
                                     ["h", "i", term_mod.KEY_ENTER,
                                      term_mod.KEY_CTRL_C]))
    application = app_mod.App(StubClient(), app_mod.TuiOptions(), term=terminal,
                              transcript_factory=StubTranscript)
    assert application.run() == 0

    rendered = "\n".join(ui_mod.strip_ansi(line) for line in screen.render())
    assert "hi" in rendered                    # user turn
    assert "你好，世界" in rendered             # assistant stream
    assert application.session_id == "s1"      # session id captured from init
    assert application.session_title == "问候"


def test_app_plain_loop_logs_without_ansi_and_exits():
    inputs = iter(["hi"])

    def readline(prompt=""):
        try:
            return next(inputs)
        except StopIteration:
            return None

    out = io.StringIO()
    plain = ui_mod.PlainUI(out=out, readline=readline)
    terminal = term_mod.Terminal(screen=term_mod.HeadlessScreen(),
                                 input_source=term_mod.FakeInput([]))
    application = app_mod.App(StubClient(), app_mod.TuiOptions(plain=True),
                              term=terminal,
                              transcript_factory=StubTranscript,
                              plain_io=plain)
    assert application.run() == 0
    text = out.getvalue()
    assert "你> hi" in text
    assert "你好，世界" in text
    assert "\x1b" not in text


def test_app_help_modal_and_exit_command():
    screen = term_mod.HeadlessScreen(60, 14)
    terminal = term_mod.Terminal(screen=screen,
                                 input_source=term_mod.FakeInput(
                                     list("/help") + [term_mod.KEY_ENTER,
                                                      term_mod.KEY_ESC,
                                                      term_mod.KEY_CTRL_C]))
    application = app_mod.App(StubClient(), app_mod.TuiOptions(), term=terminal,
                              transcript_factory=StubTranscript)
    assert application.run() == 0
    # After Esc the modal is closed again.
    assert application._ui.modal is None


# ══════════════════════════════════════════════════════════════════════════════
# /title command — POST /v1/sessions/{id}/generate-title
# ══════════════════════════════════════════════════════════════════════════════

def _system_text(application) -> str:
    return "\n".join(line for block in application._system_blocks
                     for line in block.lines)


def _bare_app(client):
    terminal = term_mod.Terminal(screen=term_mod.HeadlessScreen(40, 8),
                                 input_source=term_mod.FakeInput([]))
    return app_mod.App(client, app_mod.TuiOptions(), term=terminal,
                       transcript_factory=StubTranscript)


def test_title_command_posts_generate_title_and_updates_title():
    client = StubClient()
    application = _bare_app(client)
    application.session_id = "s1"
    application.session_title = "旧标题"

    application._run_command("/title 我的新标题")

    assert client.posts == [("/v1/sessions/s1/generate-title",
                             {"title": "我的新标题"})]
    assert application.session_title == "我的新标题"   # title bar state synced
    assert "标题已更新: 我的新标题" in _system_text(application)


def test_title_command_without_session_prompts_and_skips_post():
    client = StubClient()
    application = _bare_app(client)
    assert application.session_id is None

    application._run_command("/title 我的新标题")

    assert client.posts == []
    assert "当前无会话" in _system_text(application)


# ══════════════════════════════════════════════════════════════════════════════
# /tunnel + /status — an empty badge must not leave a dangling "- 徽标: "
# ══════════════════════════════════════════════════════════════════════════════

_DISABLED_TUNNEL = {
    "configured": True, "enabled": False, "state": "idle",
    "parent": "", "tunnel_id": "", "env_id": "", "detail": "",
}


def _tunnel_app(client):
    """App whose cached tunnel status has been refreshed from ``client``."""
    application = _bare_app(client)
    application._refresh_badge()
    return application


def test_refresh_badge_stays_empty_when_not_enabled():
    # The user's exact situation: configured (SETUP_SOURCE set) but the dial
    # flag is off, so the badge must not claim a parent registration.
    client = StubClient()
    client.tunnel_status = lambda: dict(_DISABLED_TUNNEL)
    application = _tunnel_app(client)
    assert application.badge == ""


def test_tunnel_command_no_badge_placeholder_when_not_enabled():
    client = StubClient()
    client.tunnel_status = lambda: dict(_DISABLED_TUNNEL)
    application = _tunnel_app(client)
    application._run_command("/tunnel")
    text = _system_text(application)
    assert "- 徽标: （无）" in text
    assert not text.rstrip().endswith("- 徽标:")


def test_tunnel_command_shows_badge_when_enabled_and_configured():
    client = StubClient()
    client.tunnel_status = lambda: {
        "configured": True, "enabled": True, "state": "online",
        "parent": "http://parent", "tunnel_id": "t1", "env_id": "e1", "detail": "",
    }
    application = _tunnel_app(client)
    application._run_command("/tunnel")
    text = _system_text(application)
    assert "- 徽标: 已注册->母端(在线)" in text


def test_status_command_reports_badge_placeholder_and_enabled_flag():
    client = StubClient()
    client.tunnel_status = lambda: dict(_DISABLED_TUNNEL)
    application = _tunnel_app(client)
    application._run_command("/status")
    text = _system_text(application)
    # The diagnostic line must expose enabled (not only configured/state) and
    # must render a placeholder instead of a dangling empty badge.
    assert "- 隧道：（无）（configured=True, enabled=False, state=idle）" in text


# ══════════════════════════════════════════════════════════════════════════
# Exit keys — q is ordinary text; Ctrl+C (idle) and /exit exit (contract §6)
# ══════════════════════════════════════════════════════════════════════════

def _setup_app(client=None, size=(50, 10)):
    """Build an app and run ``setup()`` so ``_ui`` (and its input line) exists."""
    screen = term_mod.HeadlessScreen(*size)
    terminal = term_mod.Terminal(screen=screen,
                                 input_source=term_mod.FakeInput([]))
    application = app_mod.App(client or StubClient(), app_mod.TuiOptions(),
                              term=terminal, transcript_factory=StubTranscript)
    application.setup()
    return application


def test_q_with_empty_input_is_inserted_and_does_not_exit():
    # The old guard fired before insertion, so the *first* key of any message
    # (input empty) used to quit.  q must now be ordinary text.
    application = _setup_app()
    application._dispatch_key("q")
    assert application.running is True
    assert application._ui.input.text() == "q"


def test_message_starting_with_q_can_be_typed_and_sent():
    client = StubClient()
    application = _setup_app(client)
    for ch in "quit now":
        application._dispatch_key(ch)
    assert application._ui.input.text() == "quit now"   # q kept as text

    application._dispatch_key(term_mod.KEY_ENTER)
    assert application.running is True                  # not treated as quit
    assert client.last_body["messages"][0]["content"] == "quit now"


def test_exit_command_exits_program():
    application = _setup_app()
    for ch in "/exit":
        application._dispatch_key(ch)
    application._dispatch_key(term_mod.KEY_ENTER)
    assert application.running is False


def test_help_documents_exit_paths_and_omits_ctrl_d_and_quit():
    keys = [key for key, _ in app_mod._KEYMAP_ROWS]
    assert "q" not in keys                              # q key row removed
    assert "Ctrl+D" not in keys                         # Ctrl+D key row removed
    assert "Ctrl+C" in keys                             # sole interrupt/exit key
    assert "/quit" not in [cmd for cmd, _ in app_mod._COMMAND_ROWS]
    assert "quit" not in app_mod.COMMAND_NAMES
    assert "/exit" in [cmd for cmd, _ in app_mod._COMMAND_ROWS]

    rendered = "\n".join(app_mod.help_lines())
    assert "/exit" in rendered
    assert "Ctrl+D" not in rendered
    assert "/quit" not in rendered


def test_status_bar_hints_use_ctrl_c_and_cjk_arrows():
    hints = ui_mod.StatusBar.HINTS
    assert "[Ctrl+C]中断/退出" in hints
    assert "[上下]回看" in hints
    # The banner's third line moved into the status bar.
    assert "回车发送" in hints
    assert "[/help]帮助" in hints
    assert "[Ctrl+D]" not in hints
    assert "↑↓" not in hints
    assert "[q]" not in hints


def test_status_bar_drops_enter_and_help_hints_at_40_but_keeps_ctrl_c():
    body = ui_mod.strip_ansi(ui_mod.StatusBar().render(40, False, ""))
    assert "[Ctrl+C]中断/退出" in body
    assert "回车发送" not in body
    assert "[/help]帮助" not in body


def test_status_bar_full_hint_order_when_wide():
    body = ui_mod.strip_ansi(ui_mod.StatusBar().render(200, False, ""))
    assert body.startswith(ui_mod.StatusBar.HINTS)


# ════════════════════════════════════════════════════════════════════════════
# Status bar width safety — no East-Asian *Ambiguous* glyphs
# ════════════════════════════════════════════════════════════════════════════

_STATUS_BAR_STATES = [
    (False, ""),                # 空闲
    (True, ""),                 # 生成中
    (False, "候选: /help"),      # 带 hint
]


@pytest.mark.parametrize("width", [40, 60, 80, 120, 200])
@pytest.mark.parametrize("streaming,hint", _STATUS_BAR_STATES)
def test_status_bar_never_overflows_and_ends_with_right_segment(width, streaming, hint):
    bar = ui_mod.StatusBar()
    rendered = bar.render(width, streaming, hint)
    right = hint or ("推理:进行中..." if streaming else "推理:空闲")

    # The row never exceeds the terminal width …
    assert ui_mod.visible_width(rendered) <= width
    # … and the right segment ends the same row (never wrapped by a
    # mis-measured wide character).
    body = ui_mod.strip_ansi(rendered)
    assert body.endswith(right)
    assert "\n" not in body


def test_fixed_ui_text_has_no_ambiguous_width_characters():
    # East-Asian *Ambiguous* glyphs (↑ ↓ → … ⛓ …) are rendered double-width by
    # many CJK fonts while the code counts them as one column; fixed UI strings
    # must therefore avoid them entirely.
    bar = ui_mod.StatusBar()
    title_bar = ui_mod.TitleBar()
    samples = [
        ui_mod.StatusBar.HINTS,
        "推理:进行中...",
        "推理:空闲",
        ui_mod.tunnel_badge({"configured": True, "enabled": True, "state": "online"}),
        ui_mod.tunnel_badge({"configured": True, "enabled": True, "state": "connecting"}),
        # New TitleBar fixed strings: the tool-count label and the truncation
        # ellipsis (must stay ASCII ``...``).
        "工具:3",
        ui_mod.strip_ansi(title_bar.render(10, "MODEL", "AGENT", "BADGE",
                                           "TITLE", 7, "WS")),
    ]
    # Tie the hard-coded right-segment samples to the live renderer.
    assert ui_mod.strip_ansi(bar.render(200, True)).endswith("推理:进行中...")
    assert ui_mod.strip_ansi(bar.render(200, False)).endswith("推理:空闲")
    # Tie the TitleBar samples to the live renderer (model truncation = ASCII).
    assert ui_mod.strip_ansi(
        title_bar.render(10, "MODEL", "AGENT", "BADGE", "TITLE", 7, "WS")
    ) == "M... AGENT"

    for text in samples:
        offenders = [ch for ch in text
                     if unicodedata.east_asian_width(ch) == "A"]
        assert offenders == [], (text, offenders)


# ══════════════════════════════════════════════════════════════════════════
# Title-bar chrome data: tool_count / workspace / welcome banner
# ══════════════════════════════════════════════════════════════════════════

def _plain_app(client, register_result=None):
    terminal = term_mod.Terminal(screen=term_mod.HeadlessScreen(40, 8),
                                 input_source=term_mod.FakeInput([]))
    opts = app_mod.TuiOptions(plain=True, register_result=register_result)
    return app_mod.App(client, opts, term=terminal,
                       transcript_factory=StubTranscript)


def test_welcome_tui_mode_without_register_is_silent():
    application = _bare_app(StubClient())
    application.opts.register_result = None
    application._welcome()
    assert application._system_blocks == []


def test_welcome_tui_mode_with_register_emits_single_line():
    application = _bare_app(StubClient())
    application.opts.register_result = {"ok": True}
    application._welcome()
    assert len(application._system_blocks) == 1
    assert application._system_blocks[0].lines == ["母端注册：成功"]


def test_welcome_tui_mode_registration_failure_line():
    application = _bare_app(StubClient())
    application.opts.register_result = {"ok": False, "error": "boom"}
    application._welcome()
    assert len(application._system_blocks) == 1
    assert application._system_blocks[0].lines == ["母端注册：失败 · boom"]


def test_welcome_plain_mode_keeps_identity_line():
    application = _plain_app(StubClient())
    application.agent_name = "小莲"
    application.model_name = "gpt-5"
    application._welcome()
    lines = [ln for blk in application._system_blocks for ln in blk.lines]
    assert lines == ["agent=小莲 · model=gpt-5"]


def test_welcome_plain_mode_identity_plus_register():
    application = _plain_app(StubClient(), register_result={"ok": True})
    application.agent_name = "小莲"
    application.model_name = "gpt-5"
    application._welcome()
    lines = [ln for blk in application._system_blocks for ln in blk.lines]
    assert lines == ["agent=小莲 · model=gpt-5", "母端注册：成功"]


def test_load_context_reads_tool_count_and_workspace():
    client = StubClient()
    client.env = lambda: {"env": {"AGENTS_WORKSPACE": "/home/agent/ws"}}
    application = _bare_app(client)
    application._load_context()
    assert application.tool_count == 1               # agent tool_ids=[read_file]
    assert application.workspace_path == "/home/agent/ws"


def test_load_context_tool_count_none_without_agent():
    client = StubClient()
    client.agents = lambda: []
    client.models = lambda: []
    application = _bare_app(client)
    application._load_context()
    assert application.tool_count is None


def test_load_context_workspace_empty_on_env_failure():
    client = StubClient()

    def _boom():
        raise RuntimeError("no env")

    client.env = _boom
    application = _bare_app(client)
    application._load_context()                      # must not raise / emit
    assert application.workspace_path == ""
    assert application._system_blocks == []


def test_agent_switch_refreshes_identity_and_tool_count():
    application = _setup_app()
    application._select_agent(ui_mod.SelectorItem(
        name="小刚",
        value={"agent_id": "a2", "nickname": "小刚", "model_id": "m2",
               "tool_ids": ["read_file", "write_file"]},
    ))
    assert application.agent_id == "a2"
    assert application.agent_name == "小刚"
    assert application.model_id == "m2"
    assert application.tool_count == 2


def test_sync_view_pushes_tool_count_and_workspace_to_ui():
    application = _setup_app()
    application.tool_count = 5
    application.workspace_path = "/w/x"
    application._sync_view()
    assert application._ui.tool_count == 5
    assert application._ui.workspace_path == "/w/x"


def test_tuiui_title_bar_renders_chrome_fields():
    screen = term_mod.HeadlessScreen(80, 6)
    tui = ui_mod.TuiUI(screen)
    tui.model_name = "gpt-5"
    tui.agent_name = "小莲"
    tui.badge = "独立"
    tui.title = "会话"
    tui.tool_count = 2
    tui.workspace_path = "/home/agent/ws"
    tui.draw()
    top = ui_mod.strip_ansi(screen.line(0))
    assert top.startswith("gpt-5 ")
    assert top.endswith("独立 小莲")
    assert "工具:2" in top and "/home/agent/ws" in top
