"""客户端工具等待态在「会话状态层」的语义（第三阶段需求 6 的后端回归）。

侧边栏要显示的是**会话轮次**的状态，不是某一次 inference generation 的状态：

    streaming ──(模型请求客户端工具)──> client_tool_calls ──(浏览器回传结果)──>
        streaming ──…──> done_success_unread / done_error_unread

其中关键的是 client_tool_calls 这一格：generation 已经结束，但轮次还开着（结果
必须由浏览器补回来）。若 finally 里的绿色 done_success_unread 把它覆盖掉，侧边栏
会先绿后橙，用户会以为这一轮已经结束、不再去点结果。因此：

  * 绿色不得覆盖等待态；等待态会把绿色替换成自己重新广播（订阅者不会停在绿色）；
  * 红色 done_error_unread 必须覆盖 —— 中断真的结束了这一轮，必须可见；
  * 新一轮 generation 接管后（浏览器已回传结果），绿色照常生效；
  * 标记已读只清未读徽标，不清等待态；
  * 等待态的 status 帧带 index.json 里的权威标题，保证每个分类显示同一个标题；
  * 等待态属于未读集合（有徽标），同时 session_stream_is_live 为假 —— 否则
    浏览器回传结果的续跑请求会被 409 挡在门外。
"""
from __future__ import annotations

import json
import threading

import runtime.server_state as ss


SID = "client-tool-status-sid"


def _parse_frames(frames: list[str]) -> list[dict]:
    out = []
    for raw in frames:
        line = raw.strip()
        assert line.startswith("data: "), raw
        out.append(json.loads(line[len("data: "):]))
    return out


def _statuses_seen(frames: list[str]) -> list[str]:
    return [e["status"] for e in _parse_frames(frames)
            if e.get("event") == "message" and "status" in e]


class _Env:
    """隔离一个 session 的状态，并在退出时清干净（模块级状态是进程共享的）。"""

    def __enter__(self):
        self.frames: list[str] = []
        self.titles = ss._session_title_provider

        def send(frame: str) -> bool:
            self.frames.append(frame)
            return True

        self.send = send
        ss._session_event_subscribers.append(send)
        return self

    def __exit__(self, *exc):
        ss._session_event_subscribers.remove(self.send)
        ss._session_streams.pop(SID, None)
        ss._session_statuses.pop(SID, None)
        ss._unread_sessions.pop(SID, None)
        ss.set_session_title_provider(self.titles)
        return False


def test_waiting_status_survives_the_green_terminal_transition():
    """generation 结束时的绿色被替换回等待态：状态、未读集合、广播帧三处一致。"""
    cancel = threading.Event()
    with _Env() as env:
        assert ss.begin_session_stream(SID, cancel) is None
        assert ss.transition_session_stream_status(SID, cancel, "streaming") is True
        assert ss.get_session_status(SID) == "streaming"

        # 工具轮交接：后端在写 handoff 帧后立即进入等待态
        assert ss.transition_session_stream_status(
            SID, cancel, ss.SESSION_STATUS_CLIENT_TOOL_CALLS) is True
        assert ss.get_session_status(SID) == ss.SESSION_STATUS_CLIENT_TOOL_CALLS
        assert ss._unread_sessions.get(SID) == ss.SESSION_STATUS_CLIENT_TOOL_CALLS
        # generation 已经结束，但这一轮没有结束：请求门控必须放行续跑
        cancel.set()
        ss.finish_session_stream(SID, cancel)
        assert ss.session_stream_is_live(SID) is False

        # finally 里的绿色收尾
        assert ss.transition_session_stream_status(
            SID, cancel, "done_success_unread") is True
        assert ss.get_session_status(SID) == ss.SESSION_STATUS_CLIENT_TOOL_CALLS
        assert ss._unread_sessions.get(SID) == ss.SESSION_STATUS_CLIENT_TOOL_CALLS

        seen = _statuses_seen(env.frames)
        assert seen[-1] == ss.SESSION_STATUS_CLIENT_TOOL_CALLS, seen
        assert "done_success_unread" not in seen, seen
        # 一次真实交接 + 一次「绿色被换成等待态」的纠正广播，没有第三条：
        # 徽标不会反复清空再点亮（闪烁），也不会停在绿色。
        assert seen.count(ss.SESSION_STATUS_CLIENT_TOOL_CALLS) == 2, seen
        assert ss._unread_sessions.get(SID) == ss.SESSION_STATUS_CLIENT_TOOL_CALLS


def test_abort_still_turns_the_row_red():
    """红色不能被等待态吞掉：中断真的结束了这一轮，用户必须看到。"""
    cancel = threading.Event()
    with _Env() as env:
        assert ss.begin_session_stream(SID, cancel) is None
        ss.transition_session_stream_status(SID, cancel, "streaming")
        ss.transition_session_stream_status(SID, cancel, ss.SESSION_STATUS_CLIENT_TOOL_CALLS)

        assert ss.transition_session_stream_status(SID, cancel, "done_error_unread") is True
        assert ss.get_session_status(SID) == "done_error_unread"
        assert ss._unread_sessions.get(SID) == "done_error_unread"
        assert _statuses_seen(env.frames)[-1] == "done_error_unread"
        cancel.set()


def test_continuation_can_finish_green_normally():
    """浏览器回传结果后新一轮 generation 接管，正常完成时绿色照常生效。"""
    first = threading.Event()
    with _Env() as env:
        assert ss.begin_session_stream(SID, first) is None
        ss.transition_session_stream_status(SID, first, "streaming")
        ss.transition_session_stream_status(SID, first, ss.SESSION_STATUS_CLIENT_TOOL_CALLS)
        first.set()
        ss.finish_session_stream(SID, first)

        # 续跑：同一个 session 的新 generation
        second = threading.Event()
        assert ss.begin_session_stream(SID, second) is None
        assert ss.transition_session_stream_status(SID, second, "streaming") is True
        assert ss.get_session_status(SID) == "streaming"
        assert ss.transition_session_stream_status(SID, second, "done_success_unread") is True
        assert ss.get_session_status(SID) == "done_success_unread"
        assert ss._unread_sessions.get(SID) == "done_success_unread"
        assert _statuses_seen(env.frames)[-1] == "done_success_unread"
        second.set()


def test_waiting_frame_carries_the_canonical_title():
    """等待态的 status 帧带 index.json 的权威标题：每个分类显示同一个标题。"""
    cancel = threading.Event()
    ss.set_session_title_provider(
        lambda sid: {"title": "标签来的会话", "title_given": False})
    with _Env() as env:
        assert ss.begin_session_stream(SID, cancel) is None
        ss.transition_session_stream_status(SID, cancel, ss.SESSION_STATUS_CLIENT_TOOL_CALLS)
        frames = [e for e in _parse_frames(env.frames) if e.get("status")
                  == ss.SESSION_STATUS_CLIENT_TOOL_CALLS]
        assert frames, env.frames
        assert frames[-1]["title"] == "标签来的会话"
        assert frames[-1]["title_given"] is False
        cancel.set()


def test_waiting_status_is_part_of_the_unread_set():
    """等待态计入未读集合：跨分类的徽标语义与 done_* 一致（有事要处理）。"""
    assert ss.SESSION_STATUS_CLIENT_TOOL_CALLS in ss.UNREAD_SESSION_STATUSES
    # 但绝不是普通的终态：状态常量本身可与 done_* 区分，前端才能显示蓝色
    assert ss.SESSION_STATUS_CLIENT_TOOL_CALLS not in ("done_success_unread",
                                                       "done_error_unread")


def test_mark_read_keeps_the_waiting_status_and_clears_only_the_badge():
    """已读只清徽标，等待信号必须留着（handler_api 的 settled_to 规则）。

    这里复现 handler_api._handle_mark_session_read 的关键分支：未读里有该会话时
    把它移除，但若实时状态是等待态，状态保持 client_tool_calls 而不是 idle。
    """
    cancel = threading.Event()
    with _Env() as env:
        assert ss.begin_session_stream(SID, cancel) is None
        ss.transition_session_stream_status(SID, cancel, ss.SESSION_STATUS_CLIENT_TOOL_CALLS)
        assert ss._unread_sessions.get(SID) == ss.SESSION_STATUS_CLIENT_TOOL_CALLS

        # handler_api:3125 的同一段判断（已读处理）
        with ss._session_state_lock:
            ss._unread_sessions.pop(SID, None)
            if ss._session_statuses.get(SID) == ss.SESSION_STATUS_CLIENT_TOOL_CALLS:
                settled_to = ss.SESSION_STATUS_CLIENT_TOOL_CALLS
            else:
                settled_to = "idle"
            ss._session_statuses[SID] = settled_to

        assert ss._unread_sessions.get(SID) is None
        assert ss.get_session_status(SID) == ss.SESSION_STATUS_CLIENT_TOOL_CALLS
        # 列表 / 快照读到的仍是等待态，切分类不会变灰
        assert dict(ss._session_statuses)[SID] == ss.SESSION_STATUS_CLIENT_TOOL_CALLS
        cancel.set()
