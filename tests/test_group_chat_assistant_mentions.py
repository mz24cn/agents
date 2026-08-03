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
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from runtime.models import Message, InferenceRequest
from runtime.group_chat import run_group_chat_stream, parse_mentions, resolve_mentions


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
