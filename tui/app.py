"""TUI main loop: state machine, keymap, slash commands.

Frozen surface (``docs/tui-contract.md`` §4.3 / §6 / §7)::

    @dataclass
    class TuiOptions:
        session_id: str | None = None
        new_session: bool = False
        plain: bool = False
        register_result: dict | None = None

    def run(client: "ServiceClient", opts: TuiOptions) -> int: ...

The app talks to the backend exclusively through ``ServiceClient`` and to the
transcript exclusively through ``tui.render.Transcript``.  Neither module is
imported at module load time — the transcript is built lazily via
:func:`_make_transcript`, which keeps ``import tui.app`` working (and unit
testable) even before those files exist.  Tests inject their own transcript
factory / terminal / plain console through :class:`App`.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from tui import term as term_mod
from tui import ui as ui_mod
from tui.chat_client import ApiError

__all__ = ["TuiOptions", "run", "App"]


# ══════════════════════════════════════════════════════════════════════════
# Options (frozen)
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class TuiOptions:
    session_id: Optional[str] = None     # --session 挂载历史会话
    new_session: bool = False            # --new
    plain: bool = False                  # --plain 纯文本模式
    register_result: Optional[dict] = None  # --register 的结果（欢迎横幅）


# ══════════════════════════════════════════════════════════════════════════
# Static content
# ══════════════════════════════════════════════════════════════════════════

COMMAND_NAMES = [
    "new", "sessions", "continue", "title", "agent", "model", "tools",
    "status", "tunnel", "plain", "help", "exit",
]

_KEYMAP_ROWS = [
    ("Enter", "发送；命令模式下执行命令"),
    ("Ctrl+C", "推理中=中断；空闲=退出"),
    ("Ctrl+L", "全量重绘"),
    ("Ctrl+O", "展开/收起最近 tool 块"),
    ("Ctrl+U", "清空输入"),
    ("↑/↓", "输入为空→转写区滚动；非空→历史输入"),
    ("←/→/Backspace/Delete", "输入编辑"),
    ("Tab", "命令补全"),
    ("Esc", "关闭模态/补全；其他忽略"),
    ("PageUp/PageDown", "整页滚动转写区"),
]

_COMMAND_ROWS = [
    ("/new", "新会话（清空转写区上下文）"),
    ("/sessions", "会话选择器，加载历史对话"),
    ("/continue", "对当前会话续推"),
    ("/title <t>", "修改当前会话标题（人工设置）"),
    ("/agent", "选择 agent"),
    ("/model", "选择模型"),
    ("/tools", "列出当前 agent 可见工具"),
    ("/status", "版本 / 鉴权 / 隧道摘要"),
    ("/tunnel", "隧道状态全字段"),
    ("/tunnel register <url>", "注册到母端"),
    ("/tunnel unregister", "从母端注销"),
    ("/plain", "运行时切换 plain 模式（再次切换回 TUI）"),
    ("/help", "本帮助页"),
    ("/exit", "退出"),
]


def help_lines() -> List[str]:
    lines = ["键位："]
    lines += [f"  {key:<22} {desc}" for key, desc in _KEYMAP_ROWS]
    lines += ["", "命令："]
    lines += [f"  {cmd:<26} {desc}" for cmd, desc in _COMMAND_ROWS]
    return lines


# ══════════════════════════════════════════════════════════════════════════
# App-side block for command output / connection info (contract §4.2)
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class SystemBlock:
    """A ``system`` block constructed directly by the app layer."""
    kind: str = "system"
    lines: List[str] = field(default_factory=list)
    full_lines: Optional[List[str]] = None
    badge: Optional[str] = None
    detail: str = ""


# ══════════════════════════════════════════════════════════════════════════
# Lazy collaborator factories (overridable in tests)
# ══════════════════════════════════════════════════════════════════════════

def _make_transcript():
    from tui.render import Transcript
    return Transcript()


def _build_terminal(opts: TuiOptions):
    return term_mod.Terminal()


# ══════════════════════════════════════════════════════════════════════════
# App
# ══════════════════════════════════════════════════════════════════════════

class App:
    def __init__(self, client, opts: TuiOptions,
                 term: Optional[term_mod.Terminal] = None,
                 transcript_factory: Optional[Callable[[], object]] = None,
                 plain_io=None) -> None:
        self.client = client
        self.opts = opts
        self.term = term if term is not None else _build_terminal(opts)
        self._transcript_factory = transcript_factory or _make_transcript
        self.plain_io = plain_io if plain_io is not None else ui_mod.PlainUI()

        self.plain = bool(opts.plain)
        self.running = True
        self.exit_code = 0

        self.transcript = None
        self._system_blocks: List[SystemBlock] = []

        self.session_id: Optional[str] = None
        self.session_title = ""
        self.agent_id: Optional[str] = None
        self.agent_name = ""
        self.model_id: Optional[str] = None
        self.model_name = ""
        self.badge = ""
        self.tool_count: Optional[int] = None   # selected agent's tool count
        self.workspace_path = ""                # chrome: AGENTS_WORKSPACE

        self._agents: List[dict] = []
        self._models: List[dict] = []
        self._tunnel_status: dict = {}

        self.handle = None
        self._streaming = False
        self._dirty = False
        self._completion = None
        self._ui: Optional[ui_mod.TuiUI] = None

        self._commands = {
            "new": self._cmd_new,
            "sessions": self._cmd_sessions,
            "continue": self._cmd_continue,
            "title": self._cmd_title,
            "agent": self._cmd_agent,
            "model": self._cmd_model,
            "tools": self._cmd_tools,
            "status": self._cmd_status,
            "tunnel": self._cmd_tunnel,
            "plain": self._cmd_plain,
            "help": self._cmd_help,
            "exit": self._cmd_exit,
        }

    # ══════════════════════════════════════════════════════════════════
    # Setup
    # ══════════════════════════════════════════════════════════════════

    def _new_transcript(self) -> None:
        self.transcript = self._transcript_factory()

    def setup(self) -> None:
        self._new_transcript()
        self._system_blocks = []
        if self.opts.new_session:
            self.session_id = "new"
        elif self.opts.session_id:
            self.session_id = self.opts.session_id

        self._load_context()
        if self.opts.session_id and not self.opts.new_session:
            self._load_conversation(self.opts.session_id, announce=False)
        self._refresh_badge()
        self._welcome()

        if not self.plain:
            self._ui = ui_mod.TuiUI(self.term.screen)
            self._ui.transcript.set_provider(self._provider)
            self._sync_view()

    def _load_context(self) -> None:
        try:
            self._agents = list(self.client.agents())
        except Exception as exc:
            self._agents = []
            self._emit_system(f"获取 agent 列表失败：{exc}")
        try:
            self._models = list(self.client.models())
        except Exception as exc:
            self._models = []
            self._emit_system(f"获取模型列表失败：{exc}")

        if self._agents:
            agent = self._agents[0]
            self.agent_id = agent.get("agent_id")
            self.agent_name = agent.get("nickname") or self.agent_id or ""
            self.model_id = agent.get("model_id")
            self.tool_count = len(agent.get("tool_ids") or [])
        if not self.model_id and self._models:
            self.model_id = self._models[0].get("model_id")
        self.model_name = self._model_display(self.model_id)

        # Workspace (title-bar chrome data): best-effort — a failed probe is
        # left silent, never surfaced as a user-facing system message.
        self.workspace_path = ""
        try:
            data = self.client.env()
            if isinstance(data, dict):
                env = data.get("env") or {}
                if isinstance(env, dict):
                    self.workspace_path = env.get("AGENTS_WORKSPACE", "") or ""
        except Exception:
            self.workspace_path = ""

    def _model_display(self, model_id: Optional[str]) -> str:
        for model in self._models:
            if model.get("model_id") == model_id:
                return model.get("model_name") or model_id or ""
        return model_id or ""

    def _welcome(self) -> None:
        lines = []
        # In TUI mode the identity now lives in the title bar and the key hints
        # in the status bar, so the old 3-line banner is gone entirely.  Plain
        # mode keeps one identity line for the remote audit trail.
        if self.plain:
            lines.append(
                f"agent={self.agent_name or '（无）'} · "
                f"model={self.model_name or '（无）'}"
            )
        result = self.opts.register_result
        if result:
            if result.get("ok"):
                lines.append("母端注册：成功")
            else:
                lines.append(f"母端注册：失败 · {result.get('error') or '未知错误'}")
        if lines:
            self._emit_system("\n".join(lines))

    def _refresh_badge(self) -> None:
        try:
            self._tunnel_status = dict(self.client.tunnel_status())
        except Exception:
            self._tunnel_status = {}
        self.badge = ui_mod.tunnel_badge(self._tunnel_status)

    # ══════════════════════════════════════════════════════════════════
    # Block plumbing
    # ══════════════════════════════════════════════════════════════════

    def _provider(self) -> List:
        try:
            convo = list(self.transcript.blocks())
        except Exception:
            convo = []
        return convo + list(self._system_blocks)

    def _emit_system(self, text: str) -> None:
        self._system_blocks.append(SystemBlock(lines=str(text).split("\n")))

    def _load_conversation(self, session_id: str, announce: bool = True) -> None:
        try:
            data = self.client.conversation(session_id)
        except Exception as exc:
            self._emit_system(f"加载会话失败：{exc}")
            return
        self._new_transcript()
        self._system_blocks = []
        try:
            self.transcript.load_conversation(data)
        except Exception as exc:
            self._emit_system(f"解析会话失败：{exc}")
        self.session_id = session_id
        title = data.get("title") if isinstance(data, dict) else None
        self.session_title = title or session_id
        if announce:
            self._emit_system(f"已加载会话 {session_id}。")

    # ══════════════════════════════════════════════════════════════════
    # Main loop
    # ══════════════════════════════════════════════════════════════════

    def run(self) -> int:
        try:
            self.setup()
            while self.running:
                if self.plain:
                    self._plain_loop()
                else:
                    self._tui_loop()
        except KeyboardInterrupt:
            pass
        finally:
            try:
                self.term.leave()
            except Exception:
                pass
        return self.exit_code

    # ── TUI loop ──
    def _tui_loop(self) -> None:
        self.term.enter()
        self.term.on_resize(self._on_resize)
        if self._ui is not None:
            self._ui.invalidate()
            self._sync_view()
        try:
            while self.running and not self.plain:
                self._tui_tick()
        finally:
            self.term.leave()
            if self.running and not self.plain:
                self.running = False

    def _tui_tick(self) -> None:
        self._sync_view()
        self._ui.draw()
        key = self.term.read_key(timeout=0.05)
        if key is not None:
            self._dispatch_key(key)
        elif not self._streaming and self._input_exhausted():
            self.running = False
            return
        self._poll_stream()

    def _on_resize(self) -> None:
        if self._ui is not None:
            self._ui.invalidate()

    def _input_exhausted(self) -> bool:
        fn = getattr(self.term.input, "exhausted", None)
        return bool(fn()) if callable(fn) else False

    def _sync_view(self) -> None:
        if self._ui is None:
            return
        self._ui.streaming = self._streaming
        if self.session_title:
            self._ui.title = self.session_title
        elif self.session_id and self.session_id != "new":
            self._ui.title = self.session_id
        else:
            self._ui.title = "新会话"
        self._ui.agent_name = self.agent_name
        self._ui.model_name = self.model_name
        self._ui.badge = self.badge
        self._ui.tool_count = self.tool_count
        self._ui.workspace_path = self.workspace_path

    # ── key dispatch ──
    def _dispatch_key(self, key: str) -> None:
        if self._ui is not None and self._ui.modal is not None:
            self._modal_key(key)
            return

        if key == term_mod.KEY_ESC:
            self._completion = None
            if self._ui is not None:
                self._ui.hint = ""
            return
        if key == term_mod.KEY_CTRL_C:
            if self._streaming:
                self._abort()
            else:
                self.running = False
            return
        if key == term_mod.KEY_CTRL_L:
            if self._ui is not None:
                self._ui.invalidate()
            return
        if key == term_mod.KEY_CTRL_O:
            if self._ui is not None:
                self._ui.transcript.toggle_last_tool()
            return
        if key == term_mod.KEY_CTRL_U:
            if self._ui is not None:
                self._ui.input.clear()
            return
        if key == term_mod.KEY_ENTER:
            self._submit()
            return
        if key == term_mod.KEY_TAB:
            self._complete()
            return
        if key in (term_mod.KEY_UP, term_mod.KEY_DOWN):
            self._handle_vertical(key)
            return
        if key == term_mod.KEY_PAGEUP:
            if self._ui is not None:
                self._ui.transcript.scroll_by(self._page_size())
            return
        if key == term_mod.KEY_PAGEDOWN:
            if self._ui is not None:
                self._ui.transcript.scroll_by(-self._page_size())
            return
        if self._ui is None:
            return
        if key == term_mod.KEY_LEFT:
            self._ui.input.left()
        elif key == term_mod.KEY_RIGHT:
            self._ui.input.right()
        elif key == term_mod.KEY_BACKSPACE:
            self._ui.input.backspace()
        elif key == term_mod.KEY_DELETE:
            self._ui.input.delete()
        elif key == term_mod.KEY_HOME:
            self._ui.input.home()
        elif key == term_mod.KEY_END:
            self._ui.input.end()
        elif len(key) == 1 and key >= " ":
            # "q" is an ordinary character (contract revision 2026-07-10): the
            # old "empty input exits" guard fired on the first character of
            # every message, so messages starting with q could never be typed.
            self._ui.input.insert(key)

    def _handle_vertical(self, key: str) -> None:
        if self._ui is None:
            return
        if self._ui.input.text() == "":
            self._ui.transcript.scroll_by(1 if key == term_mod.KEY_UP else -1)
        elif key == term_mod.KEY_UP:
            self._ui.input.history_prev()
        else:
            self._ui.input.history_next()

    def _page_size(self) -> int:
        if self._ui is None:
            return 10
        _, rows = self._ui.screen.size()
        return max(1, rows - 4)

    # ── completion ──
    def _complete(self) -> None:
        if self._ui is None:
            return
        text = self._ui.input.text()
        if not text.startswith("/"):
            return
        prefix = text[1:].split(" ", 1)[0]
        cands = [c for c in COMMAND_NAMES if c.startswith(prefix)]
        if not cands:
            return
        if len(cands) == 1:
            self._ui.input.set_text("/" + cands[0] + " ")
            self._ui.hint = ""
            self._completion = None
            return
        common = os.path.commonprefix(cands)
        self._ui.input.set_text("/" + common)
        if self._completion and self._completion[0] == cands:
            self._completion[1] = (self._completion[1] + 1) % len(cands)
        else:
            self._completion = [cands, 0]
        self._ui.input.set_text("/" + cands[self._completion[1]])
        self._ui.hint = "候选: " + " ".join("/" + c for c in cands)

    # ── modal ──
    def _modal_key(self, key: str) -> None:
        modal = self._ui.modal
        if key == term_mod.KEY_ESC:
            self._ui.modal = None
            return
        if isinstance(modal, ui_mod.Selector):
            if key == term_mod.KEY_UP:
                modal.move(-1)
            elif key == term_mod.KEY_DOWN:
                modal.move(1)
            elif key == term_mod.KEY_ENTER:
                item = modal.selected()
                self._ui.modal = None
                if item is not None and modal.on_select is not None:
                    modal.on_select(item)
        else:  # TextPage
            _, rows = self._ui.screen.size()
            body = max(1, rows - 4)
            if key in (term_mod.KEY_UP, "k"):
                modal.move(-1)
            elif key in (term_mod.KEY_DOWN, "j"):
                modal.move(1)
            elif key == term_mod.KEY_PAGEUP:
                modal.page(-1, body)
            elif key == term_mod.KEY_PAGEDOWN:
                modal.page(1, body)
            elif key in (term_mod.KEY_ENTER, "q"):
                self._ui.modal = None

    # ── submit / streaming ──
    def _submit(self) -> None:
        if self._ui is None:
            return
        text = self._ui.input.text()
        if not text.strip():
            return
        self._ui.input.push_history(text)
        self._ui.input.clear()
        if text.startswith("/"):
            self._run_command(text)
        else:
            self._send_user(text)

    def _send_user(self, text: str) -> None:
        try:
            self.transcript.append_frame(
                "message", {"role": "user", "content": text}
            )
        except Exception:
            pass
        session = self.session_id or "new"
        body = {
            "model_id": self.model_id,
            "messages": [{"role": "user", "content": text}],
            "stream": True,
            "session_id": session,
        }
        if self.agent_id:
            body["agent_ids"] = [self.agent_id]
        self._start_stream(body)

    def _send_continue(self) -> None:
        body = {
            "model_id": self.model_id,
            "messages": [],
            "stream": True,
            "session_id": self.session_id,
            "continue": True,
        }
        if self.agent_id:
            body["agent_ids"] = [self.agent_id]
        self._start_stream(body)

    def _start_stream(self, body: dict) -> None:
        def on_event(event, data):
            try:
                self.transcript.append_frame(event, data)
            except Exception:
                pass
            self._handle_event_side(event, data or {})
            self._dirty = True

        try:
            self.handle = self.client.infer_stream(body, on_event)
        except Exception as exc:
            self.handle = None
            self._emit_system(f"发送失败：{exc}")
            return
        self._streaming = True
        self._dirty = True
        if self.plain:
            self._drain_plain()

    def _handle_event_side(self, event: str, data: dict) -> None:
        if event == "init":
            sid = data.get("session_id")
            if sid:
                self.session_id = sid
            if data.get("title"):
                self.session_title = data.get("title")
            nickname = data.get("agent_nickname")
            if nickname:
                self.agent_name = nickname

    def _poll_stream(self) -> None:
        if not self._streaming:
            return
        if self.handle is None or self.handle.done():
            self._finish_stream()

    def _finish_stream(self) -> None:
        self._streaming = False
        self.handle = None
        if self.plain:
            self.plain_io.sync(self._provider())

    def _abort(self) -> None:
        handle = self.handle
        if handle is not None:
            try:
                handle.abort()
            except Exception as exc:
                self._emit_system(f"中断失败：{exc}")
        self._emit_system("已请求中断推理…")

    def _drain_plain(self) -> None:
        while self.handle is not None and not self.handle.done():
            self.plain_io.sync(self._provider())
            try:
                self.handle.wait(0.1)
            except KeyboardInterrupt:
                self._abort()
        self.plain_io.sync(self._provider())
        self._streaming = False
        self.handle = None

    # ══════════════════════════════════════════════════════════════════
    # Plain loop
    # ══════════════════════════════════════════════════════════════════

    def _plain_loop(self) -> None:
        self.plain_io.sync(self._provider())
        while self.running and self.plain:
            try:
                line = self.plain_io.readline("› ")
            except KeyboardInterrupt:
                if self._streaming:
                    self._abort()
                    self._drain_plain()
                    continue
                self.running = False
                break
            if line is None:
                self.running = False
                break
            self._handle_plain_line(line)

    def _handle_plain_line(self, line: str) -> None:
        text = line.rstrip("\n")
        if not text.strip():
            return
        if text.startswith("/"):
            self._run_command(text)
        else:
            self._send_user(text)
        if not self._streaming:
            self.plain_io.sync(self._provider())

    # ══════════════════════════════════════════════════════════════════
    # Slash commands (contract §7)
    # ══════════════════════════════════════════════════════════════════

    def _run_command(self, text: str) -> None:
        parts = text.strip().split()
        if not parts:
            return
        cmd = parts[0][1:] if parts[0].startswith("/") else parts[0]
        args = parts[1:]
        rest = text.strip()[len(parts[0]):].strip()
        handler = self._commands.get(cmd)
        if handler is None:
            self._emit_system(f"未知命令：/{cmd}（/help 查看全部命令）")
            return
        try:
            handler(args, rest)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            self._emit_system(f"命令 /{cmd} 失败：{exc}")

    def _cmd_new(self, args, rest) -> None:
        self._new_transcript()
        self._system_blocks = []
        self.session_id = "new"
        self.session_title = ""
        if self._ui is not None:
            self._ui.transcript.reset()
        self._emit_system("已开始新会话（session_id=new）。")

    def _cmd_sessions(self, args, rest) -> None:
        try:
            sessions = list(self.client.sessions(limit=20))
        except Exception as exc:
            self._emit_system(f"获取会话列表失败：{exc}")
            return
        if not sessions:
            self._emit_system("没有历史会话。")
            return
        items = []
        for s in sessions:
            sid = s.get("session_id") or s.get("id") or ""
            title = s.get("title") or sid
            secondary = str(s.get("last_inference_at") or s.get("updated") or "")
            items.append(ui_mod.SelectorItem(name=str(title), secondary=secondary,
                                             value=sid))
        if self.plain or self._ui is None:
            self._emit_system("历史会话：\n" + "\n".join(
                f"- {it.name}  [{it.secondary}]  {it.value}" for it in items
            ))
            return
        self._ui.modal = ui_mod.Selector(
            "选择会话（Enter 确认，Esc 取消）", items,
            on_select=lambda it: self._load_conversation(it.value),
        )

    def _cmd_continue(self, args, rest) -> None:
        if not self.session_id or self.session_id == "new":
            self._emit_system("当前没有可续推的会话（先发送一条消息）。")
            return
        self._emit_system("续推当前会话…")
        self._send_continue()

    def _cmd_title(self, args, rest) -> None:
        if not rest:
            self._emit_system("用法：/title <新标题>")
            return
        if not self.session_id or self.session_id == "new":
            self._emit_system("当前无会话（先发送一条消息再改名）。")
            return
        try:
            result = self.client.post(
                f"/v1/sessions/{self.session_id}/generate-title",
                {"title": rest},
            )
        except ApiError as exc:
            self._emit_system(f"标题更新失败：{exc}")
            return
        # 后端 200 返回 {"status","session_id","title","title_given"}；以返回的
        # title 为准（可能被截断），字段缺失时回退到用户输入。
        title = rest
        if isinstance(result, dict) and result.get("title"):
            title = str(result["title"])
        self.session_title = title
        self._emit_system(f"标题已更新: {title}")

    def _select_agent(self, item: ui_mod.SelectorItem) -> None:
        agent = item.value if isinstance(item.value, dict) else {}
        self.agent_id = agent.get("agent_id") or self.agent_id
        self.agent_name = agent.get("nickname") or self.agent_id or ""
        self.tool_count = len(agent.get("tool_ids") or [])
        if agent.get("model_id"):
            self.model_id = agent["model_id"]
            self.model_name = self._model_display(self.model_id)
        self._emit_system(f"已切换 agent：{self.agent_name}（model={self.model_name}）")

    def _cmd_agent(self, args, rest) -> None:
        if not self._agents:
            self._emit_system("没有可用 agent（后端无 agent 时不注入工具）。")
            return
        items = [
            ui_mod.SelectorItem(
                name=str(a.get("nickname") or a.get("agent_id")),
                secondary=str(a.get("model_id") or ""),
                value=a,
            )
            for a in self._agents
        ]
        if self.plain or self._ui is None:
            self._emit_system("可用 agent：\n" + "\n".join(
                f"- {it.name}（{it.secondary}）" for it in items
            ))
            return
        self._ui.modal = ui_mod.Selector("选择 agent", items,
                                         on_select=self._select_agent)

    def _select_model(self, item: ui_mod.SelectorItem) -> None:
        model = item.value if isinstance(item.value, dict) else {}
        self.model_id = model.get("model_id")
        self.model_name = self._model_display(self.model_id)
        self._emit_system(f"已切换模型：{self.model_name}")

    def _cmd_model(self, args, rest) -> None:
        if not self._models:
            self._emit_system("没有可用模型。")
            return
        items = [
            ui_mod.SelectorItem(
                name=str(m.get("model_name") or m.get("model_id")),
                secondary=str(m.get("model_id") or ""),
                value=m,
            )
            for m in self._models
        ]
        if self.plain or self._ui is None:
            self._emit_system("可用模型：\n" + "\n".join(
                f"- {it.name}（{it.secondary}）" for it in items
            ))
            return
        self._ui.modal = ui_mod.Selector("选择模型", items,
                                         on_select=self._select_model)

    def _cmd_tools(self, args, rest) -> None:
        agent = next(
            (a for a in self._agents if a.get("agent_id") == self.agent_id), None
        )
        if agent is None:
            self._emit_system("未选择 agent。")
            return
        tool_ids = agent.get("tool_ids") or []
        try:
            tools = list(self.client.tools())
        except Exception as exc:
            self._emit_system(f"获取工具列表失败：{exc}")
            return
        if tool_ids:
            wanted = set(tool_ids)
            tools = [t for t in tools if t.get("tool_id") in wanted]
        if not tools:
            self._emit_system("当前 agent 无可见工具（后端不会注入工具）。")
            return
        lines = ["当前 agent 可见工具："]
        for t in tools:
            name = t.get("name") or t.get("tool_id")
            desc = ui_mod.truncate_to_width(str(t.get("description") or ""), 60)
            lines.append(f"- {name}：{desc}")
        self._emit_system("\n".join(lines))

    def _cmd_status(self, args, rest) -> None:
        lines = ["状态："]
        env_map = {}
        try:
            env = self.client.env()
            env_map = env.get("env", {}) if isinstance(env, dict) else {}
        except Exception as exc:
            lines.append(f"- env 获取失败：{exc}")
        version_keys = sorted(
            k for k in env_map
            if any(tag in str(k).upper() for tag in ("VERSION", "BUILD", "COMMIT"))
        )
        if version_keys:
            for k in version_keys:
                lines.append(f"- {k}={env_map[k]}")
        else:
            lines.append(f"- env: {len(env_map)} 项（无版本类键）")
        try:
            auth = self.client.auth_status()
            if auth is None:
                lines.append("- 鉴权：已启用（未认证）")
            else:
                lines.append("- 鉴权：" + ("未启用" if not auth.get("auth_enabled")
                                          else "已启用"))
        except Exception as exc:
            lines.append(f"- 鉴权：获取失败 {exc}")
        st = self._tunnel_status or {}
        lines.append(
            f"- 隧道：{ui_mod.tunnel_badge(st) or '（无）'}"
            f"（configured={st.get('configured')}, enabled={st.get('enabled')}, "
            f"state={st.get('state')}）"
        )
        lines.append(
            f"- agent={self.agent_name or '无'} · model={self.model_name or '无'}"
            f" · session={self.session_id or '无'}"
        )
        self._emit_system("\n".join(lines))

    def _cmd_tunnel(self, args, rest) -> None:
        if args and args[0] == "register":
            if len(args) < 2:
                self._emit_system("用法：/tunnel register <母端setup-url>")
                return
            try:
                result = self.client.tunnel_register(args[1])
            except Exception as exc:
                self._emit_system(f"隧道注册失败：{exc}")
                return
            self._emit_system("隧道注册" + ("成功" if result.get("ok") else "失败")
                              + (f"：{result.get('error')}" if result.get("error")
                                 else ""))
            self._refresh_badge()
            return
        if args and args[0] == "unregister":
            try:
                result = self.client.tunnel_unregister()
            except Exception as exc:
                self._emit_system(f"隧道注销失败：{exc}")
                return
            self._emit_system("隧道注销" + ("成功" if result.get("ok") else "失败")
                              + (f"：{result.get('error')}" if result.get("error")
                                 else ""))
            self._refresh_badge()
            return
        try:
            st = self.client.tunnel_status()
        except Exception as exc:
            self._emit_system(f"获取隧道状态失败：{exc}")
            return
        lines = ["隧道状态："]
        for key in ("configured", "parent", "tunnel_id", "enabled", "state",
                    "env_id", "detail"):
            lines.append(f"- {key}: {st.get(key)}")
        lines.append(f"- 徽标: {ui_mod.tunnel_badge(st) or '（无）'}")
        self._emit_system("\n".join(lines))

    def _cmd_plain(self, args, rest) -> None:
        self.plain = not self.plain
        if self.plain:
            self._emit_system("已切换到 plain 模式（再次 /plain 返回 TUI）。")
        else:
            self._emit_system("已切换回 TUI 模式。")

    def _cmd_help(self, args, rest) -> None:
        content = help_lines()
        if self.plain or self._ui is None:
            self._emit_system("\n".join(content))
            return
        self._ui.modal = ui_mod.TextPage("帮助 / 键位", content)

    def _cmd_exit(self, args, rest) -> None:
        self.running = False


# ══════════════════════════════════════════════════════════════════════════
# Public entry point (frozen contract §4.3)
# ══════════════════════════════════════════════════════════════════════════

def run(client, opts: TuiOptions) -> int:
    """TUI 主入口。返回进程退出码。阻塞直到退出。"""
    return App(client, opts).run()
