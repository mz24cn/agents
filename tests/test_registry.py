"""Unit tests for ModelRegistry."""

import json
import os
import tempfile

import pytest

from runtime.models import ModelConfig
from runtime.registry import ModelRegistry


def _make_config(model_id: str = "test-model", **kwargs) -> ModelConfig:
    """Helper to create a ModelConfig with sensible defaults."""
    defaults = {
        "model_id": model_id,
        "api_base": "http://localhost:11434",
        "model_name": "qwen3.5:9b",
        "api_key": "",
        "model_type": "llm",
        "api_protocol": "openai",
        "generate_params": {"temperature": 0.7},
    }
    defaults.update(kwargs)
    return ModelConfig(**defaults)


class TestModelRegistryRegisterAndGet:
    def test_register_and_get(self):
        reg = ModelRegistry()
        cfg = _make_config("m1")
        reg.register(cfg)
        assert reg.get("m1") is cfg

    def test_get_nonexistent_returns_none(self):
        reg = ModelRegistry()
        assert reg.get("no-such-model") is None

    def test_register_overwrites_existing(self):
        reg = ModelRegistry()
        cfg1 = _make_config("m1", model_name="old")
        cfg2 = _make_config("m1", model_name="new")
        reg.register(cfg1)
        reg.register(cfg2)
        assert reg.get("m1").model_name == "new"


class TestModelRegistryRemove:
    def test_remove_existing(self):
        reg = ModelRegistry()
        reg.register(_make_config("m1"))
        assert reg.remove("m1") is True
        assert reg.get("m1") is None

    def test_remove_nonexistent_returns_false(self):
        reg = ModelRegistry()
        assert reg.remove("no-such") is False


class TestModelRegistryListAll:
    def test_list_all_empty(self):
        reg = ModelRegistry()
        assert reg.list_all() == []

    def test_list_all_returns_all(self):
        reg = ModelRegistry()
        reg.register(_make_config("m1"))
        reg.register(_make_config("m2"))
        ids = {c.model_id for c in reg.list_all()}
        assert ids == {"m1", "m2"}


class TestModelRegistryPersistence:
    def test_save_and_load_round_trip(self):
        reg = ModelRegistry()
        reg.register(_make_config("m1", model_name="alpha"))
        reg.register(_make_config("m2", model_name="beta", api_protocol="ollama"))

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            reg.save(path)

            reg2 = ModelRegistry()
            reg2.load(path)

            assert len(reg2.list_all()) == 2
            m1 = reg2.get("m1")
            assert m1 is not None
            assert m1.model_name == "alpha"
            m2 = reg2.get("m2")
            assert m2 is not None
            assert m2.api_protocol == "ollama"
        finally:
            os.unlink(path)

    def test_load_replaces_existing(self):
        reg = ModelRegistry()
        reg.register(_make_config("old-model"))

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump([_make_config("new-model").to_dict()], f)
            path = f.name
        try:
            reg.load(path)
            assert reg.get("old-model") is None
            assert reg.get("new-model") is not None
        finally:
            os.unlink(path)

    def test_load_nonexistent_file_raises(self):
        reg = ModelRegistry()
        with pytest.raises(FileNotFoundError):
            reg.load("/tmp/nonexistent_file_12345.json")

    def test_load_invalid_json_raises(self):
        reg = ModelRegistry()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({"not": "a list"}, f)
            path = f.name
        try:
            with pytest.raises(ValueError, match="Expected a JSON array"):
                reg.load(path)
        finally:
            os.unlink(path)

    def test_save_creates_parent_directories(self):
        reg = ModelRegistry()
        reg.register(_make_config("m1"))
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sub", "dir", "models.json")
            reg.save(path)
            assert os.path.exists(path)


from runtime.models import ToolConfig
from runtime.registry import ToolRegistry


def _make_tool_config(tool_id: str = "test-tool", tool_type: str = "function", **kwargs) -> ToolConfig:
    """Helper to create a ToolConfig with sensible defaults."""
    defaults = {
        "tool_id": tool_id,
        "tool_type": tool_type,
        "name": tool_id,
        "description": f"A {tool_type} tool",
        "parameters": {
            "type": "object",
            "properties": {"arg1": {"type": "string"}},
            "required": ["arg1"],
        },
    }
    defaults.update(kwargs)
    return ToolConfig(**defaults)


def _make_mcp_tool(tool_id: str = "mcp-tool") -> ToolConfig:
    return _make_tool_config(
        tool_id=tool_id,
        tool_type="mcp",
        mcp_server_name="memory",
        tool_name="save",
    )


def _make_skill_tool(tool_id: str = "skill-tool") -> ToolConfig:
    return _make_tool_config(
        tool_id=tool_id,
        tool_type="skill",
        steps=[{"type": "tool", "target": "some_tool"}],
    )


class TestToolRegistryRegisterAndGet:
    def test_register_and_get(self):
        reg = ToolRegistry()
        cfg = _make_tool_config("t1")
        reg.register(cfg)
        assert reg.get("t1") is cfg

    def test_get_nonexistent_returns_none(self):
        reg = ToolRegistry()
        assert reg.get("no-such-tool") is None

    def test_register_overwrites_existing(self):
        reg = ToolRegistry()
        cfg1 = _make_tool_config("t1", description="old")
        cfg2 = _make_tool_config("t1", description="new")
        reg.register(cfg1)
        reg.register(cfg2)
        assert reg.get("t1").description == "new"

    def test_register_with_callable(self):
        reg = ToolRegistry()
        cfg = _make_tool_config("t1")

        def my_fn(arg1: str) -> str:
            return arg1

        reg.register(cfg, callable_fn=my_fn)
        assert reg.get("t1") is cfg
        assert reg.get_callable("t1") is my_fn

    def test_register_without_callable_clears_old_callable(self):
        reg = ToolRegistry()
        cfg = _make_tool_config("t1")

        def my_fn(arg1: str) -> str:
            return arg1

        reg.register(cfg, callable_fn=my_fn)
        assert reg.get_callable("t1") is my_fn

        cfg2 = _make_tool_config("t1", description="updated")
        reg.register(cfg2)
        assert reg.get_callable("t1") is None


class TestToolRegistryGetCallable:
    def test_get_callable_nonexistent_returns_none(self):
        reg = ToolRegistry()
        assert reg.get_callable("no-such") is None

    def test_get_callable_no_callable_registered(self):
        reg = ToolRegistry()
        reg.register(_make_tool_config("t1"))
        assert reg.get_callable("t1") is None


class TestToolRegistryRemove:
    def test_remove_existing(self):
        reg = ToolRegistry()
        reg.register(_make_tool_config("t1"))
        assert reg.remove("t1") is True
        assert reg.get("t1") is None

    def test_remove_nonexistent_returns_false(self):
        reg = ToolRegistry()
        assert reg.remove("no-such") is False

    def test_remove_also_removes_callable(self):
        reg = ToolRegistry()
        fn = lambda arg1: arg1
        reg.register(_make_tool_config("t1"), callable_fn=fn)
        reg.remove("t1")
        assert reg.get_callable("t1") is None


class TestToolRegistryListAll:
    def test_list_all_empty(self):
        reg = ToolRegistry()
        assert reg.list_all() == []

    def test_list_all_returns_all(self):
        reg = ToolRegistry()
        reg.register(_make_tool_config("t1"))
        reg.register(_make_mcp_tool("t2"))
        reg.register(_make_skill_tool("t3"))
        ids = {c.tool_id for c in reg.list_all()}
        assert ids == {"t1", "t2", "t3"}


class TestToolRegistryListByType:
    def test_list_by_type_function(self):
        reg = ToolRegistry()
        reg.register(_make_tool_config("t1", tool_type="function"))
        reg.register(_make_mcp_tool("t2"))
        reg.register(_make_skill_tool("t3"))
        result = reg.list_by_type("function")
        assert len(result) == 1
        assert result[0].tool_id == "t1"

    def test_list_by_type_mcp(self):
        reg = ToolRegistry()
        reg.register(_make_tool_config("t1"))
        reg.register(_make_mcp_tool("t2"))
        result = reg.list_by_type("mcp")
        assert len(result) == 1
        assert result[0].tool_id == "t2"

    def test_list_by_type_no_match(self):
        reg = ToolRegistry()
        reg.register(_make_tool_config("t1"))
        assert reg.list_by_type("skill") == []


class TestToolRegistryPersistence:
    def test_save_and_load_round_trip(self):
        reg = ToolRegistry()
        reg.register(_make_tool_config("t1", description="func tool"))
        reg.register(_make_mcp_tool("t2"))
        reg.register(_make_skill_tool("t3"))

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            reg.save(path)

            reg2 = ToolRegistry()
            reg2.load(path)

            assert len(reg2.list_all()) == 3
            t1 = reg2.get("t1")
            assert t1 is not None
            assert t1.description == "func tool"
            assert t1.tool_type == "function"

            t2 = reg2.get("t2")
            assert t2 is not None
            assert t2.tool_type == "mcp"
            assert t2.mcp_server_name == "memory"

            t3 = reg2.get("t3")
            assert t3 is not None
            assert t3.tool_type == "skill"
            assert t3.steps == [{"type": "tool", "target": "some_tool"}]
        finally:
            os.unlink(path)

    def test_load_replaces_existing(self):
        reg = ToolRegistry()
        reg.register(_make_tool_config("old-tool"))

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump([_make_tool_config("new-tool").to_dict()], f)
            path = f.name
        try:
            reg.load(path)
            assert reg.get("old-tool") is None
            assert reg.get("new-tool") is not None
        finally:
            os.unlink(path)

    def test_load_clears_callables(self):
        reg = ToolRegistry()
        fn = lambda arg1: arg1
        reg.register(_make_tool_config("t1"), callable_fn=fn)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump([_make_tool_config("t1").to_dict()], f)
            path = f.name
        try:
            reg.load(path)
            assert reg.get_callable("t1") is None
        finally:
            os.unlink(path)

    def test_load_nonexistent_file_raises(self):
        reg = ToolRegistry()
        with pytest.raises(FileNotFoundError):
            reg.load("/tmp/nonexistent_tool_file_12345.json")

    def test_load_invalid_json_raises(self):
        reg = ToolRegistry()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({"not": "a list"}, f)
            path = f.name
        try:
            with pytest.raises(ValueError, match="Expected a JSON array"):
                reg.load(path)
        finally:
            os.unlink(path)

    def test_save_creates_parent_directories(self):
        reg = ToolRegistry()
        reg.register(_make_tool_config("t1"))
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sub", "dir", "tools.json")
            reg.save(path)
            assert os.path.exists(path)
