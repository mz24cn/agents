import json
from pathlib import Path

from runtime.builtin_tools import _thread_local
from runtime.builtin_tools_agent import TALK_TO_TOOL_CONFIG, _make_talk_to_fn
from runtime.common import clear_request_context, get_request_context, set_request_context
from runtime.context_manager import ContextManager
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


def test_talk_to_child_template_overwrites_literal_agents_argument():
    """Nested talk_to must overwrite a truthy literal/stale AGENTS argument
    in the target agent's template, rather than preserving {{AGENTS}}."""
    runtime = RecordingRuntime()
    manager_agent = _agent("dev-manager", "DevManager")
    coder = _agent("coder", "Coder")
    coder.update({
        "system_prompt": "",
        "template_id": "coder-template",
        "template_arguments": {"AGENTS": "{{AGENTS}}"},
    })
    manager = FakeAgentManager([manager_agent, coder])
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
    sys_msg = runtime.calls[0]["request"].messages[0]
    assert sys_msg.prompt_template == "coder-template"
    assert sys_msg.arguments["AGENTS"] != "{{AGENTS}}"
    assert "{{AGENTS}}" not in sys_msg.arguments["AGENTS"]
    assert "| DevManager | dev-manager |" in sys_msg.arguments["AGENTS"]
    assert "| Coder | coder |" not in sys_msg.arguments["AGENTS"]


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


def test_talk_to_subsession_is_incrementally_persisted(tmp_path):
    """A completed child model round is visible before infer_stream finishes."""
    parent_session_id = "parent-session"
    cm = ContextManager(infer_fn=lambda req: None, chats_dir=str(tmp_path))

    class InspectingRuntime:
        def __init__(self):
            self.incremental_messages = None

        def infer_stream(self, request, cancel_event=None):
            yield Message(role="assistant", content="first round")
            yield Message(
                role="usage",
                content=json.dumps({
                    "prompt_tokens": 2,
                    "completion_tokens": 3,
                    "total_tokens": 5,
                }),
            )
            # The talk_to loop persists the usage-closed round before asking the
            # generator for its next item, so the file must already contain it.
            paths = list(Path(tmp_path).glob(
                "parent/session/talk_*/conversation.json"
            ))
            assert len(paths) == 1
            self.incremental_messages = json.loads(
                paths[0].read_text(encoding="utf-8")
            )["messages"]
            yield Message(role="assistant", content="second round")
            yield Message(
                role="usage",
                content=json.dumps({
                    "prompt_tokens": 4,
                    "completion_tokens": 5,
                    "total_tokens": 9,
                }),
            )

    runtime = InspectingRuntime()
    manager = FakeAgentManager([
        _agent("dev-manager", "DevManager"),
        {**_agent("coder", "Coder"), "tool_ids": []},
    ])
    set_request_context(
        session_id=parent_session_id,
        context_manager=cm,
        agent_manager=manager,
        agent_id="dev-manager",
        all_agent_ids=["dev-manager", "coder"],
    )
    talk_to = _make_talk_to_fn(runtime, _thread_local)
    try:
        result = talk_to(["coder"], "help")
    finally:
        clear_request_context(list(_thread_local.__dict__.keys()))

    assert "first roundsecond round" in result
    assert [message["role"] for message in runtime.incremental_messages] == [
        "system", "user", "assistant",
    ]
    assert runtime.incremental_messages[-1]["content"] == "first round"

    paths = list(Path(tmp_path).glob(
        "parent/session/talk_*/conversation.json"
    ))
    final_messages = json.loads(paths[0].read_text(encoding="utf-8"))["messages"]
    assert [message["content"] for message in final_messages if message["role"] == "assistant"] == [
        "first round", "second round",
    ]
