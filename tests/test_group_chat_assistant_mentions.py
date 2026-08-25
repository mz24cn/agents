"""End-to-end test: assistant replies containing valid @-mentions should
drive round 2+ participation exactly like user messages containing
@-mentions drive round 1.

Scenario:
    user  -> "@SunWuKong 你好"            (mentions SunWuKong)
    round1: SunWuKong replies "@ShaWuJing 你怎么看"   (mentions ShaWuJing)
    round2: ShaWuJing should be triggered and reply
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from runtime.common import get_request_context
from runtime.models import Message, InferenceRequest
from runtime.group_chat import (
    run_group_chat_stream,
    run_group_chat_stream_gen,
    parse_mentions,
    resolve_mentions,
    route_group_chat_user_message,
)


class FakeAgentManager:
    """Minimal agent manager: get() resolves by id or nickname."""

    def __init__(self, agents: list[dict]):
        self._by_id = {a["agent_id"]: a for a in agents}

    def get(self, key: str):
        # nickname first, then id
        for a in self._by_id.values():
            if a.get("nickname") == key:
                return a
        return self._by_id.get(key)


class FakeRuntime:
    """Returns pre-scripted assistant replies per inference call."""

    def __init__(self, replies_by_call: list[str]):
        self.replies_by_call = list(replies_by_call)
        self.call_count = 0
        self.seen_requests: list[InferenceRequest] = []

    def infer_stream(self, request: InferenceRequest, cancel_event=None):
        self.call_count += 1
        self.seen_requests.append(request)
        idx = self.call_count - 1
        if idx < len(self.replies_by_call):
            content = self.replies_by_call[idx]
        else:
            content = "（无更多回复）"
        yield Message(role="assistant", content=content)


class ToolRoundRuntime:
    """One complete assistant(tool_calls) + usage + tool + final reply round."""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name

    def infer_stream(self, request: InferenceRequest, cancel_event=None):
        yield Message(
            role="assistant",
            tool_calls=[{
                "id": "call_talk_1",
                "name": self.tool_name,
                "arguments": "{}",
            }],
        )
        yield Message(role="usage", name="round", content="{}")
        yield Message(
            role="tool",
            name=self.tool_name,
            tool_id=self.tool_name,
            tool_use_id="call_talk_1",
            content="ShaWuJing replied",
        )
        yield Message(role="assistant", content="done")
        yield Message(role="usage", name="round", content="{}")


class NestedStreamingToolRuntime:
    """Emits nested-tool UI frames through the request-context callback."""

    def infer_stream(self, request: InferenceRequest, cancel_event=None):
        yield Message(
            role="assistant",
            tool_calls=[{
                "id": "call_nested_1",
                "name": "talk_to",
                "arguments": "{}",
            }],
        )
        callback = get_request_context("sse_callback")
        assert callback is not None
        callback({
            "role": "tool",
            "name": "talk_to",
            "tool_use_id": "call_nested_1",
            "streaming": True,
            "delta": "partial",
            "agent_id": "SunWuKong",
            "target_agent_id": "ShaWuJing",
            "target_agent_nickname": "沙和尚",
        })
        callback({
            "role": "tool",
            "name": "talk_to",
            "tool_use_id": "call_nested_1",
            "streaming": False,
            "agent_id": "SunWuKong",
        })
        yield Message(
            role="tool",
            name="talk_to",
            tool_id="talk_to",
            tool_use_id="call_nested_1",
            content="formal result",
        )


class ChunkedRuntime:
    """Simulates REAL streaming: assistant text arrives as token deltas, so
    an @-mention may be split across chunks ("@沙" + "和尚 你怎么看？")."""

    def __init__(self, chunk_sets: list[list[str]]):
        self.chunk_sets = chunk_sets
        self.call_count = 0
        self.seen_requests: list[InferenceRequest] = []

    def infer_stream(self, request: InferenceRequest, cancel_event=None):
        self.call_count += 1
        self.seen_requests.append(request)
        idx = self.call_count - 1
        if idx < len(self.chunk_sets):
            for c in self.chunk_sets[idx]:
                yield Message(role="assistant", content=c)
        else:
            yield Message(role="assistant", content="（无更多回复）")


AGENTS = [
    {
        "agent_id": "SunWuKong",
        "nickname": "孙悟空",
        "description": "齐天大圣",
        "system_prompt": "你是孙悟空。",
        "model_id": "m1",
        "tool_ids": [],
    },
    {
        "agent_id": "ShaWuJing",
        "nickname": "沙和尚",
        "description": "卷帘大将",
        "system_prompt": "你是沙和尚。",
        "model_id": "m1",
        "tool_ids": [],
    },
    {
        "agent_id": "ZhuBaJie",
        "nickname": "猪八戒",
        "description": "天蓬元帅",
        "system_prompt": "你是猪八戒。",
        "model_id": "m1",
        "tool_ids": [],
    },
]


def _make_req(first_user: str, mentioned: list[str]) -> InferenceRequest:
    return InferenceRequest(
        model_id="m1",
        tool_ids=[],
        messages=[Message(role="user", content=first_user, mentions=mentioned)],
        stream=True,
    )


def test_group_chat_retry_targets_only_retry_agent(tmp_path):
    """A retry roster may contain many agents, but only retry_agent_id runs."""
    from runtime.context_manager import ContextManager, ConversationTurn

    cm = ContextManager(infer_fn=lambda req: None, chats_dir=str(tmp_path))
    session_id = cm.create_session()
    cm.save_conversation(session_id, [
        ConversationTurn(
            role="system", content="group", timestamp="t0",
        ),
        ConversationTurn(
            role="user", content="question", timestamp="t1",
        ),
    ])
    runtime = FakeRuntime(["replacement"])
    manager = FakeAgentManager(AGENTS)

    output = list(run_group_chat_stream_gen(
        runtime=runtime,
        mentioned_agent_ids=["ShaWuJing"],
        all_agent_ids=["SunWuKong", "ShaWuJing", "ZhuBaJie"],
        original_messages=[],
        base_request=InferenceRequest(model_id="m1", messages=[]),
        context_manager=cm,
        session_id=session_id,
        agent_manager=manager,
        model_id="m1",
        tool_ids=[],
    ))

    assistant_output = [msg for msg in output if msg.role == "assistant"]
    assert runtime.call_count == 1
    assert assistant_output
    assert {msg.agent_id for msg in assistant_output} == {"ShaWuJing"}


@pytest.mark.parametrize("tool_name", ["talk_to", "delegate"])
def test_group_chat_generator_yields_self_streaming_tool_result_with_canonical_id(tool_name):
    am = FakeAgentManager(AGENTS)
    runtime = ToolRoundRuntime(tool_name)

    messages = list(run_group_chat_stream_gen(
        runtime=runtime,
        mentioned_agent_ids=["SunWuKong"],
        all_agent_ids=["SunWuKong", "ShaWuJing", "ZhuBaJie"],
        original_messages=[Message(role="user", content="@SunWuKong ask")],
        base_request=_make_req("@SunWuKong ask", ["SunWuKong"]),
        cancel_event=None,
        sse_callback=None,
        context_manager=None,
        session_id=None,
        agent_manager=am,
        model_id="m1",
        tool_ids=[],
        max_rounds=1,
    ))

    tool_messages = [m for m in messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0].name == tool_name
    assert tool_messages[0].tool_id == tool_name
    assert tool_messages[0].tool_use_id == "call_talk_1"


def test_group_chat_nested_stream_frames_are_ordered_and_not_persisted():
    am = FakeAgentManager(AGENTS)
    events = []

    messages = list(run_group_chat_stream_gen(
        runtime=NestedStreamingToolRuntime(),
        mentioned_agent_ids=["SunWuKong"],
        all_agent_ids=["SunWuKong", "ShaWuJing", "ZhuBaJie"],
        original_messages=[Message(role="user", content="@SunWuKong ask")],
        base_request=_make_req("@SunWuKong ask", ["SunWuKong"]),
        cancel_event=None,
        sse_callback=lambda frame: events.append(frame),
        context_manager=None,
        session_id=None,
        agent_manager=am,
        model_id="m1",
        tool_ids=[],
        max_rounds=1,
    ))

    # Ephemeral frames reach the SSE callback in production order.
    assert [event["streaming"] for event in events] == [True, False]
    assert events[0]["target_agent_id"] == "ShaWuJing"
    assert all(event["tool_use_id"] == "call_nested_1" for event in events)

    # They never enter the canonical message stream/history. Only the formal
    # result is yielded and eligible for persistence.
    assert [message.role for message in messages] == ["assistant", "tool"]
    assert messages[0].tool_calls[0]["id"] == "call_nested_1"
    assert messages[1].tool_use_id == "call_nested_1"
    assert messages[1].content == "formal result"


def test_parse_mentions_supports_chinese_nicknames_and_ids():
    assert parse_mentions("@孙悟空 @ShaWuJing 讨论一下") == ["孙悟空", "ShaWuJing"]
    assert parse_mentions("普通文本没有 @") == []


def test_resolve_mentions_by_nickname_and_id():
    am = FakeAgentManager(AGENTS)
    ids = ["SunWuKong", "ShaWuJing", "ZhuBaJie"]
    assert resolve_mentions(["孙悟空"], am, ids) == ["SunWuKong"]
    assert resolve_mentions(["ShaWuJing"], am, ids) == ["ShaWuJing"]
    assert resolve_mentions(["孙悟空", "猪八戒"], am, ids) == ["SunWuKong", "ZhuBaJie"]
    # unknown mention -> dropped
    assert resolve_mentions(["不存在的人"], am, ids) == []


def test_no_mention_routes_to_most_recent_responding_agent():
    am = FakeAgentManager(AGENTS)
    ids = ["SunWuKong", "ShaWuJing", "ZhuBaJie"]
    prior_turns = [
        Message(role="user", content="@all 都说说"),
        Message(role="assistant", content="俺老孙先说", agent_id="SunWuKong"),
        Message(role="assistant", content="我补充一下", agent_id="ShaWuJing"),
    ]

    assert route_group_chat_user_message(
        "继续说", am, ids, prior_turns
    ) == ["ShaWuJing"]


def test_explicit_mention_switches_away_from_previous_agent():
    am = FakeAgentManager(AGENTS)
    ids = ["SunWuKong", "ShaWuJing", "ZhuBaJie"]
    prior_turns = [
        Message(role="assistant", content="我在回答", agent_id="ShaWuJing"),
    ]

    assert route_group_chat_user_message(
        "@猪八戒 你来回答", am, ids, prior_turns
    ) == ["ZhuBaJie"]


def test_first_no_mention_routes_only_to_all_leaders():
    agents = [dict(agent) for agent in AGENTS]
    agents[0]["labels"] = ["leader"]
    agents[1]["labels"] = ["reviewer", "leader"]
    agents[2]["labels"] = []
    am = FakeAgentManager(agents)
    ids = ["SunWuKong", "ShaWuJing", "ZhuBaJie"]

    assert route_group_chat_user_message(
        "大家好", am, ids, []
    ) == ["SunWuKong", "ShaWuJing"]


def test_first_no_mention_without_leader_falls_back_to_all_agents():
    am = FakeAgentManager(AGENTS)
    ids = ["SunWuKong", "ShaWuJing", "ZhuBaJie"]

    assert route_group_chat_user_message(
        "大家好", am, ids, []
    ) == ids


def test_round2_with_persisted_conversation_turns_does_not_assume_dict(tmp_path):
    """Persisted history is loaded as ConversationTurn objects.

    When a round-1 assistant reply @-mentions another agent, round 2 excludes
    that trigger reply from replayed history.  The filtering path must support
    ConversationTurn objects as well as the dicts appended during this run.
    """
    from runtime.context_manager import ContextManager, ConversationTurn

    cm = ContextManager(infer_fn=lambda req: None, chats_dir=str(tmp_path))
    session_id = cm.create_session()
    cm.save_conversation(session_id, [
        ConversationTurn(role="system", content="group", timestamp="t0"),
        ConversationTurn(
            role="user",
            content="@SunWuKong 你好",
            timestamp="t1",
            mentions=["SunWuKong"],
        ),
    ])
    am = FakeAgentManager(AGENTS)
    runtime = FakeRuntime([
        "@ShaWuJing 你怎么看？",
        "我觉得有道理。",
    ])

    collected = run_group_chat_stream(
        runtime=runtime,
        mentioned_agent_ids=["SunWuKong"],
        all_agent_ids=["SunWuKong", "ShaWuJing", "ZhuBaJie"],
        original_messages=[Message(role="user", content="@SunWuKong 你好")],
        base_request=_make_req("@SunWuKong 你好", ["SunWuKong"]),
        cancel_event=None,
        sse_callback=None,
        context_manager=cm,
        session_id=session_id,
        agent_manager=am,
        model_id="m1",
        tool_ids=[],
        max_rounds=5,
    )

    assistant_msgs = [m for m in collected if m.role == "assistant"]
    assert [m.agent_id for m in assistant_msgs] == ["SunWuKong", "ShaWuJing"]
    assert runtime.call_count == 2


def test_assistant_mention_triggers_round2():
    am = FakeAgentManager(AGENTS)
    runtime = FakeRuntime([
        "@ShaWuJing 你怎么看？",   # SunWuKong's round-1 reply mentions ShaWuJing
        "我觉得有道理。",          # ShaWuJing's round-2 reply
    ])

    collected = run_group_chat_stream(
        runtime=runtime,
        mentioned_agent_ids=["SunWuKong"],
        all_agent_ids=["SunWuKong", "ShaWuJing", "ZhuBaJie"],
        original_messages=[Message(role="user", content="@SunWuKong 你好")],
        base_request=_make_req("@SunWuKong 你好", ["SunWuKong"]),
        cancel_event=None,
        sse_callback=None,
        context_manager=None,
        session_id=None,
        agent_manager=am,
        model_id="m1",
        tool_ids=[],
        max_rounds=5,
    )

    assistant_msgs = [m for m in collected if m.role == "assistant"]
    agents_that_replied = [getattr(m, "agent_id", None) for m in assistant_msgs]
    assert agents_that_replied == ["SunWuKong", "ShaWuJing"], agents_that_replied
    # ShaWuJing's context (2nd request) should contain SunWuKong's @-reply
    # as the final user-visible message.
    req2 = runtime.seen_requests[1]
    assert req2.messages[-1].role == "user"
    assert "@ShaWuJing" in req2.messages[-1].content


def test_invalid_mention_in_assistant_reply_does_not_trigger():
    am = FakeAgentManager(AGENTS)
    runtime = FakeRuntime([
        "没有 @ 任何人的普通回复",
    ])

    collected = run_group_chat_stream(
        runtime=runtime,
        mentioned_agent_ids=["SunWuKong"],
        all_agent_ids=["SunWuKong", "ShaWuJing", "ZhuBaJie"],
        original_messages=[Message(role="user", content="@SunWuKong 你好")],
        base_request=_make_req("@SunWuKong 你好", ["SunWuKong"]),
        cancel_event=None,
        sse_callback=None,
        context_manager=None,
        session_id=None,
        agent_manager=am,
        model_id="m1",
        tool_ids=[],
        max_rounds=5,
    )

    assistant_msgs = [m for m in collected if m.role == "assistant"]
    agents_that_replied = [getattr(m, "agent_id", None) for m in assistant_msgs]
    assert agents_that_replied == ["SunWuKong"], agents_that_replied


def test_round2_trigger_message_has_identity_prefix_not_user_prefix():
    """The @-trigger message presented to ShaWuJing must carry the
    *mentioning agent's* identity (**孙悟空** (SunWuKong): ...), NOT the
    **用户** (user): prefix that real user speech gets."""
    am = FakeAgentManager(AGENTS)
    runtime = FakeRuntime([
        "@ShaWuJing 你怎么看？",
        "好的，我明白了。",
    ])

    collected = run_group_chat_stream(
        runtime=runtime,
        mentioned_agent_ids=["SunWuKong"],
        all_agent_ids=["SunWuKong", "ShaWuJing", "ZhuBaJie"],
        original_messages=[Message(role="user", content="@SunWuKong 你好")],
        base_request=_make_req("@SunWuKong 你好", ["SunWuKong"]),
        cancel_event=None,
        sse_callback=None,
        context_manager=None,
        session_id=None,
        agent_manager=am,
        model_id="m1",
        tool_ids=[],
        max_rounds=5,
    )

    req2 = runtime.seen_requests[1]
    last = req2.messages[-1]
    assert "**孙悟空** (SunWuKong):" in last.content
    assert "**用户** (user):" not in last.content


def test_streamed_chunks_aggregated_before_mention_scan():
    """Real streaming splits text into deltas — an @-mention can span chunks
    ('@沙' + '和尚 你怎么看？'). The aggregated reply must still trigger the
    mentioned agent, and the round-2 context must contain FULL text (no
    token fragments like '@沙')."""
    am = FakeAgentManager(AGENTS)
    runtime = ChunkedRuntime([
        ["我觉得", "@沙", "和尚 你怎么看？", "（完）"],   # SunWuKong round 1
        ["有道理", "，我听你的。"],                      # ShaWuJing round 2
    ])

    collected = run_group_chat_stream(
        runtime=runtime,
        mentioned_agent_ids=["SunWuKong"],
        all_agent_ids=["SunWuKong", "ShaWuJing", "ZhuBaJie"],
        original_messages=[Message(role="user", content="@SunWuKong 你好")],
        base_request=_make_req("@SunWuKong 你好", ["SunWuKong"]),
        cancel_event=None,
        sse_callback=None,
        context_manager=None,
        session_id=None,
        agent_manager=am,
        model_id="m1",
        tool_ids=[],
        max_rounds=5,
    )

    # Both agents replied (mention survived chunk boundaries)
    assistant_msgs = [m for m in collected if m.role == "assistant"]
    agents_that_replied = [getattr(m, "agent_id", None) for m in assistant_msgs]
    assert agents_that_replied == ["SunWuKong", "ShaWuJing"], agents_that_replied

    # The aggregated SunWuKong reply carries mentions
    swk = assistant_msgs[0]
    assert swk.mentions == ["ShaWuJing"], swk.mentions
    assert swk.content == "我觉得@沙和尚 你怎么看？（完）", repr(swk.content)

    # Round-2 context: full text, no token fragments, trigger present once
    req2 = runtime.seen_requests[1]
    joined = "\n".join(m.content or "" for m in req2.messages)
    assert "@沙和尚 你怎么看？" in joined
    assert "@沙" not in joined.replace("@沙和尚", "")
    assert joined.count("**孙悟空** (SunWuKong): 我觉得@沙和尚 你怎么看？（完）") == 1


def test_chain_trigger_across_three_rounds():
    """Chain: user @A → A replies @B → B replies @C → C participates.
    Verifies trigger_msgs rebuilds each round and round 3 gets B's reply as
    its current message."""
    am = FakeAgentManager(AGENTS)
    runtime = FakeRuntime([
        "@ShaWuJing 你先说",            # SunWuKong r1
        "@ZhuBaJie 八戒你觉得呢",        # ShaWuJing r2
        "俺老猪觉得可以。",              # ZhuBaJie r3
    ])

    collected = run_group_chat_stream(
        runtime=runtime,
        mentioned_agent_ids=["SunWuKong"],
        all_agent_ids=["SunWuKong", "ShaWuJing", "ZhuBaJie"],
        original_messages=[Message(role="user", content="@SunWuKong 开会")],
        base_request=_make_req("@SunWuKong 开会", ["SunWuKong"]),
        cancel_event=None,
        sse_callback=None,
        context_manager=None,
        session_id=None,
        agent_manager=am,
        model_id="m1",
        tool_ids=[],
        max_rounds=5,
    )

    assistant_msgs = [m for m in collected if m.role == "assistant"]
    replied = [getattr(m, "agent_id", None) for m in assistant_msgs]
    assert replied == ["SunWuKong", "ShaWuJing", "ZhuBaJie"], replied

    # 3 inference calls happened
    assert runtime.call_count == 3

    # Round 3 (ZhuBaJie) sees ShaWuJing's @-reply as its current message,
    # with ShaWuJing's identity — not the user's.
    req3 = runtime.seen_requests[2]
    last = req3.messages[-1]
    assert "**沙和尚** (ShaWuJing): @ZhuBaJie 八戒你觉得呢" in last.content
    assert "**用户** (user):" not in last.content.split(
        "**沙和尚** (ShaWuJing): @ZhuBaJie 八戒你觉得呢")[0]


def test_round2_mention_of_already_processed_agent_no_loop():
    """A mentions B; B replies back @A (already processed in round 1).
    A must NOT be pulled into round 3 — prevents infinite ping-pong."""
    am = FakeAgentManager(AGENTS)
    runtime = FakeRuntime([
        "@ShaWuJing 你怎么看？",
        "@SunWuKong 我觉得不错。",
    ])

    collected = run_group_chat_stream(
        runtime=runtime,
        mentioned_agent_ids=["SunWuKong"],
        all_agent_ids=["SunWuKong", "ShaWuJing", "ZhuBaJie"],
        original_messages=[Message(role="user", content="@SunWuKong 你好")],
        base_request=_make_req("@SunWuKong 你好", ["SunWuKong"]),
        cancel_event=None,
        sse_callback=None,
        context_manager=None,
        session_id=None,
        agent_manager=am,
        model_id="m1",
        tool_ids=[],
        max_rounds=5,
    )

    assistant_msgs = [m for m in collected if m.role == "assistant"]
    replied = [getattr(m, "agent_id", None) for m in assistant_msgs]
    assert replied == ["SunWuKong", "ShaWuJing"], replied
    assert runtime.call_count == 2


class HungRuntime:
    """Simulates an agent whose model backend never returns (hangs inside
    infer_stream), while other agents reply normally.

    The agent is identified by its per-agent system prompt marker, so the
    runtime knows which thread it is serving."""
    def __init__(self, hang_marker: str):
        self.hang_marker = hang_marker
        self.calls: list[str] = []

    def infer_stream(self, request: InferenceRequest, cancel_event=None):
        system = ""
        for m in request.messages:
            if m.role == "system":
                system = m.content or ""
                break
        self.calls.append(system)
        if self.hang_marker in system:
            while True:
                time.sleep(0.05)
                if cancel_event is not None and cancel_event.is_set():
                    yield Message(role="assistant",
                                  content="Error: user interrupted.")
                    return
        yield Message(role="assistant", content="\u6211\u8fd9\u8fb9\u4e00\u5207\u6b63\u5e38\u3002")


class SlowRuntime:
    """Simulates a slow agent that eventually completes."""

    def __init__(self, sleep_s: float):
        self.sleep_s = sleep_s

    def infer_stream(self, request: InferenceRequest, cancel_event=None):
        time.sleep(self.sleep_s)
        yield Message(role="assistant", content="\u7ec8\u4e8e\u5b8c\u6210\u4e86\u3002")


def test_group_chat_waits_for_slow_agents_without_infer_round_deadline():
    """Group chat has no independent model-inference wall-clock deadline."""
    import threading

    am = FakeAgentManager(AGENTS)
    runtime = SlowRuntime(sleep_s=0.15)

    start = time.monotonic()
    collected = run_group_chat_stream(
        runtime=runtime,
        mentioned_agent_ids=["SunWuKong", "ShaWuJing"],
        all_agent_ids=["SunWuKong", "ShaWuJing", "ZhuBaJie"],  # N = 3
        original_messages=[
            Message(role="user", content="@SunWuKong @ShaWuJing \u4f60\u4eec\u597d")],
        base_request=_make_req("@SunWuKong @ShaWuJing \u4f60\u4eec\u597d",
                               ["SunWuKong", "ShaWuJing"]),
        cancel_event=threading.Event(),
        sse_callback=None,
        context_manager=None,
        session_id=None,
        agent_manager=am,
        model_id="m1",
        tool_ids=[],
        max_rounds=5,
    )
    elapsed = time.monotonic() - start

    assert 0.1 <= elapsed < 3.0, f"elapsed={elapsed:.2f}s"

    by_agent = {
        getattr(m, "agent_id", None): (m.content or "")
        for m in collected if m.role == "assistant"
    }
    assert "\u7ec8\u4e8e\u5b8c\u6210" in by_agent.get("SunWuKong", ""), by_agent
    assert "\u7ec8\u4e8e\u5b8c\u6210" in by_agent.get("ShaWuJing", ""), by_agent
    assert not any("timed out" in (c or "") for c in by_agent.values()), by_agent


def test_group_chat_sse_heartbeat_during_long_wait(monkeypatch):
    """Slow agents still produce keep-alives while group chat waits."""
    import threading

    monkeypatch.setattr(
        "runtime.group_chat._GROUP_CHAT_HEARTBEAT_INTERVAL", 0.05)
    am = FakeAgentManager(AGENTS)
    runtime = SlowRuntime(sleep_s=0.2)
    heartbeats: list[float] = []

    collected = run_group_chat_stream(
        runtime=runtime,
        mentioned_agent_ids=["SunWuKong", "ShaWuJing"],
        all_agent_ids=["SunWuKong", "ShaWuJing", "ZhuBaJie"],  # N = 3
        original_messages=[
            Message(role="user", content="@SunWuKong @ShaWuJing \u4f60\u4eec\u597d")],
        base_request=_make_req("@SunWuKong @ShaWuJing \u4f60\u4eec\u597d",
                               ["SunWuKong", "ShaWuJing"]),
        cancel_event=threading.Event(),
        sse_callback=None,
        sse_heartbeat=lambda: heartbeats.append(time.monotonic()),
        context_manager=None,
        session_id=None,
        agent_manager=am,
        model_id="m1",
        tool_ids=[],
        max_rounds=5,
    )
    assert len(heartbeats) >= 2, f"only {len(heartbeats)} heartbeats"

    by_agent = {
        getattr(m, "agent_id", None): (m.content or "")
        for m in collected if m.role == "assistant"
    }
    assert "\u7ec8\u4e8e\u5b8c\u6210" in by_agent.get("SunWuKong", ""), by_agent


def test_model_api_timeout_default(monkeypatch):
    """MODEL_API_TIMEOUT is the model-call timeout; no round timeout remains."""
    from runtime.runtime import _get_model_api_timeout

    monkeypatch.delenv("MODEL_API_TIMEOUT", raising=False)
    assert _get_model_api_timeout() == 180
