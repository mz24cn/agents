"""Remote-execution group chat: talk_to / delegate must stay available.

Regression test for the "talk_to 在本环境不可用 (not found in the current
tool list)" bug. In a remote session the per-agent tool scope used to be the
intersection of the *top-level* selected tools (base_request.tools) and the
agent's own tool_ids, so an agent that lists ``talk_to`` (but whose session
did not) silently lost the tool. The simplified design instead builds the
scope from the parent (local) registry by the agent's own tool_ids -- exactly
like local mode -- and keeps the orchestration tools (talk_to / delegate)
executing on the parent instead of forwarding them to the child.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.builtin_tools_agent import (  # noqa: E402
    TALK_TO_TOOL_CONFIG,
    DELEGATE_TOOL_CONFIG,
)
from runtime.common import (  # noqa: E402
    clear_request_context,
    set_request_context,
    _thread_local,
)
from runtime.group_chat import run_group_chat_stream_gen  # noqa: E402
from runtime.models import InferenceRequest, Message, ToolConfig  # noqa: E402
from runtime.registry import ToolRegistry  # noqa: E402
from runtime.remote_tool_proxy import RemoteToolProxy  # noqa: E402


def make_proxy():
    return RemoteToolProxy(
        {"id": "http://127.0.0.1:1", "url": "http://127.0.0.1:1/v1/setup"}
    )


def _write_file_config() -> ToolConfig:
    return ToolConfig(
        tool_id="write_file",
        tool_type="function",
        name="write_file",
        description="write a file",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )


class FakeAgentManager:
    def __init__(self, agents):
        self._by_id = {a["agent_id"]: a for a in agents}

    def get(self, key):
        for a in self._by_id.values():
            if a.get("nickname") == key:
                return a
        return self._by_id.get(key)


class CaptureRuntime:
    """Records the InferenceRequest handed to each agent."""

    def __init__(self, tool_registry):
        self._tool_registry = tool_registry
        self.captured = []

    def infer_stream(self, request, cancel_event=None):
        self.captured.append(request)
        yield Message(role="assistant", content="ok")


def test_wrap_parent_configs_exempts_orchestration_tools():
    proxy = make_proxy()
    wf = _write_file_config()
    wrapped = proxy.wrap_parent_configs(
        [TALK_TO_TOOL_CONFIG, DELEGATE_TOOL_CONFIG, wf]
    )
    by_id = {w.tool_id: w for w in wrapped}

    # orchestration tools keep the *parent* config (local execution)
    assert by_id["talk_to"] is TALK_TO_TOOL_CONFIG
    assert by_id["delegate"] is DELEGATE_TOOL_CONFIG
    assert getattr(by_id["talk_to"], "is_remote_proxy", False) is False
    assert getattr(by_id["delegate"], "is_remote_proxy", False) is False
    assert getattr(by_id["talk_to"], "callable_fn", None) is None

    # everything else is wrapped as a child-forwarding proxy
    assert by_id["write_file"] is not wf
    assert by_id["write_file"].is_remote_proxy is True
    assert getattr(by_id["write_file"], "callable_fn", None) is not None


def test_remote_group_chat_talk_to_stays_in_agent_scope():
    proxy = make_proxy()
    registry = ToolRegistry()
    registry.register(TALK_TO_TOOL_CONFIG, callable_fn=lambda **kw: "talked")
    registry.register(_write_file_config())

    agent_a = {
        "agent_id": "A", "nickname": "A", "model_id": "m1",
        "tool_ids": ["talk_to", "write_file"],
    }
    agent_b = {
        "agent_id": "B", "nickname": "B", "model_id": "m1",
        "tool_ids": ["talk_to", "write_file"],
    }
    manager = FakeAgentManager([agent_a, agent_b])

    runtime = CaptureRuntime(registry)

    # Simulate the remote session: the *top-level* selection only picked
    # write_file (so base_request.tools has no talk_to), but the agents list
    # talk_to. This is exactly the case that used to drop talk_to.
    base_tools = proxy.wrap_parent_configs([_write_file_config()])
    base_request = InferenceRequest(model_id="m1", tools=base_tools, messages=[])

    set_request_context(
        agent_id="A",
        agent_manager=manager,
        all_agent_ids=["A", "B"],
        model_id="m1",
        remote_tool_proxy=proxy,
    )
    try:
        list(run_group_chat_stream_gen(
            runtime=runtime,
            mentioned_agent_ids=["A"],
            all_agent_ids=["A", "B"],
            original_messages=[Message(role="user", content="hi")],
            base_request=base_request,
            cancel_event=None,
            context_manager=None,
            session_id=None,
            agent_manager=manager,
            model_id="m1",
            tool_ids=["write_file"],
            max_rounds=1,
        ))
    finally:
        clear_request_context(list(_thread_local.__dict__.keys()))

    assert runtime.captured, "no agent inference captured"
    tools = runtime.captured[0].tools
    assert tools is not None, "remote agent request must carry its tool scope"
    ids = {t.tool_id for t in tools}

    # talk_to survived even though it was absent from the top-level selection
    assert "talk_to" in ids
    assert "write_file" in ids

    talk_to_cfg = next(t for t in tools if t.tool_id == "talk_to")
    write_file_cfg = next(t for t in tools if t.tool_id == "write_file")
    # talk_to runs on the parent (not forwarded to the child)
    assert getattr(talk_to_cfg, "is_remote_proxy", False) is False
    assert getattr(talk_to_cfg, "callable_fn", None) is None
    # write_file is a child-forwarding proxy
    assert write_file_cfg.is_remote_proxy is True
    assert getattr(write_file_cfg, "callable_fn", None) is not None
