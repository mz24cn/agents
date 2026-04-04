"""Unit and property-based tests for MCPClientManager.

Tests the singleton pattern, tool conversion, connection state management,
and JSON-RPC message building. Does not require real MCP servers.
"""

import json
import pytest
from hypothesis import given, settings, strategies as st

from runtime.mcp_client import MCPClientManager
from runtime.models import ToolConfig


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the MCPClientManager singleton before each test."""
    MCPClientManager._reset()
    yield
    MCPClientManager._reset()


# ------------------------------------------------------------------
# Singleton tests
# ------------------------------------------------------------------

class TestSingleton:
    def test_same_instance(self):
        a = MCPClientManager()
        b = MCPClientManager()
        assert a is b
        assert id(a) == id(b)

    def test_reset_creates_new_instance(self):
        a = MCPClientManager()
        MCPClientManager._reset()
        b = MCPClientManager()
        assert a is not b

    def test_shared_state(self):
        a = MCPClientManager()
        b = MCPClientManager()
        # They share the same _connections dict
        assert a._connections is b._connections


# ------------------------------------------------------------------
# JSON-RPC building tests
# ------------------------------------------------------------------

class TestJsonRpc:
    def test_build_jsonrpc_basic(self):
        mgr = MCPClientManager()
        msg = mgr._build_jsonrpc("tools/list")
        assert msg["jsonrpc"] == "2.0"
        assert msg["method"] == "tools/list"
        assert "id" in msg
        assert "params" not in msg

    def test_build_jsonrpc_with_params(self):
        mgr = MCPClientManager()
        msg = mgr._build_jsonrpc("tools/call", {"name": "echo", "arguments": {"text": "hi"}})
        assert msg["params"]["name"] == "echo"
        assert msg["params"]["arguments"]["text"] == "hi"

    def test_incrementing_ids(self):
        mgr = MCPClientManager()
        msg1 = mgr._build_jsonrpc("a")
        msg2 = mgr._build_jsonrpc("b")
        assert msg2["id"] > msg1["id"]


# ------------------------------------------------------------------
# Tool conversion tests
# ------------------------------------------------------------------

class TestConvertTools:
    def test_empty_list(self):
        mgr = MCPClientManager()
        result = mgr._convert_tools("srv", [])
        assert result == []

    def test_single_tool(self):
        mgr = MCPClientManager()
        raw = [{
            "name": "echo",
            "description": "Echo back input",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }]
        tools = mgr._convert_tools("my-server", raw)
        assert len(tools) == 1
        t = tools[0]
        assert isinstance(t, ToolConfig)
        assert t.tool_type == "mcp"
        assert t.tool_id == "mcp-my-server-echo"
        assert t.name == "echo"
        assert t.mcp_server_name == "my-server"
        assert t.tool_name == "echo"
        assert t.description == "Echo back input"
        assert t.parameters["type"] == "object"
        assert "text" in t.parameters["properties"]

    def test_multiple_tools(self):
        mgr = MCPClientManager()
        raw = [
            {"name": "tool_a", "description": "A", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "tool_b", "description": "B", "inputSchema": {"type": "object", "properties": {}}},
        ]
        tools = mgr._convert_tools("s", raw)
        assert len(tools) == 2
        assert tools[0].tool_id == "mcp-s-tool_a"
        assert tools[1].tool_id == "mcp-s-tool_b"

    def test_missing_input_schema_defaults(self):
        mgr = MCPClientManager()
        raw = [{"name": "bare"}]
        tools = mgr._convert_tools("s", raw)
        assert len(tools) == 1
        assert tools[0].parameters == {"type": "object", "properties": {}}
        assert tools[0].description == ""


# ------------------------------------------------------------------
# Connection state tests (no real server needed)
# ------------------------------------------------------------------

class TestConnectionState:
    def test_is_connected_unknown_server(self):
        mgr = MCPClientManager()
        assert mgr.is_connected("nonexistent") is False

    def test_disconnect_unknown_server_no_error(self):
        mgr = MCPClientManager()
        mgr.disconnect("nonexistent")  # Should not raise

    def test_disconnect_all_empty(self):
        mgr = MCPClientManager()
        mgr.disconnect_all()  # Should not raise

    def test_reconnect_unknown_server_raises(self):
        mgr = MCPClientManager()
        with pytest.raises(RuntimeError, match="No connection info"):
            mgr.reconnect("nonexistent")

    def test_get_tools_not_connected_raises(self):
        mgr = MCPClientManager()
        with pytest.raises(RuntimeError, match="not registered"):
            mgr.get_tools("nonexistent")

    def test_call_tool_not_connected_raises(self):
        mgr = MCPClientManager()
        with pytest.raises(RuntimeError, match="not registered"):
            mgr.call_tool("nonexistent", "echo", {"text": "hi"})

    def test_manual_connection_state(self):
        """Simulate a URL connection dict to test state management."""
        mgr = MCPClientManager()
        mgr._connections["test"] = {
            "type": "url",
            "server_name": "test",
            "url": "http://localhost:9999",
            "headers": None,
            "connected": True,
            "server_info": {},
            "tools_cache": None,
        }
        assert mgr.is_connected("test") is True

        mgr.disconnect("test")
        assert mgr.is_connected("test") is False

    def test_get_tools_with_cache(self):
        """When tools_cache is set, get_tools returns cached tools."""
        mgr = MCPClientManager()
        cached = [ToolConfig(
            tool_id="mcp-test-echo",
            tool_type="mcp",
            name="echo",
            description="Echo",
            parameters={"type": "object", "properties": {}},
            mcp_server_name="test",
            tool_name="echo",
        )]
        mgr._connections["test"] = {
            "type": "url",
            "server_name": "test",
            "url": "http://localhost:9999",
            "headers": None,
            "connected": True,
            "server_info": {},
            "tools_cache": cached,
        }
        result = mgr.get_tools("test")
        assert len(result) == 1
        assert result[0].name == "echo"
        # Should be a copy, not the same list
        assert result is not cached

    def test_disconnect_clears_cache(self):
        """Disconnecting should clear the tools cache."""
        mgr = MCPClientManager()
        mgr._connections["test"] = {
            "type": "url",
            "server_name": "test",
            "url": "http://localhost:9999",
            "headers": None,
            "connected": True,
            "server_info": {},
            "tools_cache": [ToolConfig(
                tool_id="x", tool_type="mcp", name="x",
                description="x", parameters={},
            )],
        }
        mgr.disconnect("test")
        assert mgr._connections["test"]["tools_cache"] is None


# ------------------------------------------------------------------
# SSE parsing tests
# ------------------------------------------------------------------

class TestSSEParsing:
    def test_parse_sse_response_basic(self):
        mgr = MCPClientManager()
        sse_data = b'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n\n'
        result = mgr._parse_sse_response(sse_data)
        assert result["jsonrpc"] == "2.0"
        assert result["result"]["tools"] == []

    def test_parse_sse_response_skips_done(self):
        mgr = MCPClientManager()
        sse_data = b'data: [DONE]\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n'
        result = mgr._parse_sse_response(sse_data)
        assert result["jsonrpc"] == "2.0"

    def test_parse_sse_response_no_valid_data_raises(self):
        mgr = MCPClientManager()
        sse_data = b'event: ping\ndata: [DONE]\n\n'
        with pytest.raises(RuntimeError, match="No valid JSON-RPC"):
            mgr._parse_sse_response(sse_data)


# ------------------------------------------------------------------
# Property-based tests
# ------------------------------------------------------------------

# Feature: composable-agent-runtime, Property 10: MCP Client 单例
class TestSingletonProperty:
    """Property-based test verifying MCPClientManager singleton pattern.

    **Validates: Requirements 3.1**
    """

    @settings(max_examples=100)
    @given(n=st.integers(min_value=2, max_value=20))
    def test_multiple_instantiations_return_same_object(self, n):
        """For any number of instantiations, all MCPClientManager instances
        must be the exact same object (same id())."""
        # Feature: composable-agent-runtime, Property 10: MCP Client 单例
        # Reset singleton before each Hypothesis example to ensure clean state
        MCPClientManager._reset()
        instances = [MCPClientManager() for _ in range(n)]
        first_id = id(instances[0])
        for inst in instances:
            assert id(inst) == first_id
            assert inst is instances[0]


# ------------------------------------------------------------------
# Strategy helpers for Property 11
# ------------------------------------------------------------------

# Generate valid JSON Schema property definitions
_json_schema_types = st.sampled_from(["string", "number", "integer", "boolean"])

_schema_property = st.fixed_dictionaries({
    "type": _json_schema_types,
}).flatmap(lambda d: st.fixed_dictionaries({
    "type": st.just(d["type"]),
    "description": st.text(min_size=0, max_size=50),
}))

# Generate a dict of properties: {prop_name: {type, description}}
_schema_properties = st.dictionaries(
    keys=st.from_regex(r"[a-z][a-z0-9_]{0,19}", fullmatch=True),
    values=_schema_property,
    min_size=0,
    max_size=5,
)

# Generate a valid inputSchema
_input_schema = _schema_properties.flatmap(lambda props: st.fixed_dictionaries({
    "type": st.just("object"),
    "properties": st.just(props),
}))

# Generate a single raw MCP tool definition
_raw_tool = st.fixed_dictionaries({
    "name": st.from_regex(r"[a-z][a-z0-9_]{0,29}", fullmatch=True),
    "description": st.text(min_size=0, max_size=100),
    "inputSchema": _input_schema,
})

# Generate a list of raw MCP tools (1..10)
_raw_tools_list = st.lists(_raw_tool, min_size=1, max_size=10)

# Generate a server name
_server_name = st.from_regex(r"[a-z][a-z0-9\-]{0,19}", fullmatch=True)


# Feature: composable-agent-runtime, Property 11: MCP 工具发现与 Schema 生成
class TestMCPToolDiscoveryProperty:
    """Property-based test verifying MCP tool discovery and Schema generation.

    **Validates: Requirements 3.4, 3.5**
    """

    @settings(max_examples=100)
    @given(server_name=_server_name, raw_tools=_raw_tools_list)
    def test_convert_tools_produces_valid_tool_configs(self, server_name, raw_tools):
        """For any MCP server tool list, _convert_tools should produce
        ToolConfig objects with correct tool_type, mcp_server_name,
        matching tool_name, and valid parameters Schema."""
        # Feature: composable-agent-runtime, Property 11: MCP 工具发现与 Schema 生成
        MCPClientManager._reset()
        mgr = MCPClientManager()

        result = mgr._convert_tools(server_name, raw_tools)

        # Same number of tools out as in
        assert len(result) == len(raw_tools)

        for tool_config, raw in zip(result, raw_tools):
            # Must be a ToolConfig instance
            assert isinstance(tool_config, ToolConfig)

            # tool_type must be "mcp"
            assert tool_config.tool_type == "mcp"

            # mcp_server_name must match the input server_name
            assert tool_config.mcp_server_name == server_name

            # tool_name must match the raw tool name
            assert tool_config.tool_name == raw["name"]

            # name must match the raw tool name
            assert tool_config.name == raw["name"]

            # description must match
            assert tool_config.description == raw["description"]

            # tool_id must follow the mcp-{server}-{name} pattern
            assert tool_config.tool_id == f"mcp-{server_name}-{raw['name']}"

            # parameters must be a valid Schema with "type": "object" and "properties"
            params = tool_config.parameters
            assert isinstance(params, dict)
            assert params.get("type") == "object"
            assert "properties" in params
            assert isinstance(params["properties"], dict)


# ------------------------------------------------------------------
# load_config tests
# ------------------------------------------------------------------

from runtime.registry import ToolRegistry as _ToolRegistry


class TestLoadConfig:
    """Tests for MCPClientManager.load_config() convenience method."""

    def test_load_config_skips_disabled_servers(self):
        mgr = MCPClientManager()
        config = {
            "mcpServers": {
                "disabled_server": {
                    "command": "echo",
                    "args": ["hello"],
                    "disabled": True,
                },
            }
        }
        tools = mgr.load_config(config)
        assert tools == []
        assert mgr.is_connected("disabled_server") is False

    def test_load_config_skips_entries_without_command_or_url(self):
        mgr = MCPClientManager()
        config = {
            "mcpServers": {
                "bad_entry": {"some_key": "some_value"},
            }
        }
        tools = mgr.load_config(config)
        assert tools == []

    def test_load_config_accepts_inner_dict(self):
        """load_config should also accept the inner mcpServers dict directly."""
        mgr = MCPClientManager()
        config = {
            "disabled_server": {
                "command": "echo",
                "disabled": True,
            },
        }
        tools = mgr.load_config(config)
        assert tools == []

    def test_load_config_registers_tools_to_registry(self):
        """load_config is now lazy — it only stores connection configs without
        starting processes or discovering tools.  Already-connected servers
        are skipped (no-op)."""
        mgr = MCPClientManager()

        # Pre-populate a fake connection with cached tools
        cached_tools = [ToolConfig(
            tool_id="mcp-fake-echo",
            tool_type="mcp",
            name="echo",
            description="Echo tool",
            parameters={"type": "object", "properties": {}},
            mcp_server_name="fake",
            tool_name="echo",
        )]
        mgr._connections["fake"] = {
            "type": "url",
            "server_name": "fake",
            "url": "http://localhost:9999",
            "headers": None,
            "connected": True,
            "server_info": {},
            "tools_cache": cached_tools,
        }

        registry = _ToolRegistry()

        # Already connected — load_config skips it, returns []
        config = {
            "mcpServers": {
                "fake": {"url": "http://localhost:9999"},
            }
        }
        tools = mgr.load_config(config, tool_registry=registry)

        # Lazy mode: no tools returned, no registry side-effects
        assert tools == []

    def test_load_config_without_registry_returns_tools(self):
        """load_config is lazy — stores config only, returns empty list."""
        mgr = MCPClientManager()

        cached_tools = [ToolConfig(
            tool_id="mcp-srv-t1",
            tool_type="mcp",
            name="t1",
            description="Tool 1",
            parameters={"type": "object", "properties": {}},
            mcp_server_name="srv",
            tool_name="t1",
        )]
        mgr._connections["srv"] = {
            "type": "url",
            "server_name": "srv",
            "url": "http://localhost:9999",
            "headers": None,
            "connected": True,
            "server_info": {},
            "tools_cache": cached_tools,
        }

        tools = mgr.load_config({"mcpServers": {"srv": {"url": "http://localhost:9999"}}})
        # Already connected — skipped, returns []
        assert tools == []

    def test_load_config_invalid_config_raises(self):
        mgr = MCPClientManager()
        with pytest.raises(ValueError, match="dict"):
            mgr.load_config({"mcpServers": "not_a_dict"})
