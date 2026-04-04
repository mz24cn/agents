"""Unit tests for runtime.tools — register_function_tool decorator."""

from runtime.registry import ToolRegistry
from runtime.tools import (
    _extract_tool_config,
    _parse_docstring,
    _python_type_to_json_schema,
    register_function_tool,
)


# ---------------------------------------------------------------------------
# _python_type_to_json_schema
# ---------------------------------------------------------------------------

class TestTypeMapping:
    def test_str(self):
        assert _python_type_to_json_schema(str) == "string"

    def test_int(self):
        assert _python_type_to_json_schema(int) == "integer"

    def test_float(self):
        assert _python_type_to_json_schema(float) == "number"

    def test_bool(self):
        assert _python_type_to_json_schema(bool) == "boolean"

    def test_list(self):
        assert _python_type_to_json_schema(list) == "array"

    def test_dict(self):
        assert _python_type_to_json_schema(dict) == "object"

    def test_unknown_falls_back_to_string(self):
        assert _python_type_to_json_schema(bytes) == "string"


# ---------------------------------------------------------------------------
# _parse_docstring
# ---------------------------------------------------------------------------

class TestParseDocstring:
    def test_no_docstring(self):
        def fn():
            pass
        desc, params = _parse_docstring(fn)
        assert desc == ""
        assert params == {}

    def test_description_only(self):
        def fn():
            """Short description."""
        desc, params = _parse_docstring(fn)
        assert desc == "Short description."
        assert params == {}

    def test_google_style_args(self):
        def fn(x: int, y: str):
            """Do something.

            Args:
                x: The x value.
                y: The y value.
            """
        desc, params = _parse_docstring(fn)
        assert desc == "Do something."
        assert params == {"x": "The x value.", "y": "The y value."}

    def test_args_with_returns_section(self):
        def fn(a: int):
            """Compute.

            Args:
                a: Input value.

            Returns:
                The result.
            """
        desc, params = _parse_docstring(fn)
        assert desc == "Compute."
        assert params == {"a": "Input value."}
        # "Returns" section should not leak into params


# ---------------------------------------------------------------------------
# _extract_tool_config
# ---------------------------------------------------------------------------

class TestExtractToolConfig:
    def test_basic_function(self):
        def add(a: int, b: int):
            """Add two numbers.

            Args:
                a: First number.
                b: Second number.
            """
        cfg = _extract_tool_config(add)
        assert cfg.tool_id == "add"
        assert cfg.tool_type == "function"
        assert cfg.name == "add"
        assert cfg.description == "Add two numbers."
        assert cfg.parameters["type"] == "object"
        assert "a" in cfg.parameters["properties"]
        assert "b" in cfg.parameters["properties"]
        assert cfg.parameters["properties"]["a"]["type"] == "integer"
        assert cfg.parameters["properties"]["b"]["type"] == "integer"
        assert cfg.parameters["properties"]["a"]["description"] == "First number."
        assert set(cfg.parameters["required"]) == {"a", "b"}

    def test_custom_name_and_description(self):
        def foo(x: str):
            """Original."""
        cfg = _extract_tool_config(foo, name="bar", description="Custom desc")
        assert cfg.tool_id == "bar"
        assert cfg.name == "bar"
        assert cfg.description == "Custom desc"

    def test_optional_params_not_required(self):
        def greet(name: str, greeting: str = "hello"):
            """Greet someone."""
        cfg = _extract_tool_config(greet)
        assert "name" in cfg.parameters["required"]
        assert "greeting" not in cfg.parameters["required"]

    def test_no_annotations_default_to_string(self):
        def legacy(x, y):
            """Legacy function."""
        cfg = _extract_tool_config(legacy)
        assert cfg.parameters["properties"]["x"]["type"] == "string"
        assert cfg.parameters["properties"]["y"]["type"] == "string"

    def test_all_type_mappings(self):
        def multi(a: str, b: int, c: float, d: bool, e: list, f: dict):
            """Multi-type."""
        cfg = _extract_tool_config(multi)
        props = cfg.parameters["properties"]
        assert props["a"]["type"] == "string"
        assert props["b"]["type"] == "integer"
        assert props["c"]["type"] == "number"
        assert props["d"]["type"] == "boolean"
        assert props["e"]["type"] == "array"
        assert props["f"]["type"] == "object"


# ---------------------------------------------------------------------------
# register_function_tool decorator
# ---------------------------------------------------------------------------

class TestRegisterFunctionTool:
    def test_registers_and_returns_function(self):
        registry = ToolRegistry()

        @register_function_tool(registry)
        def get_weather(city: str, units: str = "celsius"):
            """Get weather for a city.

            Args:
                city: The city name.
                units: Temperature units.
            """
            return f"Weather in {city}"

        # The original function should be returned unchanged
        assert get_weather("Paris") == "Weather in Paris"

        # ToolConfig should be registered
        cfg = registry.get("get_weather")
        assert cfg is not None
        assert cfg.tool_type == "function"
        assert cfg.name == "get_weather"
        assert cfg.description == "Get weather for a city."
        assert cfg.parameters["type"] == "object"
        assert "city" in cfg.parameters["properties"]
        assert cfg.parameters["properties"]["city"]["type"] == "string"
        assert cfg.parameters["properties"]["city"]["description"] == "The city name."
        assert "city" in cfg.parameters["required"]
        assert "units" not in cfg.parameters["required"]

        # Callable should be registered
        fn = registry.get_callable("get_weather")
        assert fn is get_weather

    def test_custom_name(self):
        registry = ToolRegistry()

        @register_function_tool(registry, name="my_tool", description="My custom tool")
        def internal_fn(x: int):
            """Internal."""
            return x * 2

        assert registry.get("my_tool") is not None
        assert registry.get("my_tool").description == "My custom tool"
        assert registry.get_callable("my_tool") is internal_fn

    def test_multiple_tools(self):
        registry = ToolRegistry()

        @register_function_tool(registry)
        def tool_a(a: str):
            """Tool A."""

        @register_function_tool(registry)
        def tool_b(b: int):
            """Tool B."""

        assert len(registry.list_all()) == 2
        assert len(registry.list_by_type("function")) == 2
        assert registry.get("tool_a") is not None
        assert registry.get("tool_b") is not None

    def test_no_params_function(self):
        registry = ToolRegistry()

        @register_function_tool(registry)
        def ping():
            """Ping the server."""
            return "pong"

        cfg = registry.get("ping")
        assert cfg is not None
        assert cfg.parameters["properties"] == {}
        assert cfg.parameters["required"] == []

    def test_generated_config_is_valid_openai_schema(self):
        """Verify the generated ToolConfig has the required OpenAI schema structure."""
        registry = ToolRegistry()

        @register_function_tool(registry)
        def search(query: str, limit: int = 10):
            """Search for items.

            Args:
                query: Search query string.
                limit: Max results to return.
            """

        cfg = registry.get("search")
        params = cfg.parameters

        # Must have top-level "type": "object"
        assert params["type"] == "object"
        # Must have "properties" dict
        assert isinstance(params["properties"], dict)
        # Must have "required" list
        assert isinstance(params["required"], list)
        # Each property must have a "type" field
        for prop_name, prop_schema in params["properties"].items():
            assert "type" in prop_schema


# ---------------------------------------------------------------------------
# Property-Based Tests
# ---------------------------------------------------------------------------

import string
import types as _types
from hypothesis import given, settings, assume
import hypothesis.strategies as st

from runtime.tools import _extract_tool_config, _python_type_to_json_schema

# Feature: composable-agent-runtime, Property 7: 函数工具 Schema 自动生成

# Valid JSON Schema types that _python_type_to_json_schema can produce
_VALID_JSON_SCHEMA_TYPES = {"string", "integer", "number", "boolean", "array", "object"}

# Supported Python types and their expected JSON Schema mappings
_SUPPORTED_TYPES = [str, int, float, bool, list, dict]

# Strategy: generate a valid Python identifier for parameter names
import keyword as _keyword

_param_name_st = st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True).filter(
    lambda s: s.isidentifier() and not s.startswith("__") and not _keyword.iskeyword(s)
)

# Strategy: generate a single parameter spec (name, type, has_default)
_param_spec_st = st.tuples(
    _param_name_st,
    st.sampled_from(_SUPPORTED_TYPES),
    st.booleans(),  # has_default
)

# Strategy: generate a list of parameter specs with unique names
_param_list_st = st.lists(
    _param_spec_st, min_size=0, max_size=8
).map(
    # Deduplicate by name, keeping first occurrence
    lambda specs: list({name: (name, typ, has_def) for name, typ, has_def in specs}.values())
)


def _build_function(param_specs: list[tuple[str, type, bool]]):
    """Dynamically create a Python function from parameter specifications.

    Each spec is (param_name, param_type, has_default).
    Required params (has_default=False) come first, then optional ones.
    """
    # Sort: required params first, then optional
    sorted_specs = sorted(param_specs, key=lambda s: s[2])

    if not sorted_specs:
        # No-arg function
        def fn():
            """Auto-generated test function."""
            pass
        return fn

    # Build function source code
    parts = []
    for name, typ, has_default in sorted_specs:
        type_name = typ.__name__
        if has_default:
            # Use a simple default value for each type
            defaults = {
                "str": '""',
                "int": "0",
                "float": "0.0",
                "bool": "False",
                "list": "None",
                "dict": "None",
            }
            default_val = defaults.get(type_name, "None")
            parts.append(f"{name}: {type_name} = {default_val}")
        else:
            parts.append(f"{name}: {type_name}")

    params_str = ", ".join(parts)
    func_code = f"def _generated_fn({params_str}):\n    \"\"\"Auto-generated test function.\"\"\"\n    pass"

    local_ns: dict = {}
    exec(func_code, {"__builtins__": __builtins__}, local_ns)
    return local_ns["_generated_fn"]


class TestProperty7FunctionToolSchemaGeneration:
    """Property 7: 函数工具 Schema 自动生成

    For any Python function with typed parameters, _extract_tool_config()
    should generate a ToolConfig whose parameters field is a valid
    OpenAI function calling JSON Schema with correct structure and
    parameter names/types matching the function signature.

    **Validates: Requirements 2.1, 2.2**
    """

    @given(param_specs=_param_list_st)
    @settings(max_examples=150)
    def test_schema_structure_is_valid_json_schema(self, param_specs):
        """Generated ToolConfig always has valid JSON Schema structure."""
        fn = _build_function(param_specs)
        cfg = _extract_tool_config(fn)

        params = cfg.parameters

        # Top-level must be "object"
        assert params["type"] == "object"
        # "properties" must be a dict
        assert isinstance(params["properties"], dict)
        # "required" must be a list
        assert isinstance(params["required"], list)

        # Each property must have a "type" with a valid JSON Schema type
        for prop_name, prop_schema in params["properties"].items():
            assert "type" in prop_schema, f"Property '{prop_name}' missing 'type'"
            assert prop_schema["type"] in _VALID_JSON_SCHEMA_TYPES, (
                f"Property '{prop_name}' has invalid type '{prop_schema['type']}'"
            )

    @given(param_specs=_param_list_st)
    @settings(max_examples=150)
    def test_parameter_names_match_function_signature(self, param_specs):
        """Schema property names match the function's parameter names exactly."""
        fn = _build_function(param_specs)
        cfg = _extract_tool_config(fn)

        expected_names = {name for name, _, _ in param_specs}
        actual_names = set(cfg.parameters["properties"].keys())
        assert actual_names == expected_names

    @given(param_specs=_param_list_st)
    @settings(max_examples=150)
    def test_parameter_types_match_annotations(self, param_specs):
        """Schema types correspond to the Python type annotations."""
        fn = _build_function(param_specs)
        cfg = _extract_tool_config(fn)

        for name, typ, _ in param_specs:
            expected_json_type = _python_type_to_json_schema(typ)
            actual_json_type = cfg.parameters["properties"][name]["type"]
            assert actual_json_type == expected_json_type, (
                f"Param '{name}': expected '{expected_json_type}', got '{actual_json_type}'"
            )

    @given(param_specs=_param_list_st)
    @settings(max_examples=150)
    def test_required_params_are_those_without_defaults(self, param_specs):
        """Only parameters without default values appear in 'required'."""
        fn = _build_function(param_specs)
        cfg = _extract_tool_config(fn)

        expected_required = {name for name, _, has_default in param_specs if not has_default}
        actual_required = set(cfg.parameters["required"])
        assert actual_required == expected_required

    @given(param_specs=_param_list_st)
    @settings(max_examples=150)
    def test_tool_config_metadata(self, param_specs):
        """Generated ToolConfig has correct tool_type and name."""
        fn = _build_function(param_specs)
        cfg = _extract_tool_config(fn)

        assert cfg.tool_type == "function"
        assert cfg.tool_id == fn.__name__
        assert cfg.name == fn.__name__
        assert isinstance(cfg.description, str)
        assert len(cfg.description) > 0
