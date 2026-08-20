"""File-journal coverage for group-chat and nested agent workers."""

import json

from runtime.builtin_tools_agent import _make_delegate_fn, _make_talk_to_fn
from runtime.builtin_tools_coding import _write_file
from runtime.common import _thread_local, clear_request_context, get_request_context, set_request_context
from runtime.context_manager import get_file_journals_list
from runtime.group_chat import run_group_chat_stream_gen
from runtime.models import InferenceRequest, Message
from runtime.models import ToolConfig


TURN_TIMESTAMP = "2026-08-19T12:34:56Z"


class AgentManager:
    def __init__(self, agents):
        self.agents = agents

    def get(self, agent_id):
        return self.agents.get(agent_id)


class ContextManager:
    def load_conversation(self, session_id):
        return []

    def get_summary(self, session_id):
        return "", {}

    def get_memory_entries(self, session_id):
        return []


class GroupWritingRuntime:
    def infer_stream(self, request, cancel_event=None):
        agent_id = get_request_context("agent_id")
        result = json.loads(_write_file(f"{agent_id}.txt", f"written by {agent_id}"))
        assert result["journal"]["session_id"] == "group-session"
        yield Message(role="tool", name="write_file", content=json.dumps(result))
        yield Message(role="assistant", content="done")
        yield Message(role="usage", name="round", content="{}")


class TalkWritingRuntime:
    def infer_stream(self, request, cancel_event=None):
        result = json.loads(_write_file("talk-child.txt", "written by talk_to"))
        assert result["journal"]["session_id"] == "group-session"
        yield Message(role="assistant", content="child done")


class DelegateWritingRuntime:
    def infer_stream(self, request, cancel_event=None):
        result = json.loads(_write_file("delegate-child.txt", "written by delegate"))
        assert result["journal"]["session_id"] == "group-session"
        yield Message(role="assistant", content="delegate done")


def _set_parent_context(workspace, session_dir):
    set_request_context(
        workspace=str(workspace),
        session_id="group-session",
        session_dir=str(session_dir),
        user_message_timestamp=TURN_TIMESTAMP,
        file_journal_manager=None,
        depth=0,
        tool_scope=[],
    )


def test_parallel_group_chat_workers_journal_on_parent_user_turn(tmp_path):
    workspace = tmp_path / "workspace"
    session_dir = tmp_path / "chat_data" / "group-session"
    workspace.mkdir(parents=True)
    session_dir.mkdir(parents=True)
    _set_parent_context(workspace, session_dir)

    agents = {
        "agent-a": {"agent_id": "agent-a", "nickname": "A", "model_id": "m", "tool_ids": ["write_file"]},
        "agent-b": {"agent_id": "agent-b", "nickname": "B", "model_id": "m", "tool_ids": ["write_file"]},
        "agent-c": {"agent_id": "agent-c", "nickname": "C", "model_id": "m", "tool_ids": ["write_file"]},
    }
    try:
        list(run_group_chat_stream_gen(
            runtime=GroupWritingRuntime(),
            mentioned_agent_ids=["agent-a", "agent-b"],
            all_agent_ids=list(agents),
            original_messages=[Message(role="user", content="@all write", timestamp=TURN_TIMESTAMP)],
            base_request=InferenceRequest(model_id="m", messages=[]),
            context_manager=ContextManager(),
            session_id="group-session",
            agent_manager=AgentManager(agents),
            model_id="m",
            tool_ids=["write_file"],
            max_rounds=1,
        ))
    finally:
        clear_request_context(list(_thread_local.__dict__.keys()))

    assert get_file_journals_list(str(session_dir)) == [TURN_TIMESTAMP]
    manifest = json.loads((session_dir / "file_journals" / "260819_123456" / "manifest.json").read_text())
    assert set(manifest["files"]) == {"agent-a.txt", "agent-b.txt"}
    assert all("after" in entry for entry in manifest["files"].values())


def test_talk_to_file_changes_journal_on_parent_user_turn(tmp_path):
    workspace = tmp_path / "workspace"
    session_dir = tmp_path / "chat_data" / "group-session"
    workspace.mkdir(parents=True)
    session_dir.mkdir(parents=True)
    _set_parent_context(workspace, session_dir)

    agent_manager = AgentManager({
        "child": {
            "agent_id": "child",
            "nickname": "Child",
            "model_id": "m",
            "tool_ids": ["write_file"],
            "system_prompt": "help",
        }
    })
    set_request_context(agent_manager=agent_manager, context_manager=None, all_agent_ids=["child"])
    talk_to = _make_talk_to_fn(TalkWritingRuntime(), _thread_local)
    try:
        result = talk_to(["child"], "write the file")
    finally:
        clear_request_context(list(_thread_local.__dict__.keys()))

    assert "child done" in result
    assert get_file_journals_list(str(session_dir)) == [TURN_TIMESTAMP]
    manifest = json.loads((session_dir / "file_journals" / "260819_123456" / "manifest.json").read_text())
    assert set(manifest["files"]) == {"talk-child.txt"}
    assert "after" in manifest["files"]["talk-child.txt"]


def test_delegate_file_changes_journal_on_parent_user_turn(tmp_path):
    workspace = tmp_path / "workspace"
    session_dir = tmp_path / "chat_data" / "group-session"
    workspace.mkdir(parents=True)
    session_dir.mkdir(parents=True)
    _set_parent_context(workspace, session_dir)
    set_request_context(tool_scope=[ToolConfig(
        tool_id="write_file", tool_type="function", name="write_file",
        description="", parameters={"type": "object"}, builtin=True,
    )])

    delegate = _make_delegate_fn(DelegateWritingRuntime(), _thread_local)
    try:
        result = delegate("m", ["write_file"], "write the file")
    finally:
        clear_request_context(list(_thread_local.__dict__.keys()))

    assert "delegate done" in result
    assert get_file_journals_list(str(session_dir)) == [TURN_TIMESTAMP]
    manifest = json.loads((session_dir / "file_journals" / "260819_123456" / "manifest.json").read_text())
    assert set(manifest["files"]) == {"delegate-child.txt"}
    assert "after" in manifest["files"]["delegate-child.txt"]
