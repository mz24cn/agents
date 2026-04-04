# Feature: composable-agent-runtime, Property 5: 输入格式归一化
"""Property-based tests for Runtime input format normalization.

Verifies that Runtime._normalize_messages() produces a consistent message
format regardless of whether the input is plain text or a pre-built
messages list.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from runtime.models import InferenceRequest, Message
from runtime.runtime import Runtime


# --- Hypothesis strategies ---

# Strategy for non-empty text strings (plain text input)
text_input_st = st.text(min_size=1, max_size=200)

# Strategy for Message objects with various roles
message_st = st.builds(
    Message,
    role=st.sampled_from(["system", "user", "assistant", "function"]),
    content=st.text(max_size=200),
    name=st.none(),
    images=st.none(),
    audio=st.none(),
)

# Strategy for non-empty message lists
messages_list_st = st.lists(message_st, min_size=1, max_size=10)


# --- Property tests ---

# **Validates: Requirements 1.5, 1.6**


@given(text=text_input_st)
@settings(max_examples=200)
def test_normalize_text_input_wraps_as_user_message(text: str) -> None:
    """For any plain text input, _normalize_messages should wrap it as
    [Message(role="user", content=text)]."""
    request = InferenceRequest(model_id="test-model", text=text)
    result = Runtime._normalize_messages(request)

    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], Message)
    assert result[0].role == "user"
    assert result[0].content == text


@given(messages=messages_list_st)
@settings(max_examples=200)
def test_normalize_messages_input_passes_through_unchanged(
    messages: list,
) -> None:
    """For any pre-built messages list, _normalize_messages should return
    the same messages unchanged."""
    request = InferenceRequest(model_id="test-model", messages=messages)
    result = Runtime._normalize_messages(request)

    assert isinstance(result, list)
    assert len(result) == len(messages)
    for original, normalized in zip(messages, result):
        assert normalized.role == original.role
        assert normalized.content == original.content


@given(text=text_input_st, messages=messages_list_st)
@settings(max_examples=200)
def test_normalize_messages_takes_precedence_over_text(
    text: str, messages: list
) -> None:
    """When both text and messages are provided, messages should take
    precedence (as documented in _normalize_messages)."""
    request = InferenceRequest(
        model_id="test-model", text=text, messages=messages
    )
    result = Runtime._normalize_messages(request)

    # messages takes precedence, so result should match messages, not text
    assert len(result) == len(messages)
    for original, normalized in zip(messages, result):
        assert normalized.role == original.role
        assert normalized.content == original.content


@given(text=text_input_st)
@settings(max_examples=100)
def test_normalize_text_and_messages_produce_consistent_format(
    text: str,
) -> None:
    """Both text input and messages input should produce a list of Message
    objects — the normalized format is always list[Message]."""
    # Text input path
    text_request = InferenceRequest(model_id="test-model", text=text)
    text_result = Runtime._normalize_messages(text_request)

    # Equivalent messages input path
    equivalent_messages = [Message(role="user", content=text)]
    msg_request = InferenceRequest(
        model_id="test-model", messages=equivalent_messages
    )
    msg_result = Runtime._normalize_messages(msg_request)

    # Both should produce the same normalized output
    assert len(text_result) == len(msg_result)
    assert text_result[0].role == msg_result[0].role
    assert text_result[0].content == msg_result[0].content


def test_normalize_empty_input_returns_empty_list() -> None:
    """When neither text nor messages is provided, should return empty list."""
    request = InferenceRequest(model_id="test-model")
    result = Runtime._normalize_messages(request)
    assert result == []


# Feature: composable-agent-runtime, Property 12: 选择性工具启用
"""Property-based test for selective tool enabling.

Verifies that when a subset of tool_ids is requested, only those tools
are gathered from the ToolRegistry — no more, no less.
"""

from runtime.models import ToolConfig
from runtime.registry import ToolRegistry


# --- Hypothesis strategy for ToolConfig ---

_tool_type_st = st.sampled_from(["function", "mcp", "skill"])

_tool_config_st = st.builds(
    ToolConfig,
    tool_id=st.text(
        alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-"),
        min_size=1,
        max_size=30,
    ),
    tool_type=_tool_type_st,
    name=st.text(min_size=1, max_size=50),
    description=st.text(max_size=100),
    parameters=st.just({"type": "object", "properties": {}, "required": []}),
)


def _unique_tool_configs(configs: list[ToolConfig]) -> list[ToolConfig]:
    """Deduplicate tool configs by tool_id, keeping the first occurrence."""
    seen: set[str] = set()
    result: list[ToolConfig] = []
    for cfg in configs:
        if cfg.tool_id not in seen:
            seen.add(cfg.tool_id)
            result.append(cfg)
    return result


# **Validates: Requirements 3.6, 5.1, 5.3**


@given(
    configs=st.lists(_tool_config_st, min_size=1, max_size=20),
    data=st.data(),
)
@settings(max_examples=200)
def test_selective_tool_enabling_returns_exact_subset(
    configs: list[ToolConfig], data: st.DataObject
) -> None:
    """For any ToolRegistry with N tools and any subset of tool_ids,
    gathering tools by those IDs should return exactly the matching subset."""
    # Deduplicate by tool_id so the registry has unique entries
    unique_configs = _unique_tool_configs(configs)

    # Build registry
    registry = ToolRegistry()
    for cfg in unique_configs:
        registry.register(cfg)

    all_ids = [cfg.tool_id for cfg in unique_configs]

    # Draw a random subset of tool_ids (may be empty, may be all)
    subset_ids = data.draw(
        st.lists(st.sampled_from(all_ids), unique=True, max_size=len(all_ids)),
        label="selected_tool_ids",
    )

    # Gather tools using the same logic as Runtime.infer()
    gathered: list[ToolConfig] = []
    for tool_id in subset_ids:
        tool_config = registry.get(tool_id)
        if tool_config is not None:
            gathered.append(tool_config)

    # Property: gathered set equals exactly the requested subset
    gathered_ids = {cfg.tool_id for cfg in gathered}
    expected_ids = set(subset_ids)

    assert gathered_ids == expected_ids, (
        f"Expected tool_ids {expected_ids}, got {gathered_ids}"
    )

    # Property: count matches (no duplicates introduced)
    assert len(gathered) == len(subset_ids)

    # Property: each gathered config matches the registered one
    for cfg in gathered:
        registered = registry.get(cfg.tool_id)
        assert registered is not None
        assert registered.tool_id == cfg.tool_id
        assert registered.name == cfg.name
        assert registered.tool_type == cfg.tool_type


@given(
    configs=st.lists(_tool_config_st, min_size=1, max_size=15),
    extra_ids=st.lists(
        st.text(
            alphabet=st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
            min_size=1,
            max_size=20,
        ),
        min_size=1,
        max_size=5,
    ),
    data=st.data(),
)
@settings(max_examples=200)
def test_selective_tool_enabling_ignores_unknown_ids(
    configs: list[ToolConfig],
    extra_ids: list[str],
    data: st.DataObject,
) -> None:
    """When tool_ids include IDs not in the registry, those are silently
    skipped — only registered tools are gathered."""
    unique_configs = _unique_tool_configs(configs)

    registry = ToolRegistry()
    for cfg in unique_configs:
        registry.register(cfg)

    all_ids = [cfg.tool_id for cfg in unique_configs]

    # Draw a subset of valid IDs
    valid_subset = data.draw(
        st.lists(st.sampled_from(all_ids), unique=True, max_size=len(all_ids)),
        label="valid_ids",
    )

    # Combine with unknown IDs (uppercase ensures no collision with lowercase tool_ids)
    request_ids = valid_subset + extra_ids

    # Gather using Runtime.infer() logic
    gathered: list[ToolConfig] = []
    for tool_id in request_ids:
        tool_config = registry.get(tool_id)
        if tool_config is not None:
            gathered.append(tool_config)

    gathered_ids = {cfg.tool_id for cfg in gathered}
    expected_ids = set(valid_subset)

    # Property: only valid IDs are gathered, unknown IDs are ignored
    assert gathered_ids == expected_ids
    assert len(gathered) == len(valid_subset)


# Feature: composable-agent-runtime, Property 15: 工具调用循环与最大轮次限制
"""Property-based test for tool call loop and max rounds limit.

Verifies that when the model continuously returns function_call responses,
the Runtime.infer() tool call loop terminates after max_tool_rounds rounds.
"""

import io
import json
from unittest.mock import patch, MagicMock

from runtime.models import ModelConfig, InferenceRequest
from runtime.registry import ModelRegistry

# **Validates: Requirements 5.4, 5.5**


def _make_openai_function_call_response(tool_name: str, arguments: str = "{}") -> bytes:
    """Build a fake OpenAI Chat Completions JSON response with a tool_call."""
    return json.dumps({
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_test",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": arguments,
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }).encode("utf-8")


@given(max_rounds=st.integers(min_value=1, max_value=10))
@settings(max_examples=100)
def test_tool_call_loop_terminates_at_max_rounds(max_rounds: int) -> None:
    """For any max_tool_rounds value N (1-10), when the model always returns
    function_call, the Runtime should terminate after N tool call rounds.

    The tool callable should be invoked exactly N times, and urlopen should
    be called exactly N+1 times (N rounds + 1 final call where the loop
    breaks before executing the tool).
    """
    # 1. Set up registries
    model_registry = ModelRegistry()
    model_registry.register(
        ModelConfig(
            model_id="test-model",
            api_base="http://localhost:9999",
            model_name="test",
            api_protocol="openai",
        )
    )

    tool_registry = ToolRegistry()
    tool_call_count = 0

    def dummy_tool() -> str:
        nonlocal tool_call_count
        tool_call_count += 1
        return "tool_result"

    tool_config = ToolConfig(
        tool_id="dummy_tool",
        tool_type="function",
        name="dummy_tool",
        description="A dummy tool for testing",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    tool_registry.register(tool_config, callable_fn=dummy_tool)

    # 2. Build mock response that always returns a function_call
    response_bytes = _make_openai_function_call_response("dummy_tool")

    urlopen_call_count = 0

    def mock_urlopen(request, **kwargs):
        nonlocal urlopen_call_count
        urlopen_call_count += 1
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_bytes
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    # 3. Run inference with max_tool_rounds=N
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)
    request = InferenceRequest(
        model_id="test-model",
        tool_ids=["dummy_tool"],
        text="hello",
        max_tool_rounds=max_rounds,
    )

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        result = runtime.infer(request)

    # 4. Verify properties
    # The inference should complete successfully (not hang)
    assert result.success is True

    # The tool should have been called exactly N times
    assert tool_call_count == max_rounds, (
        f"Expected tool to be called {max_rounds} times, got {tool_call_count}"
    )

    # urlopen should be called N+1 times:
    # N rounds where tool executes + 1 final round where loop breaks
    assert urlopen_call_count == max_rounds + 1, (
        f"Expected urlopen called {max_rounds + 1} times, got {urlopen_call_count}"
    )

    # Conversation history should contain:
    # 1 user message + N * (assistant + function) + 1 final assistant = 2N + 2
    assert result.messages is not None
    expected_msg_count = 2 * max_rounds + 2
    assert len(result.messages) == expected_msg_count, (
        f"Expected {expected_msg_count} messages, got {len(result.messages)}"
    )

    # First message should be the user message
    assert result.messages[0].role == "user"
    assert result.messages[0].content == "hello"

    # Last message should be an assistant message (the one that triggered the break)
    assert result.messages[-1].role == "assistant"

    # Verify the pattern: user, (assistant, function) * N, assistant
    for i in range(max_rounds):
        assistant_idx = 1 + 2 * i
        function_idx = 2 + 2 * i
        assert result.messages[assistant_idx].role == "assistant"
        assert result.messages[assistant_idx].tool_calls is not None
        assert result.messages[function_idx].role == "function"
        assert result.messages[function_idx].name == "dummy_tool"


# Feature: composable-agent-runtime, Property 8: 工具分发与执行
"""Property-based test for tool dispatch and execution.

Verifies that after registering a function tool, when the model returns a
function_call response matching that tool, the Runtime successfully looks up
the tool, executes it, and the tool result appears in the conversation as a
function role message.
"""

# **Validates: Requirements 2.4, 4.3**

# Strategy: tool names must be valid identifiers (letters, digits, underscores)
_tool_name_st = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_"),
    min_size=1,
    max_size=30,
).filter(lambda s: s[0].isalpha())

# Strategy: argument values (simple strings for tool arguments)
_arg_value_st = st.text(min_size=0, max_size=100)


def _make_openai_plain_text_response(content: str) -> bytes:
    """Build a fake OpenAI Chat Completions JSON response with plain text (no tool_call)."""
    return json.dumps({
        "id": "chatcmpl-final",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
    }).encode("utf-8")


@given(
    tool_name=_tool_name_st,
    arg_value=_arg_value_st,
)
@settings(max_examples=100)
def test_tool_dispatch_and_execution(tool_name: str, arg_value: str) -> None:
    """For any registered tool with a matching function_call, Runtime
    successfully dispatches and executes it.

    Steps:
        1. Register a function tool with a random name in ToolRegistry
        2. Mock urlopen to return a function_call response for that tool on
           the first call, then a plain text response on the second call
        3. Run Runtime.infer() and verify:
           - The tool was found and executed (callable was invoked)
           - The tool result appears in conversation as a function role message
           - The inference completes successfully
    """
    # 1. Set up registries
    model_registry = ModelRegistry()
    model_registry.register(
        ModelConfig(
            model_id="test-model",
            api_base="http://localhost:9999",
            model_name="test",
            api_protocol="openai",
        )
    )

    tool_registry = ToolRegistry()
    tool_was_called = False
    received_arg = None

    def tool_fn(input_value: str = "") -> str:
        nonlocal tool_was_called, received_arg
        tool_was_called = True
        received_arg = input_value
        return f"result_for_{input_value}"

    tool_config = ToolConfig(
        tool_id=tool_name,
        tool_type="function",
        name=tool_name,
        description=f"Test tool {tool_name}",
        parameters={
            "type": "object",
            "properties": {
                "input_value": {"type": "string", "description": "input value"},
            },
            "required": [],
        },
    )
    tool_registry.register(tool_config, callable_fn=tool_fn)

    # 2. Build mock responses:
    #    First call -> function_call targeting our tool
    #    Second call -> plain text (no tool call, ends the loop)
    arguments_json = json.dumps({"input_value": arg_value})
    function_call_response = _make_openai_function_call_response(
        tool_name, arguments_json
    )
    plain_text_response = _make_openai_plain_text_response("done")

    call_index = 0

    def mock_urlopen(request, **kwargs):
        nonlocal call_index
        call_index += 1
        mock_resp = MagicMock()
        if call_index == 1:
            mock_resp.read.return_value = function_call_response
        else:
            mock_resp.read.return_value = plain_text_response
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    # 3. Run inference
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)
    request = InferenceRequest(
        model_id="test-model",
        tool_ids=[tool_name],
        text="please call the tool",
    )

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        result = runtime.infer(request)

    # 4. Verify properties

    # Inference completed successfully
    assert result.success is True, f"Inference failed: {result.error}"

    # The tool was found and executed
    assert tool_was_called is True, (
        f"Tool '{tool_name}' was not called — dispatch failed"
    )

    # The tool received the correct argument
    assert received_arg == arg_value, (
        f"Expected arg '{arg_value}', got '{received_arg}'"
    )

    # The conversation should contain a function role message with the tool result
    function_messages = [
        m for m in result.messages if m.role == "function"
    ]
    assert len(function_messages) >= 1, (
        "No function role message found in conversation"
    )

    # The function message should reference our tool by name
    fn_msg = function_messages[0]
    assert fn_msg.name == tool_name, (
        f"Expected function message name '{tool_name}', got '{fn_msg.name}'"
    )

    # The function message content should contain the tool's return value
    expected_result = f"result_for_{arg_value}"
    assert fn_msg.content == expected_result, (
        f"Expected tool result '{expected_result}', got '{fn_msg.content}'"
    )

    # urlopen was called exactly twice (function_call + final plain text)
    assert call_index == 2, (
        f"Expected 2 urlopen calls, got {call_index}"
    )

    # Conversation structure: user, assistant(function_call), function, assistant(done)
    assert len(result.messages) == 4, (
        f"Expected 4 messages, got {len(result.messages)}"
    )
    assert result.messages[0].role == "user"
    assert result.messages[1].role == "assistant"
    assert result.messages[1].tool_calls is not None
    assert result.messages[1].tool_calls[0]["name"] == tool_name
    assert result.messages[2].role == "function"
    assert result.messages[3].role == "assistant"
    assert result.messages[3].tool_calls is None


# Feature: composable-agent-runtime, Property 9: 工具错误处理
"""Property-based test for tool error handling.

Verifies three error scenarios:
(a) tool_name not in registry → result contains "not found"
(b) tool execution raises exception → result contains exception type and description
(c) HTTP call failure → InferenceResult has success=False and error_code set
"""

import urllib.error

# **Validates: Requirements 2.5, 2.6, 1.10, 3.9**


# --- Strategies ---

# Random tool names that are NOT registered (uppercase to avoid collision with registered tools)
_nonexistent_tool_name_st = st.text(
    alphabet=st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
    min_size=1,
    max_size=30,
)

# Random exception messages
_exception_msg_st = st.text(min_size=1, max_size=100)

# Random HTTP error codes
_http_error_code_st = st.sampled_from([400, 401, 403, 404, 500, 502, 503])


def _setup_model_registry() -> ModelRegistry:
    """Create a ModelRegistry with a test model."""
    registry = ModelRegistry()
    registry.register(
        ModelConfig(
            model_id="test-model",
            api_base="http://localhost:9999",
            model_name="test",
            api_protocol="openai",
        )
    )
    return registry


# --- Scenario (a): tool_name not found ---


@given(bad_tool_name=_nonexistent_tool_name_st)
@settings(max_examples=50)
def test_tool_error_not_found(bad_tool_name: str) -> None:
    """When the model returns a function_call for a tool_name that does NOT
    exist in the ToolRegistry, the function role message should contain
    'not found'."""
    model_registry = _setup_model_registry()
    tool_registry = ToolRegistry()

    # Register a real tool so tool_ids is non-empty, but the model will
    # call a different (non-existent) tool name.
    real_tool = ToolConfig(
        tool_id="real_tool",
        tool_type="function",
        name="real_tool",
        description="A real tool",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    tool_registry.register(real_tool, callable_fn=lambda: "ok")

    # First call: model returns function_call for the non-existent tool
    # Second call: model returns plain text (ends the loop)
    fc_response = _make_openai_function_call_response(bad_tool_name)
    plain_response = _make_openai_plain_text_response("done")
    call_idx = 0

    def mock_urlopen(request, **kwargs):
        nonlocal call_idx
        call_idx += 1
        mock_resp = MagicMock()
        mock_resp.read.return_value = fc_response if call_idx == 1 else plain_response
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)
    request = InferenceRequest(
        model_id="test-model",
        tool_ids=["real_tool"],
        text="hello",
    )

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        result = runtime.infer(request)

    # The inference should still succeed (error is in the function message, not fatal)
    assert result.success is True

    # Find the function role message for the bad tool
    fn_msgs = [m for m in result.messages if m.role == "function" and m.name == bad_tool_name]
    assert len(fn_msgs) >= 1, f"No function message for '{bad_tool_name}'"
    assert "not found" in fn_msgs[0].content.lower(), (
        f"Expected 'not found' in function message, got: {fn_msgs[0].content}"
    )


# --- Scenario (b): tool throws exception ---


@given(exc_msg=_exception_msg_st)
@settings(max_examples=50)
def test_tool_error_exception(exc_msg: str) -> None:
    """When a registered tool raises an exception during execution, the
    function role message should contain the exception type name and
    description."""
    model_registry = _setup_model_registry()
    tool_registry = ToolRegistry()

    def failing_tool() -> str:
        raise ValueError(exc_msg)

    tool_config = ToolConfig(
        tool_id="failing_tool",
        tool_type="function",
        name="failing_tool",
        description="A tool that always fails",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    tool_registry.register(tool_config, callable_fn=failing_tool)

    fc_response = _make_openai_function_call_response("failing_tool")
    plain_response = _make_openai_plain_text_response("done")
    call_idx = 0

    def mock_urlopen(request, **kwargs):
        nonlocal call_idx
        call_idx += 1
        mock_resp = MagicMock()
        mock_resp.read.return_value = fc_response if call_idx == 1 else plain_response
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)
    request = InferenceRequest(
        model_id="test-model",
        tool_ids=["failing_tool"],
        text="hello",
    )

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        result = runtime.infer(request)

    assert result.success is True

    fn_msgs = [m for m in result.messages if m.role == "function" and m.name == "failing_tool"]
    assert len(fn_msgs) >= 1, "No function message for 'failing_tool'"

    content = fn_msgs[0].content
    # Should contain the exception type name
    assert "ValueError" in content, (
        f"Expected 'ValueError' in function message, got: {content}"
    )
    # Should contain the exception description
    assert exc_msg in content, (
        f"Expected exception message '{exc_msg}' in function message, got: {content}"
    )


# --- Scenario (c): HTTP call failure ---


@given(http_code=_http_error_code_st)
@settings(max_examples=34)
def test_tool_error_http_failure(http_code: int) -> None:
    """When urllib.request.urlopen raises an HTTPError, the InferenceResult
    should have success=False and error_code set to the HTTP status code."""
    model_registry = _setup_model_registry()
    tool_registry = ToolRegistry()

    def mock_urlopen(request, **kwargs):
        raise urllib.error.HTTPError(
            url="http://localhost:9999/v1/chat/completions",
            code=http_code,
            msg="Simulated error",
            hdrs={},
            fp=io.BytesIO(b"error body"),
        )

    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)
    request = InferenceRequest(
        model_id="test-model",
        text="hello",
    )

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        result = runtime.infer(request)

    assert result.success is False, "Expected success=False for HTTP error"
    assert result.error_code is not None, "Expected error_code to be set"
    assert result.error_code == str(http_code), (
        f"Expected error_code '{http_code}', got '{result.error_code}'"
    )
    assert result.error is not None, "Expected error message to be set"


# Feature: composable-agent-runtime, Property 16: 工具实例复用
"""Property-based test for tool instance reuse.

Verifies that for any tool_id, calling ToolRegistry.get_callable() multiple
times (simulating multiple inference sessions) always returns the same
callable object (same id()), confirming tool instance reuse.
"""

# **Validates: Requirements 5.6**


# Strategy: number of simulated inference sessions (2-10)
_session_count_st = st.integers(min_value=2, max_value=10)

# Strategy: tool_id strings
_tool_id_st = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_"),
    min_size=1,
    max_size=30,
).filter(lambda s: s[0].isalpha())


@given(
    tool_id=_tool_id_st,
    num_sessions=_session_count_st,
)
@settings(max_examples=100)
def test_tool_instance_reuse_across_sessions(tool_id: str, num_sessions: int) -> None:
    """For any tool_id registered in ToolRegistry, get_callable() returns
    the same callable object (same id()) across multiple inference sessions.

    Steps:
        1. Register a function tool in ToolRegistry
        2. Call get_callable(tool_id) num_sessions times (simulating 2-10
           inference sessions)
        3. Verify all returned callables have the same id()
    """
    registry = ToolRegistry()

    def my_tool_fn(x: str = "") -> str:
        return f"result_{x}"

    tool_config = ToolConfig(
        tool_id=tool_id,
        tool_type="function",
        name=tool_id,
        description=f"Test tool {tool_id}",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    registry.register(tool_config, callable_fn=my_tool_fn)

    # Simulate multiple inference sessions by calling get_callable repeatedly
    callables = [registry.get_callable(tool_id) for _ in range(num_sessions)]

    # All returned callables must be non-None
    for i, c in enumerate(callables):
        assert c is not None, (
            f"get_callable returned None on session {i} for tool_id='{tool_id}'"
        )

    # All returned callables must be the exact same object (same id())
    first_id = id(callables[0])
    for i, c in enumerate(callables[1:], start=1):
        assert id(c) == first_id, (
            f"Session {i}: get_callable returned a different object "
            f"(id={id(c)}) than session 0 (id={first_id}) for tool_id='{tool_id}'"
        )
