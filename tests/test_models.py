# Feature: composable-agent-runtime, Property 1: ModelConfig 序列化往返一致性
"""Property-based tests for data model serialization round-trip consistency."""

from hypothesis import given, settings
from hypothesis import strategies as st

from runtime.models import ModelConfig


# --- Hypothesis strategies ---

# Strategy for generate_params: a dict with string keys and JSON-compatible values
json_primitives = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53), max_value=2**53),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=50),
)

generate_params_st = st.dictionaries(
    keys=st.text(min_size=1, max_size=20),
    values=json_primitives,
    max_size=10,
)

model_config_st = st.builds(
    ModelConfig,
    model_id=st.text(min_size=1, max_size=50),
    api_base=st.text(min_size=1, max_size=100),
    model_name=st.text(min_size=1, max_size=50),
    api_key=st.text(max_size=100),
    model_type=st.sampled_from(["llm", "vlm"]),
    api_protocol=st.sampled_from(["openai", "ollama"]),
    generate_params=generate_params_st,
)


# --- Property test ---

# **Validates: Requirements 1.11, 1.12**
@given(config=model_config_st)
@settings(max_examples=200)
def test_model_config_round_trip(config: ModelConfig) -> None:
    """For any valid ModelConfig, from_dict(to_dict(config)) should equal the original."""
    serialized = config.to_dict()
    deserialized = ModelConfig.from_dict(serialized)

    assert deserialized.model_id == config.model_id
    assert deserialized.api_base == config.api_base
    assert deserialized.model_name == config.model_name
    assert deserialized.api_key == config.api_key
    assert deserialized.model_type == config.model_type
    assert deserialized.api_protocol == config.api_protocol
    assert deserialized.generate_params == config.generate_params

# Feature: composable-agent-runtime, Property 2: ToolConfig 序列化往返一致性（所有工具类型）

from runtime.models import ToolConfig

# --- Hypothesis strategies for ToolConfig ---

# Strategy for parameters: an OpenAI function calling JSON Schema dict
parameters_st = st.fixed_dictionaries(
    {
        "type": st.just("object"),
        "properties": st.dictionaries(
            keys=st.text(min_size=1, max_size=20),
            values=st.fixed_dictionaries(
                {"type": st.sampled_from(["string", "integer", "number", "boolean"])}
            ),
            max_size=5,
        ),
        "required": st.just([]),
    }
)

# Strategy for skill step dicts
skill_step_st = st.fixed_dictionaries(
    {
        "type": st.sampled_from(["tool", "inference"]),
        "target": st.text(min_size=1, max_size=30),
    }
)

# Strategy for function-type ToolConfig
function_tool_config_st = st.builds(
    ToolConfig,
    tool_id=st.text(min_size=1, max_size=50),
    tool_type=st.just("function"),
    name=st.text(min_size=1, max_size=50),
    description=st.text(min_size=0, max_size=200),
    parameters=parameters_st,
    mcp_server_name=st.just(None),
    tool_name=st.just(None),
    steps=st.just(None),
)

# Strategy for mcp-type ToolConfig
mcp_tool_config_st = st.builds(
    ToolConfig,
    tool_id=st.text(min_size=1, max_size=50),
    tool_type=st.just("mcp"),
    name=st.text(min_size=1, max_size=50),
    description=st.text(min_size=0, max_size=200),
    parameters=parameters_st,
    mcp_server_name=st.text(min_size=1, max_size=30),
    tool_name=st.text(min_size=1, max_size=30),
    steps=st.just(None),
)

# Strategy for skill-type ToolConfig
skill_tool_config_st = st.builds(
    ToolConfig,
    tool_id=st.text(min_size=1, max_size=50),
    tool_type=st.just("skill"),
    name=st.text(min_size=1, max_size=50),
    description=st.text(min_size=0, max_size=200),
    parameters=parameters_st,
    mcp_server_name=st.just(None),
    tool_name=st.just(None),
    steps=st.lists(skill_step_st, min_size=1, max_size=5),
)

# Combined strategy covering all three tool types
tool_config_st = st.one_of(
    function_tool_config_st,
    mcp_tool_config_st,
    skill_tool_config_st,
)


# --- Property tests ---

# **Validates: Requirements 2.7, 2.8, 3.10, 3.11, 4.6, 4.7**
@given(config=function_tool_config_st)
@settings(max_examples=100)
def test_function_tool_config_round_trip(config: ToolConfig) -> None:
    """For any valid function-type ToolConfig, from_dict(to_dict(config)) should equal the original."""
    serialized = config.to_dict()
    deserialized = ToolConfig.from_dict(serialized)

    assert deserialized.tool_id == config.tool_id
    assert deserialized.tool_type == config.tool_type
    assert deserialized.name == config.name
    assert deserialized.description == config.description
    assert deserialized.parameters == config.parameters
    assert deserialized.mcp_server_name is None
    assert deserialized.tool_name is None
    assert deserialized.steps is None


@given(config=mcp_tool_config_st)
@settings(max_examples=100)
def test_mcp_tool_config_round_trip(config: ToolConfig) -> None:
    """For any valid mcp-type ToolConfig, from_dict(to_dict(config)) should equal the original."""
    serialized = config.to_dict()
    deserialized = ToolConfig.from_dict(serialized)

    assert deserialized.tool_id == config.tool_id
    assert deserialized.tool_type == config.tool_type
    assert deserialized.name == config.name
    assert deserialized.description == config.description
    assert deserialized.parameters == config.parameters
    assert deserialized.mcp_server_name == config.mcp_server_name
    assert deserialized.tool_name == config.tool_name
    assert deserialized.steps is None


@given(config=skill_tool_config_st)
@settings(max_examples=100)
def test_skill_tool_config_round_trip(config: ToolConfig) -> None:
    """For any valid skill-type ToolConfig, from_dict(to_dict(config)) should equal the original."""
    serialized = config.to_dict()
    deserialized = ToolConfig.from_dict(serialized)

    assert deserialized.tool_id == config.tool_id
    assert deserialized.tool_type == config.tool_type
    assert deserialized.name == config.name
    assert deserialized.description == config.description
    assert deserialized.parameters == config.parameters
    assert deserialized.mcp_server_name is None
    assert deserialized.tool_name is None
    assert deserialized.steps == config.steps


@given(config=tool_config_st)
@settings(max_examples=100)
def test_tool_config_round_trip_all_types(config: ToolConfig) -> None:
    """For any valid ToolConfig (function/mcp/skill), from_dict(to_dict(config)) should equal the original."""
    serialized = config.to_dict()
    deserialized = ToolConfig.from_dict(serialized)

    assert deserialized.tool_id == config.tool_id
    assert deserialized.tool_type == config.tool_type
    assert deserialized.name == config.name
    assert deserialized.description == config.description
    assert deserialized.parameters == config.parameters
    assert deserialized.mcp_server_name == config.mcp_server_name
    assert deserialized.tool_name == config.tool_name
    assert deserialized.steps == config.steps
