"""Tests for inference-time tool augmentation."""

from unittest.mock import patch

from runtime.handler_infer import _add_exec_cli_for_open_terminal
from runtime.models import ToolConfig
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime


def test_adds_exec_cli_for_non_group_session_with_open_terminal():
    tool_ids = ["read_file"]

    with patch(
        "runtime.handler_infer.get_terminal_for_session",
        return_value={"session_id": "session-1"},
    ) as get_terminal:
        result = _add_exec_cli_for_open_terminal(tool_ids, "session-1", False)

    assert result == ["read_file", "exec_cli"]
    assert tool_ids == ["read_file"]
    get_terminal.assert_called_once_with("session-1")


def test_keeps_tool_ids_unchanged_when_exec_cli_is_already_selected():
    tool_ids = ["read_file", "exec_cli"]

    with patch("runtime.handler_infer.get_terminal_for_session") as get_terminal:
        result = _add_exec_cli_for_open_terminal(tool_ids, "session-1", False)

    assert result is tool_ids
    get_terminal.assert_not_called()


def test_does_not_add_exec_cli_without_an_open_terminal():
    tool_ids = ["read_file"]

    with patch("runtime.handler_infer.get_terminal_for_session", return_value=None):
        result = _add_exec_cli_for_open_terminal(tool_ids, "session-1", False)

    assert result is tool_ids


def test_does_not_add_exec_cli_for_group_chat():
    tool_ids = ["read_file"]

    with patch("runtime.handler_infer.get_terminal_for_session") as get_terminal:
        result = _add_exec_cli_for_open_terminal(tool_ids, "session-1", True)

    assert result is tool_ids
    get_terminal.assert_not_called()


def test_explicit_tool_scope_rejects_tool_removed_from_current_request():
    registry = ToolRegistry()
    removed = ToolConfig(
        tool_id="removed-tool",
        tool_type="function",
        name="removed_tool",
        description="old tool",
        parameters={"type": "object", "properties": {}},
    )
    registry.register(removed, callable_fn=lambda: "should not run")
    runtime = Runtime(ModelRegistry(), registry)

    result, config = runtime._execute_tool_call(
        "removed_tool", {}, tool_scope=[]
    )

    assert config is None
    assert "specified tool 'removed_tool' is temporarily unavailable" in result
    assert "not found in the current tool list" in result
