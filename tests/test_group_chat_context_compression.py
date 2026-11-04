"""Group-chat agents compress their own session, mid-loop, without racing each other.

The failing session's log showed the context guard firing zero times: the
single-agent path hands ``infer_stream`` a compressor (``on_round_complete`` /
``on_context_overflow``) while group chat called it bare, so an agent could run
hundreds of tool rounds and re-send a growing context with nothing ever
compressing it -- the pre-send guard could only log a warning.

Group chat needs its own compressor, for two reasons:

* the request must be rebuilt from the AGENT's point of view (the session-centric
  ``assemble_context`` would hand one agent another agent's assistant turn as if
  it had said it itself);
* several agents share one session, so compression -- which rewrites summary.md
  and the conversation window -- has to be serialised per session.
"""

from __future__ import annotations

import json
import os
import threading

from runtime.context_manager import ContextManager
from runtime.group_chat import (
    GroupChatContextCompressor,
    _FrameWatermark,
    _compression_lock_for,
    run_group_chat_stream_gen,
)
from runtime.models import InferenceRequest, Message


AGENTS = [
    {"agent_id": "DevManager", "nickname": "开发经理", "system_prompt": "你是开发经理",
     "template_id": None, "template_arguments": {}, "tool_ids": [], "model_id": "agent-model"},
    {"agent_id": "CodeMaster", "nickname": "代码经理", "system_prompt": "你是代码经理",
     "template_id": None, "template_arguments": {}, "tool_ids": [], "model_id": "agent-model"},
]


class _Manager:
    def __init__(self, agents=AGENTS):
        self._by_id = {a["agent_id"]: a for a in agents}

    def get(self, key):
        for a in self._by_id.values():
            if a.get("nickname") == key:
                return a
        return self._by_id.get(key)


def _session(tmp_path, session="s-group") -> ContextManager:
    cm = ContextManager(infer_fn=lambda *a, **k: "摘要", chats_dir=str(tmp_path))
    messages = [
        {"role": "user", "content": "把采集流程改成增量的", "timestamp": "2026-11-04T11:00:00"},
        {"role": "assistant", "content": "我先看代码", "agent_id": "DevManager",
         "tool_calls": [{"id": "c1", "name": "read_file", "arguments": "{}"}],
         "timestamp": "2026-11-04T11:00:10"},
        {"role": "tool", "content": "文件内容", "agent_id": "DevManager",
         "tool_use_id": "c1", "name": "read_file", "timestamp": "2026-11-04T11:00:11"},
        {"role": "assistant", "content": "我这边看法不同", "agent_id": "CodeMaster",
         "timestamp": "2026-11-04T11:00:20"},
        {"role": "user", "content": "继续", "timestamp": "2026-11-04T11:00:30"},
    ]
    os.makedirs(os.path.join(str(tmp_path), session), exist_ok=True)
    with open(os.path.join(str(tmp_path), session, "conversation.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"meta": {}, "messages": messages}, fh)
    return cm


def _compressor(cm, session, agent_id="DevManager", ready=None):
    agent = next(a for a in AGENTS if a["agent_id"] == agent_id)
    return GroupChatContextCompressor(
        context_manager=cm,
        session_id=session,
        ready=ready,
        inference_model_id="agent-model",
        agent_id=agent_id,
        agent=agent,
        agents_markdown="| 用户 | user |\n| 开发经理 | DevManager |",
        dispatch_agents_markdown="| 代码经理 | CodeMaster |",
    )


def test_rebuild_is_the_agent_view_not_the_session_view(tmp_path):
    cm = _session(tmp_path)

    rebuilt = _compressor(cm, "s-group")._rebuild_from_disk()

    assert rebuilt, "nothing was rebuilt"
    roles = {c.splitlines()[0]: m.role for m in rebuilt for c in [m.content or ""]}
    # CodeMaster's turn must not become DevManager's own voice...
    assert roles.get("我这边看法不同") == "user", roles
    # ...while DevManager's own turn stays in the assistant role.
    assert roles.get("我先看代码") == "assistant", roles
    # Framing survived the rebuild. (A plain-prompt agent gets it in ``content``;
    # a template-backed agent in ``render_suffix`` -- see
    # tests/test_render_suffix_context.py for that half.)
    assert any("这是一场群聊对话" in ((m.content or "") + (m.render_suffix or ""))
               for m in rebuilt if m.role == "system"), [m.role for m in rebuilt]


def test_rebuild_applies_the_floored_window(tmp_path):
    session = "s-windowed"
    cm = ContextManager(infer_fn=lambda *a, **k: "摘要", chats_dir=str(tmp_path),
                        recent_turns_k=4)
    messages = [{"role": "user", "content": f"问题 {i}" if i % 3 == 0 else f"输出 {i}",
                 "timestamp": "2026-11-04T11:00:00"} for i in range(60)]
    os.makedirs(os.path.join(str(tmp_path), session), exist_ok=True)
    with open(os.path.join(str(tmp_path), session, "conversation.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"meta": {}, "messages": messages}, fh)
    with open(os.path.join(str(tmp_path), session, "summary.md"), "w",
              encoding="utf-8") as fh:
        fh.write("---\nsummary_version: 2\nsummarized_up_to_turn: 55\n---\n\n摘要。\n")

    rebuilt = _compressor(cm, session)._rebuild_from_disk()

    replayed = [m for m in rebuilt if m.role in ("user", "assistant", "tool")]
    assert len(replayed) <= 6, [m.content for m in replayed]
    assert all(int(m.content.split()[-1]) >= 56 for m in replayed if m.content[:5] in ("问题 ", "输出 "))


def test_compression_is_serialised_per_session():
    first = _compressor(None, "s-a")
    second = _compressor(None, "s-a")
    other = _compressor(None, "s-b")

    assert first._session_lock is second._session_lock
    assert first._session_lock is not other._session_lock
    assert _compression_lock_for("s-a") is first._session_lock


def test_two_agents_of_one_session_do_not_compress_concurrently(tmp_path):
    cm = _session(tmp_path)
    original_load = cm.load_conversation
    inside: list = []
    widest = [0]
    tracker = threading.Lock()

    def _loaded(*args, **kwargs):
        ident = threading.get_ident()
        with tracker:
            inside.append(ident)
            # Re-entry by the same thread is fine; two agents is the bug.
            widest[0] = max(widest[0], len(set(inside)))
        try:
            return original_load(*args, **kwargs)
        finally:
            with tracker:
                inside.remove(ident)

    cm.load_conversation = _loaded

    def _run(agent_id):
        compressor = _compressor(cm, "s-group", agent_id=agent_id)
        compressor([], 10 ** 9, 196_608)

    threads = [threading.Thread(target=_run, args=(a,)) for a in
               ("DevManager", "CodeMaster", "DevManager", "CodeMaster")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert widest[0] == 1, "two agents read/compressed the same session at once"


def test_watermark_gates_the_from_disk_rebuild(tmp_path):
    cm = _session(tmp_path)
    loads = []
    original_load = cm.load_conversation
    cm.load_conversation = lambda *a, **k: (loads.append(1), original_load(*a, **k))[1]

    watermark = _FrameWatermark()
    compressor = _compressor(
        cm, "s-group",
        ready=lambda: watermark.settled(),
    )

    watermark.produced()          # a frame is still travelling to the caller
    assert watermark.settled() is False
    assert compressor([], 10 ** 9, 196_608) is None
    replacement, after = compressor.ensure_within_budget(
        [Message(role="user", content="x" * 4000)], input_budget=10,
        estimate=lambda msgs: 5000, max_context=196_608)
    assert not loads, "rebuilt from disk while a frame was still unpersisted"

    watermark.consumed()
    assert watermark.settled() is True
    compressor([], 10 ** 9, 196_608)
    assert loads, "the compressor never read the session once it was safe"


def test_group_chat_hands_each_agent_its_own_compressor(tmp_path):
    cm = _session(tmp_path)
    seen = []

    class _HookRuntime:
        def infer_stream(self, request, cancel_event=None,
                         on_round_complete=None, on_context_overflow=None,
                         **_pipeline_kwargs):
            seen.append({
                "model_id": request.model_id,
                "round_hook": on_round_complete,
                "overflow_hook": on_context_overflow,
            })
            if on_round_complete is not None:
                # 400 000 tokens against a 196 608-token window: far past the
                # in-loop trigger, so the compressor must engage.
                on_round_complete(list(request.messages), 400_000, 196_608)
            yield Message(role="assistant", content="完成", agent_id="DevManager")
            yield Message(role="usage", content="{}")

    runtime = _HookRuntime()
    list(run_group_chat_stream_gen(
        runtime=runtime,
        mentioned_agent_ids=["DevManager"],
        all_agent_ids=["DevManager"],
        original_messages=[Message(role="user", content="继续")],
        base_request=InferenceRequest(model_id="model", tool_ids=[], stream=True),
        cancel_event=threading.Event(),
        sse_callback=None,
        sse_heartbeat=None,
        context_manager=cm,
        session_id="s-group",
        agent_manager=_Manager(),
        model_id="model",
        tool_ids=[],
        max_rounds=1,
    ))

    assert seen, "the agent never ran"
    entry = seen[0]
    assert isinstance(entry["round_hook"], GroupChatContextCompressor)
    assert entry["overflow_hook"] == entry["round_hook"].ensure_within_budget
    # The window is the AGENT's model, not the group request's model.
    assert entry["round_hook"].inference_model_id == "agent-model"
    assert entry["model_id"] == "agent-model"


def test_group_chat_without_a_session_runs_without_a_compressor(tmp_path):
    seen = []

    class _Runtime:
        def infer_stream(self, request, cancel_event=None,
                         on_round_complete=None, on_context_overflow=None,
                         **_pipeline_kwargs):
            seen.append((on_round_complete, on_context_overflow))
            yield Message(role="assistant", content="完成", agent_id="DevManager")
            yield Message(role="usage", content="{}")

    list(run_group_chat_stream_gen(
        runtime=_Runtime(),
        mentioned_agent_ids=["DevManager"],
        all_agent_ids=["DevManager"],
        original_messages=[Message(role="user", content="继续")],
        base_request=InferenceRequest(model_id="model", tool_ids=[], stream=True),
        cancel_event=threading.Event(),
        sse_callback=None,
        sse_heartbeat=None,
        context_manager=None,
        session_id="",
        agent_manager=_Manager(),
        model_id="model",
        tool_ids=[],
        max_rounds=1,
    ))

    assert seen == [(None, None)]
