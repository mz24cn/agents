from runtime.builtin_tools import _thread_local
from runtime.builtin_tools_agent import TALK_TO_TOOL_CONFIG, _make_talk_to_fn
from runtime.common import clear_request_context, get_request_context, set_request_context
from runtime.models import Message
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime


class FakeAgentManager:
    def __init__(self, agents):
        self.agents = {agent["agent_id"]: agent for agent in agents}

    def get(self, key):
        for agent in self.agents.values():
            if agent.get("nickname") == key:
                return agent
        return self.agents.get(key)


class RecordingRuntime:
    def __init__(self):
        self.calls = []

    def infer_stream(self, request, cancel_event=None):
        self.calls.append({
            "request": request,
            "agent_id": get_request_context("agent_id"),
        })
        yield Message(role="assistant", content="done")


def _agent(agent_id, nickname):
    return {
        "agent_id": agent_id,
        "nickname": nickname,
        "model_id": "m",
        "tool_ids": ["talk_to"],
        "system_prompt": "Agents:\n{{AGENTS}}",
    }


def test_talk_to_rejects_calling_agent_by_id_or_nickname():
    runtime = RecordingRuntime()
    manager = FakeAgentManager([
        _agent("dev-manager", "DevManager"),
        _agent("coder", "Coder"),
    ])
    set_request_context(
        agent_manager=manager,
        agent_id="dev-manager",
        all_agent_ids=["dev-manager", "coder"],
    )
    talk_to = _make_talk_to_fn(runtime, _thread_local)
    try:
        result = talk_to(["DevManager", "dev-manager"], "help")
    finally:
        clear_request_context(list(_thread_local.__dict__.keys()))

    assert runtime.calls == []
    assert result.count("talk_to cannot target the calling agent itself") == 2


def test_runtime_execution_layer_rejects_talk_to_self_by_id_or_nickname():
    manager = FakeAgentManager([
        _agent("dev-manager", "DevManager"),
        _agent("coder", "Coder"),
    ])
    calls = []
    registry = ToolRegistry()
    registry.register(
        TALK_TO_TOOL_CONFIG,
        callable_fn=lambda agents, message: calls.append((agents, message)) or "called",
    )
    runtime = Runtime(ModelRegistry(), registry)
    set_request_context(agent_manager=manager, agent_id="dev-manager")
    try:
        by_id, _ = runtime._execute_tool_call(
            "talk_to", {"agents": ["dev-manager"], "message": "help"}
        )
        by_nickname, _ = runtime._execute_tool_call(
            "talk_to", {"agents": ["DevManager"], "message": "help"}
        )
    finally:
        clear_request_context(list(_thread_local.__dict__.keys()))

    assert calls == []
    assert by_id == "Error: talk_to cannot target the calling agent itself."
    assert by_nickname == "Error: talk_to cannot target the calling agent itself."


def test_runtime_execution_layer_allows_talk_to_other_agent():
    manager = FakeAgentManager([
        _agent("dev-manager", "DevManager"),
        _agent("coder", "Coder"),
    ])
    calls = []
    registry = ToolRegistry()
    registry.register(
        TALK_TO_TOOL_CONFIG,
        callable_fn=lambda agents, message: calls.append((agents, message)) or "called",
    )
    runtime = Runtime(ModelRegistry(), registry)
    set_request_context(agent_manager=manager, agent_id="dev-manager")
    try:
        result, _ = runtime._execute_tool_call(
            "talk_to", {"agents": ["coder"], "message": "help"}
        )
    finally:
        clear_request_context(list(_thread_local.__dict__.keys()))

    assert result == "called"
    assert calls == [(["coder"], "help")]


def test_runtime_execution_layer_rejects_agent_removed_from_current_roster():
    manager = FakeAgentManager([
        _agent("dev-manager", "DevManager"),
        _agent("coder", "Coder"),
        _agent("removed", "Removed"),
    ])
    calls = []
    registry = ToolRegistry()
    registry.register(
        TALK_TO_TOOL_CONFIG,
        callable_fn=lambda agents, message: calls.append((agents, message)) or "called",
    )
    runtime = Runtime(ModelRegistry(), registry)
    set_request_context(
        agent_manager=manager,
        agent_id="dev-manager",
        all_agent_ids=["dev-manager", "coder"],
    )
    try:
        result, _ = runtime._execute_tool_call(
            "talk_to",
            {"agents": ["Removed"], "message": "help"},
            tool_scope=[TALK_TO_TOOL_CONFIG],
        )
    finally:
        clear_request_context(list(_thread_local.__dict__.keys()))

    assert calls == []
    assert "does not exist in the current conversation or has left" in result


def test_talk_to_child_context_uses_target_identity_and_excludes_target_from_roster():
    runtime = RecordingRuntime()
    manager = FakeAgentManager([
        _agent("dev-manager", "DevManager"),
        _agent("coder", "Coder"),
    ])
    set_request_context(
        agent_manager=manager,
        agent_id="dev-manager",
        all_agent_ids=["dev-manager", "coder"],
    )
    talk_to = _make_talk_to_fn(runtime, _thread_local)
    try:
        result = talk_to(["coder"], "help")
    finally:
        clear_request_context(list(_thread_local.__dict__.keys()))

    assert "done" in result
    assert len(runtime.calls) == 1
    assert runtime.calls[0]["agent_id"] == "coder"
    system_content = runtime.calls[0]["request"].messages[0].content
    assert "| DevManager | dev-manager |" in system_content
    assert "| Coder | coder |" not in system_content
