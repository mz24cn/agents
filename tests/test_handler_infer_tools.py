"""Tests for inference-time tool augmentation."""

from unittest.mock import MagicMock, patch

from runtime.handler_infer import (
    _add_exec_cli_for_open_terminal,
    _remote_session_has_terminal,
)
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


class _FakeRemoteProxy:
    """Minimal stand-in exposing only ``http_json`` (as used by the probe)."""

    def __init__(self, result=None, raises=None):
        self._result = result
        self._raises = raises
        self.calls = []

    def http_json(self, path, method="GET", **kwargs):
        self.calls.append((path, method, kwargs))
        if self._raises is not None:
            raise self._raises
        return self._result


def test_remote_terminal_detected_by_auto_terminal_id():
    proxy = _FakeRemoteProxy(result=(200, {"terminals": [
        {"terminal_id": "session-1:auto", "session_id": "session-1"},
    ]}))
    assert _remote_session_has_terminal(proxy, "session-1", False) is True
    assert proxy.calls and proxy.calls[0][0] == "/v1/terminals"


def test_remote_terminal_detected_by_session_id_only():
    # The child strips ``:``-suffixes from the stored session id, so a bare
    # session_id match must also count.
    proxy = _FakeRemoteProxy(result=(200, {"terminals": [
        {"terminal_id": "abc:auto", "session_id": "abc"},
    ]}))
    assert _remote_session_has_terminal(proxy, "abc", False) is True


def test_remote_terminal_absent_for_other_session():
    proxy = _FakeRemoteProxy(result=(200, {"terminals": [
        {"terminal_id": "other:auto", "session_id": "other"},
    ]}))
    assert _remote_session_has_terminal(proxy, "session-1", False) is False


def test_remote_terminal_probe_is_best_effort():
    # Any failure (child unreachable / non-200 / malformed) degrades to False
    # instead of breaking inference.
    assert _remote_session_has_terminal(
        _FakeRemoteProxy(raises=RuntimeError("boom")), "s", False) is False
    assert _remote_session_has_terminal(
        _FakeRemoteProxy(result=(500, {"error": "nope"})), "s", False) is False
    assert _remote_session_has_terminal(
        _FakeRemoteProxy(result=(200, {})), "s", False) is False
    assert _remote_session_has_terminal(
        _FakeRemoteProxy(result=(200, {"terminals": "oops"})), "s", False) is False


def test_remote_terminal_skipped_for_group_chat_and_blank_session():
    proxy = _FakeRemoteProxy(result=(200, {"terminals": []}))
    assert _remote_session_has_terminal(proxy, "session-1", True) is False
    assert _remote_session_has_terminal(proxy, None, False) is False
    assert proxy.calls == []  # never probes the child


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
