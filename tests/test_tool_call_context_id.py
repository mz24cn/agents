"""Regression tests for canonical tool-call IDs in self-streaming tools."""

from runtime.common import _thread_local
from runtime.models import ToolConfig
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime


def test_execute_tool_call_exposes_and_restores_canonical_tool_use_id():
    registry = ToolRegistry()
    config = ToolConfig(
        tool_id="capture",
        tool_type="function",
        name="capture",
        description="capture current tool call id",
        parameters={"type": "object", "properties": {}},
    )
    seen = []

    def capture():
        seen.append(getattr(_thread_local, "tool_use_id", None))
        return "ok"

    registry.register(config, callable_fn=capture)
    runtime = Runtime(ModelRegistry(), registry)

    _thread_local.tool_use_id = "outer_call"
    try:
        result, resolved = runtime._execute_tool_call(
            "capture", {}, tool_scope=[config], tool_use_id="call_model_123"
        )
        assert result == "ok"
        assert resolved is config
        assert seen == ["call_model_123"]
        assert _thread_local.tool_use_id == "outer_call"
    finally:
        try:
            delattr(_thread_local, "tool_use_id")
        except AttributeError:
            pass


def test_execute_tool_call_clears_temporary_tool_use_id():
    registry = ToolRegistry()
    config = ToolConfig(
        tool_id="capture",
        tool_type="function",
        name="capture",
        description="capture current tool call id",
        parameters={"type": "object", "properties": {}},
    )
    registry.register(
        config,
        callable_fn=lambda: str(getattr(_thread_local, "tool_use_id", None)),
    )
    runtime = Runtime(ModelRegistry(), registry)

    try:
        delattr(_thread_local, "tool_use_id")
    except AttributeError:
        pass

    result, _ = runtime._execute_tool_call(
        "capture", {}, tool_scope=[config], tool_use_id="call_model_456"
    )
    assert result == "call_model_456"
    assert not hasattr(_thread_local, "tool_use_id")
