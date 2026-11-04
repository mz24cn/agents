"""The verbatim window a compressed session replays has exactly one authority.

Regression for a real group-chat session (1381 turns, ``summarized_up_to_turn:
1368``) that replayed 96 turns / 711 996 characters of transcript to a model with
a 196 608-token window and was rejected with

    HTTP 400: request exceeds the context window: prompt (471777 tokens) leaves
    no room to answer in the context (196608); requests are never truncated

Group chat kept a private copy of the "recent K turns" arithmetic, and that copy
had no floor: its "walk back to the start of this turn" loop walked from turn
1371 all the way back to the previous user message at turn 1285, because the
manager had run 94 tool rounds (``MAX_TOOL_ROUNDS=500``) with no new user turn.
The single-agent path had always been floored by ``summarized_up_to_turn + 1``.
"""

from __future__ import annotations

import os

from runtime.context_manager import ContextManager, ConversationTurn


def _turn(role: str, content: str = "", **kwargs) -> ConversationTurn:
    return ConversationTurn(role=role, content=content,
                            timestamp="2026-11-04T11:18:46", **kwargs)


def _tool_chain(total: int, user_at=(0,)) -> list:
    """One long tool chain; user turns only at the indexes in *user_at*."""
    user_at = set(user_at)
    turns: list = []
    for index in range(total):
        if index in user_at:
            turns.append(_turn("user", f"问题 {index}"))
        elif index and turns[index - 1].role == "assistant":
            turns.append(_turn("tool", f"输出 {index} " + "x" * 400,
                               tool_use_id=f"call_{index - 1}"))
        else:
            turns.append(_turn("assistant", f"调用 {index}",
                               tool_calls=[{"id": f"call_{index}", "name": "exec_shell",
                                            "arguments": "{}"}]))
    return turns


def _manager(tmp_path, recent_turns_k: int = 10) -> ContextManager:
    return ContextManager(infer_fn=lambda *a, **k: "", chats_dir=str(tmp_path),
                          recent_turns_k=recent_turns_k)


def _old_group_chat_walk(turns: list, summarized_up_to: int, k: int) -> int:
    """The removed group-chat copy of the window arithmetic, for contrast."""
    recent_start = max(0, len(turns) - k)
    if recent_start <= summarized_up_to:
        recent_start = min(summarized_up_to + 1, len(turns))
    while (recent_start < len(turns) and recent_start > 0
           and turns[recent_start].role == "tool"):
        recent_start -= 1
    while (recent_start < len(turns) and recent_start > 0
           and turns[recent_start].role not in ("user", "system")):
        recent_start -= 1
    return recent_start


def _start_index(turns: list, window: list) -> int:
    if not window:
        return len(turns)
    head = window[0]
    for index, turn in enumerate(turns):
        if turn is head:
            return index
    raise AssertionError("window is not a slice of turns")


def test_window_never_replays_what_the_summary_already_covers(tmp_path):
    # The broken session's shape: a user turn at 1285, then 96 rounds of pure
    # tool traffic; the summary covers everything up to and including 1368.
    turns = _tool_chain(1381, user_at=(0, 1285))
    fm = {"summarized_up_to_turn": 1368}
    cm = _manager(tmp_path)

    window = cm.select_unsummarized_turns(turns, fm)

    start = _start_index(turns, window)
    # 1368 is allowed for one reason only: it is the assistant(tool_use) that owns
    # turn 1369's tool result, and a window may not open with an orphan result.
    assert start >= 1369 or (start == 1368 and turns[start].tool_calls), \
        "the window replayed turns the summary already covers"
    # K = 10, widened only by the tool chain the window had to complete: the same
    # 13 turns / ~80k characters the floored single-agent path yields.
    assert len(window) <= 13

    # Keep the contrast executable rather than narrated: the removed private copy
    # walked back to turn 1285 and re-sent 96 turns.
    old_start = _old_group_chat_walk(turns, 1368, 10)
    assert old_start == 1285
    assert len(turns) - old_start == 96
    replayed = sum(len(t.content) for t in turns[old_start:])
    sent = sum(len(t.content) for t in window)
    assert replayed > 5 * sent, "the private copy replayed what the summary covered"


def test_window_backtracks_to_the_turn_start_but_not_below_the_floor(tmp_path):
    # K lands inside a round whose opening user turn is above the floor: the whole
    # round is taken, because a window may not begin mid-exchange.
    turns = _tool_chain(30, user_at=(0, 21))
    cm = _manager(tmp_path, recent_turns_k=4)

    window = cm.select_unsummarized_turns(turns, {"summarized_up_to_turn": 15})

    start = _start_index(turns, window)
    assert start >= 16, "walked below the floor"
    assert turns[start].role == "user", "window begins mid tool chain"


def test_window_crosses_the_floor_only_to_keep_a_tool_chain_whole(tmp_path):
    # The floor itself points at a tool result whose assistant(tool_use) is the
    # last summarized turn: protocol integrity beats the floor, and only then
    # (an orphan tool result is rejected outright by e.g. Anthropic).
    turns = _tool_chain(12, user_at=(0,))
    turns[10] = _turn("assistant", "调用 10",
                      tool_calls=[{"id": "call_10", "name": "exec_shell",
                                   "arguments": "{}"}])
    turns[11] = _turn("tool", "输出 11", tool_use_id="call_10")
    cm = _manager(tmp_path, recent_turns_k=1)

    window = cm.select_unsummarized_turns(turns, {"summarized_up_to_turn": 9})

    assert _start_index(turns, window) == 10
    assert window[0].role == "assistant" and window[0].tool_calls

    # Nothing to pair with: the orphan tool results are skipped instead, and the
    # caller's user-anchor guarantee re-anchors the request.
    orphaned = [_turn("tool", f"孤立输出 {i}", tool_use_id=f"x{i}")
                for i in range(12)]
    assert cm.select_unsummarized_turns(orphaned, {"summarized_up_to_turn": 9}) == []


def test_dict_shaped_turns_are_accepted(tmp_path):
    # Group chat appends round-2+ turns to its in-memory history as plain dicts.
    turns = [{"role": "user", "content": "问题"},
             {"role": "assistant", "content": "调用", "tool_calls": [
                 {"id": "c1", "name": "exec_shell", "arguments": "{}"}]},
             {"role": "tool", "content": "输出", "tool_use_id": "c1"}]
    cm = _manager(tmp_path, recent_turns_k=1)

    window = cm.select_unsummarized_turns(turns, {"summarized_up_to_turn": -1})

    assert [t["role"] for t in window] == ["user", "assistant", "tool"]


def test_leading_system_turn_is_skipped(tmp_path):
    # A system turn at the head of the window would become a second system
    # message; the caller merges system content into its single system message.
    turns = [_turn("user", "问题"), _turn("system", "群聊框架"), _turn("assistant", "答")]
    cm = _manager(tmp_path, recent_turns_k=1)

    window = cm.select_unsummarized_turns(turns, {})

    assert window and window[0].role != "system"


def test_no_summary_keeps_the_whole_history(tmp_path):
    turns = _tool_chain(30)
    cm = _manager(tmp_path, recent_turns_k=4)

    assert cm.select_unsummarized_turns(turns, {}) == turns
    assert cm.select_unsummarized_turns(turns, None) == turns
    assert cm.select_unsummarized_turns([], {"summarized_up_to_turn": 5}) == []


def test_unusable_front_matter_never_opens_the_window_early(tmp_path):
    turns = _tool_chain(30, user_at=(0,))
    cm = _manager(tmp_path, recent_turns_k=4)

    for bad in ({"summarized_up_to_turn": "12"}, {"summarized_up_to_turn": True},
                {"summarized_up_to_turn": -50}, {}):
        window = cm.select_unsummarized_turns(turns, bad)
        start = _start_index(turns, window)
        assert start == 0 or start >= len(turns) - 4 - 2, bad


def test_assemble_context_goes_through_the_same_window(tmp_path):
    """assemble_context must delegate: one authority, no second implementation."""

    class _Spy(ContextManager):
        windows: list = []

        def select_unsummarized_turns(self, turns, summary_fm):
            window = super().select_unsummarized_turns(turns, summary_fm)
            _Spy.windows.append(list(window))
            return window

    session = "s-window"
    cm = _Spy(infer_fn=lambda *a, **k: "", chats_dir=str(tmp_path), recent_turns_k=4)
    turns = _tool_chain(40, user_at=(0,))
    cm.save_conversation(session, turns)
    with open(os.path.join(str(tmp_path), session, "summary.md"), "w",
              encoding="utf-8") as fh:
        fh.write("---\nsummary_version: 3\nsummarized_up_to_turn: 33\n---\n\n早前对话摘要。\n")

    assembled = cm.assemble_context(session, [{"role": "user", "content": "继续"}])

    assert len(_Spy.windows) == 1, "assemble_context no longer uses the shared window"
    window_contents = [t.content for t in _Spy.windows[0]]
    replayed = [m.get("content") for m in assembled
                if m.get("role") in ("user", "assistant", "tool")]

    # Exactly the shared window + this call's new message, plus (at most) the
    # anchor user turn the caller prepends when the window holds no user message.
    assert replayed[-1] == "继续"
    assert replayed[:-1] in (window_contents, ["问题 0"] + window_contents)


class _FakeAgentManager:
    def __init__(self, agents: list):
        self._by_id = {a["agent_id"]: a for a in agents}

    def get(self, key: str):
        for a in self._by_id.values():
            if a.get("nickname") == key:
                return a
        return self._by_id.get(key)


def test_group_chat_replays_the_shared_window(tmp_path):
    """The group-chat path must not carry its own window arithmetic any more."""
    import threading

    from runtime.group_chat import run_group_chat_stream_gen
    from runtime.models import InferenceRequest, Message

    session = "261103_132046"
    cm = _manager(tmp_path)
    turns = _tool_chain(1381, user_at=(0, 1285))
    cm.save_conversation(session, turns)
    with open(os.path.join(str(tmp_path), session, "summary.md"), "w",
              encoding="utf-8") as fh:
        fh.write("---\nsummary_version: 17\nsummarized_up_to_turn: 1368\n---\n\n"
                 "这个会话在做一个长任务。\n")

    captured: list = []

    class _CapturingRuntime:
        def infer_stream(self, request, cancel_event=None, **_pipeline_kwargs):
            captured.append(request)
            yield Message(role="assistant", content="完成", agent_id="Manager")
            yield Message(role="usage", content="{}")

    list(run_group_chat_stream_gen(
        runtime=_CapturingRuntime(),
        mentioned_agent_ids=["Manager"],
        all_agent_ids=["Manager"],
        original_messages=[Message(role="user", content="继续")],
        base_request=InferenceRequest(model_id="m", tool_ids=[], stream=True),
        cancel_event=threading.Event(),
        sse_callback=None,
        sse_heartbeat=None,
        context_manager=cm,
        session_id=session,
        agent_manager=_FakeAgentManager([
            {"agent_id": "Manager", "nickname": "经理", "system_prompt": "你是经理",
             "model_id": "m", "tool_ids": []},
        ]),
        model_id="m",
        tool_ids=[],
        max_rounds=1,
    ))

    assert captured, "the agent was never asked to run"
    request = captured[0]
    characters = sum(len(m.content or "") + len(m.render_suffix or "")
                     for m in request.messages)
    # 13 turns of window (~80k characters in the broken session) instead of 96
    # turns / 711 996 characters.
    assert len(request.messages) <= 20, [m.role for m in request.messages]
    assert characters < 30_000, characters
