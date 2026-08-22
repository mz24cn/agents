# Feature: agent-service, Property 5: 输入格式归一化
"""Property-based tests for Runtime input format normalization.

Verifies that Runtime._normalize_messages() produces a consistent message
format regardless of whether the input is plain text or a pre-built
messages list.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from runtime.models import InferenceRequest, Message, ModelConfig
from runtime.runtime import Runtime
from runtime.registry import ModelRegistry, ToolRegistry


def test_ensure_tool_call_results_repairs_dangling_and_malformed_call():
    from runtime.runtime import _ensure_tool_call_results

    messages = [
        Message(role="user", content="inspect"),
        Message(role="assistant", tool_calls=[{
            "id": "call-1",
            "name": "read_image",
            "arguments": "{\"path\": \"partial",
        }]),
    ]

    repaired = _ensure_tool_call_results(messages)
    assert [message.role for message in repaired] == ["user", "assistant", "tool"]
    assert repaired[1].tool_calls[0]["arguments"] == "{}"
    assert repaired[2].tool_use_id == "call-1"
    assert "[interrupted]" in repaired[2].content


def test_ensure_tool_call_results_drops_orphan_tool_result():
    from runtime.runtime import _ensure_tool_call_results

    repaired = _ensure_tool_call_results([
        Message(role="user", content="hello"),
        Message(role="tool", name="read_file", tool_use_id="orphan", content="x"),
        Message(role="assistant", content="done"),
    ])
    assert [message.role for message in repaired] == ["user", "assistant"]


def test_prepare_reasoning_for_tool_rounds_repairs_request_copy_only():
    from runtime.runtime import _prepare_reasoning_for_tool_rounds

    original = Message(role="assistant", content="", tool_calls=[{
        "id": "call-1", "name": "lookup", "arguments": "{}",
    }])
    messages = [
        Message(role="assistant", content="old answer"),
        Message(role="user", content="look it up"),
        original,
        Message(role="tool", content="result", name="lookup", tool_use_id="call-1"),
    ]
    config = ModelConfig(
        model_id="deepseek",
        api_base="https://example.test",
        model_name="deepseek",
        labels=["require-reasoning-for-tool-rounds"],
    )

    prepared = _prepare_reasoning_for_tool_rounds(messages, config)

    assert prepared is not messages
    assert prepared[0] is messages[0]  # before the latest user: out of scope
    assert prepared[2] is not original
    assert prepared[2].thinking
    assert original.thinking is None  # synthetic reasoning must not leak to persistence


def test_prepare_reasoning_for_tool_rounds_preserves_real_reasoning():
    from runtime.runtime import _prepare_reasoning_for_tool_rounds

    assistant = Message(role="assistant", thinking="real reasoning")
    messages = [
        Message(role="user", content="question"),
        assistant,
        Message(role="tool", content="result"),
    ]
    config = ModelConfig(
        model_id="deepseek",
        api_base="https://example.test",
        model_name="deepseek",
        labels=["require-reasoning-for-tool-rounds"],
    )

    prepared = _prepare_reasoning_for_tool_rounds(messages, config)

    assert prepared is messages
    assert prepared[1].thinking == "real reasoning"


def test_prepare_reasoning_for_tool_rounds_requires_label_and_trailing_tool():
    from runtime.runtime import _prepare_reasoning_for_tool_rounds

    assistant = Message(role="assistant", content="answer")
    trailing_assistant = [Message(role="user", content="q"), assistant]
    unlabeled_tool_round = trailing_assistant + [Message(role="tool", content="result")]
    unlabeled = ModelConfig("plain", "https://example.test", "plain")
    labeled = ModelConfig(
        "deepseek", "https://example.test", "deepseek",
        labels=["require-reasoning-for-tool-rounds"],
    )

    assert _prepare_reasoning_for_tool_rounds(unlabeled_tool_round, unlabeled) is unlabeled_tool_round
    assert _prepare_reasoning_for_tool_rounds(trailing_assistant, labeled) is trailing_assistant
    assert assistant.thinking is None


# --- Hypothesis strategies ---

# Strategy for non-empty text strings (plain text input)
text_input_st = st.text(min_size=1, max_size=200)

# Strategy for Message objects with various roles
message_st = st.builds(
    Message,
    role=st.sampled_from(["system", "user", "assistant", "tool"]),
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
    runtime = Runtime(model_registry=ModelRegistry(), tool_registry=ToolRegistry())
    request = InferenceRequest(model_id="test-model", text=text)
    result = runtime._normalize_messages(request)

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
    runtime = Runtime(model_registry=ModelRegistry(), tool_registry=ToolRegistry())
    request = InferenceRequest(model_id="test-model", messages=messages)
    result = runtime._normalize_messages(request)

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
    runtime = Runtime(model_registry=ModelRegistry(), tool_registry=ToolRegistry())
    request = InferenceRequest(
        model_id="test-model", text=text, messages=messages
    )
    result = runtime._normalize_messages(request)

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
    runtime = Runtime(model_registry=ModelRegistry(), tool_registry=ToolRegistry())
    # Text input path
    text_request = InferenceRequest(model_id="test-model", text=text)
    text_result = runtime._normalize_messages(text_request)

    # Equivalent messages input path
    equivalent_messages = [Message(role="user", content=text)]
    msg_request = InferenceRequest(
        model_id="test-model", messages=equivalent_messages
    )
    msg_result = runtime._normalize_messages(msg_request)

    # Both should produce the same normalized output
    assert len(text_result) == len(msg_result)
    assert text_result[0].role == msg_result[0].role
    assert text_result[0].content == msg_result[0].content


def test_normalize_empty_input_returns_empty_list() -> None:
    """When neither text nor messages is provided, should return empty list."""
    runtime = Runtime(model_registry=ModelRegistry(), tool_registry=ToolRegistry())
    request = InferenceRequest(model_id="test-model")
    result = runtime._normalize_messages(request)
    assert result == []


# Feature: agent-service, Property 12: 选择性工具启用
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


# Feature: agent-service, Property 15: 工具调用循环与最大轮次限制
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


def test_max_infer_per_minute_throttle_sleeps_after_ten_rounds() -> None:
    """MAX_INFER_PER_MINUTE is enforced dynamically once 10 tool rounds complete."""
    runtime = Runtime(model_registry=ModelRegistry(), tool_registry=ToolRegistry())
    current_time = [101.0]
    sleep_calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        current_time[0] += seconds

    with patch.dict("os.environ", {"MAX_INFER_PER_MINUTE": "120"}, clear=False), \
         patch("runtime.runtime.time.monotonic", side_effect=lambda: current_time[0]), \
         patch("runtime.runtime.time.sleep", side_effect=fake_sleep):
        should_continue = runtime._maybe_throttle_inference_loop(loop_start=100.0, infer_round=10)

    assert should_continue is True
    assert sleep_calls
    # 120/minute => at least 0.5s average. At round 10 target elapsed is 5s;
    # with only 1s elapsed initially, the helper should sleep about 4s total.
    assert abs(sum(sleep_calls) - 4.0) < 1e-6


def test_max_infer_per_minute_throttle_ignored_before_ten_rounds() -> None:
    """The rate limiter must not affect short inference loops (< 10 rounds)."""
    runtime = Runtime(model_registry=ModelRegistry(), tool_registry=ToolRegistry())

    with patch.dict("os.environ", {"MAX_INFER_PER_MINUTE": "1"}, clear=False), \
         patch("runtime.runtime.time.sleep") as sleep_mock:
        should_continue = runtime._maybe_throttle_inference_loop(loop_start=100.0, infer_round=9)

    assert should_continue is True
    sleep_mock.assert_not_called()


@given(max_rounds=st.integers(min_value=1, max_value=10))
@settings(max_examples=100, deadline=None)
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
    # 1 user message + N * (assistant + tool) + 1 final assistant(note) = 2N + 2
    assert result.messages is not None
    expected_msg_count = 2 * max_rounds + 2
    assert len(result.messages) == expected_msg_count, (
        f"Expected {expected_msg_count} messages, got {len(result.messages)}"
    )

    # First message should be the user message
    assert result.messages[0].role == "user"
    assert result.messages[0].content == "hello"

    # Last message should be a plain assistant note (NOT a fabricated tool
    # reply) explaining that the max rounds limit was reached.
    last_msg = result.messages[-1]
    assert last_msg.role == "assistant", (
        f"Expected final assistant note, got role={last_msg.role!r}"
    )
    assert "maximum tool-call rounds" in (last_msg.content or "")
    # The pending tool_calls must be stripped so the history has no dangling
    # tool_calls (OpenAI/Anthropic reject an assistant message whose tool_calls
    # are never followed by tool results).
    assert last_msg.tool_calls is None

    # Verify the pattern: user, (assistant, tool) * N, assistant(note)
    for i in range(max_rounds):
        assistant_idx = 1 + 2 * i
        function_idx = 2 + 2 * i
        assert result.messages[assistant_idx].role == "assistant"
        assert result.messages[assistant_idx].tool_calls is not None
        assert result.messages[function_idx].role == "tool"
        assert result.messages[function_idx].name == "dummy_tool"


def test_normalize_tool_call_order_moves_misplaced_tool_messages() -> None:
    """_normalize_tool_call_order must move a tool message that references a
    LATER assistant(tool_calls) declaration to right after that assistant."""
    from runtime.runtime import _normalize_tool_call_order

    assistant_tc = Message(
        role="assistant",
        content="",
        tool_calls=[{"id": "call_ET", "name": "exec_cli", "arguments": "{}"}],
    )
    tool_error = Message(
        role="tool",
        name="exec_cli",
        tool_use_id="call_ET",
        content="Error: maximum tool-call rounds (200) reached.",
    )
    user = Message(role="user", content="继续")

    # Correctly ordered history stays untouched.
    ordered = [assistant_tc, tool_error, user]
    assert _normalize_tool_call_order(ordered) is ordered

    # Flipped [tool, assistant] order is reordered to [assistant, tool].
    flipped = [tool_error, assistant_tc, user]
    normalized = _normalize_tool_call_order(flipped)
    assert [m.role for m in normalized] == ["assistant", "tool", "user"]
    assert normalized[0].tool_calls[0]["id"] == "call_ET"
    assert normalized[1].tool_use_id == "call_ET"

    # Idempotent: applying again changes nothing.
    assert _normalize_tool_call_order(normalized) == normalized


def test_normalize_tool_call_order_multiple_misplaced_tools() -> None:
    """Multiple tool messages for the same later assistant are all moved after it."""
    from runtime.runtime import _normalize_tool_call_order

    assistant_tc = Message(
        role="assistant",
        content="",
        tool_calls=[
            {"id": "call_A", "name": "t1", "arguments": "{}"},
            {"id": "call_B", "name": "t2", "arguments": "{}"},
        ],
    )
    tool_a = Message(role="tool", name="t1", tool_use_id="call_A", content="r1")
    tool_b = Message(role="tool", name="t2", tool_use_id="call_B", content="r2")
    user = Message(role="user", content="next")

    flipped = [tool_b, user, tool_a, assistant_tc]
    normalized = _normalize_tool_call_order(flipped)
    roles = [m.role for m in normalized]
    assert roles == ["user", "assistant", "tool", "tool"]
    # Relative order of the two tool messages is preserved (r2 was first in
    # the original list, r1 second).
    assert [m.content for m in normalized if m.role == "tool"] == ["r2", "r1"]


# Feature: agent-service, Property 8: 工具分发与执行
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

    # The conversation should contain a tool role message with the tool result
    function_messages = [
        m for m in result.messages if m.role == "tool"
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
    assert result.messages[2].role == "tool"
    assert result.messages[3].role == "assistant"
    assert result.messages[3].tool_calls is None


# Feature: agent-service, Property 9: 工具错误处理
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

    # Find the tool role message for the bad tool
    fn_msgs = [m for m in result.messages if m.role == "tool" and m.name == bad_tool_name]
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

    fn_msgs = [m for m in result.messages if m.role == "tool" and m.name == "failing_tool"]
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


# Feature: agent-service, Property 16: 工具实例复用
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


# ------------------------------------------------------------------
# Tool execution guard (TOOL_EXEC_TIMEOUT)
# ------------------------------------------------------------------

def test_function_tool_hang_returns_timeout_error(monkeypatch) -> None:
    """A hung function tool must not block the caller: _execute_function_tool
    runs the callable on a daemon worker and returns an error after
    TOOL_EXEC_TIMEOUT instead of hanging (group-chat workers included)."""
    import time

    from runtime.models import ToolConfig
    from runtime.registry import ToolRegistry

    monkeypatch.setenv("TOOL_EXEC_TIMEOUT", "0.2")

    registry = ToolRegistry()

    def hung_fn(x: str = "") -> str:
        time.sleep(60)  # simulate a permanently stuck tool
        return f"never_{x}"

    tool_config = ToolConfig(
        tool_id="hung_tool",
        tool_type="function",
        name="hung_tool",
        description="A tool that hangs",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    registry.register(tool_config, callable_fn=hung_fn)

    runtime = Runtime(model_registry=ModelRegistry(), tool_registry=registry)
    start = time.monotonic()
    result = runtime._execute_function_tool(tool_config, {"x": "hi"})
    elapsed = time.monotonic() - start

    assert "timed out" in result, result
    assert elapsed < 5, f"tool guard did not fire; took {elapsed:.1f}s"


def test_tool_exec_timeout_parsing(monkeypatch) -> None:
    """TOOL_EXEC_TIMEOUT parsing: default 600 s, invalid -> 600 s,
    empty / <= 0 -> guard disabled (None)."""
    from runtime.runtime import _get_tool_exec_timeout

    monkeypatch.delenv("TOOL_EXEC_TIMEOUT", raising=False)
    assert _get_tool_exec_timeout() == 600.0

    monkeypatch.setenv("TOOL_EXEC_TIMEOUT", "1.5")
    assert _get_tool_exec_timeout() == 1.5

    monkeypatch.setenv("TOOL_EXEC_TIMEOUT", "abc")
    assert _get_tool_exec_timeout() == 600.0

    monkeypatch.setenv("TOOL_EXEC_TIMEOUT", "")
    assert _get_tool_exec_timeout() is None

    monkeypatch.setenv("TOOL_EXEC_TIMEOUT", "0")
    assert _get_tool_exec_timeout() is None


def test_function_tool_normal_call_with_guard_enabled(monkeypatch) -> None:
    """With the guard enabled, a normal fast tool still returns its result."""
    import time

    from runtime.models import ToolConfig
    from runtime.registry import ToolRegistry

    monkeypatch.setenv("TOOL_EXEC_TIMEOUT", "30")

    registry = ToolRegistry()

    def fast_fn(x: str = "") -> str:
        return f"ok_{x}"

    tool_config = ToolConfig(
        tool_id="fast_tool",
        tool_type="function",
        name="fast_tool",
        description="Fast tool",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    registry.register(tool_config, callable_fn=fast_fn)

    runtime = Runtime(model_registry=ModelRegistry(), tool_registry=registry)
    start = time.monotonic()
    result = runtime._execute_function_tool(tool_config, {"x": "hi"})
    assert result == "ok_hi"
    assert time.monotonic() - start < 2


def test_function_tool_guard_disabled_runs_inline(monkeypatch) -> None:
    """TOOL_EXEC_TIMEOUT disabled -> callable runs inline (no worker)."""
    from runtime.models import ToolConfig
    from runtime.registry import ToolRegistry

    monkeypatch.setenv("TOOL_EXEC_TIMEOUT", "")

    registry = ToolRegistry()
    called: list = []

    def fast_fn(x: str = "") -> str:
        called.append(x)
        return f"ok_{x}"

    tool_config = ToolConfig(
        tool_id="fast_tool_inline",
        tool_type="function",
        name="fast_tool_inline",
        description="Fast tool",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    registry.register(tool_config, callable_fn=fast_fn)

    runtime = Runtime(model_registry=ModelRegistry(), tool_registry=registry)
    result = runtime._execute_function_tool(tool_config, {"x": "hi"})
    assert result == "ok_hi"
    assert called == ["hi"]


# ------------------------------------------------------------------
# 场景化回归测试：工具调用必须继承请求线程的会话上下文
# ------------------------------------------------------------------
# 背景：TOOL_EXEC_TIMEOUT guard 默认启用，function 工具会在独立 worker
# 线程执行。`threading.local` 是线程隔离的 —— worker 线程若没有重放请求线程
# 的会话上下文（session_id / session_dir / workspace），exec_cli 会拿不到
# session_id 从而每次新建 shell，write_file 的 journal 会退化为 stateless，
# exec_shell 会在错误的目录执行。这些测试按"真实请求"复刻 server.py 的处理
# 流程（先 set_request_context，再走 Runtime 执行工具），而不是只测函数内部
# 机制，因此能真正捕获此类回归。

import json
import os

import pytest

import runtime.builtin_tools as _bt
import runtime.server as _server


def _set_session_context(session_id: str, workspace: str, session_dir: str) -> None:
    """复刻 runtime/server.py 在处理推理请求前设置的请求上下文。"""
    from runtime.common import set_request_context

    set_request_context(
        session_id=session_id,
        session_dir=session_dir,
        workspace=workspace,
        user_message_timestamp="2025-06-01T12:00:00Z",
        depth=0,
        tool_scope=[],
    )


def _clear_session_context() -> None:
    from runtime.common import clear_request_context

    clear_request_context([
        "session_id", "session_dir", "workspace", "user_message_timestamp",
        "depth", "tool_scope",
    ])


@pytest.fixture
def session_ctx(tmp_path):
    """构造一个真实的会话上下文并保证测试后清理，返回 (workspace, session_dir)。"""
    workspace = str(tmp_path / "workspace")
    session_dir = str(tmp_path / "chat_data" / "sess-1")
    os.makedirs(workspace, exist_ok=True)
    os.makedirs(session_dir, exist_ok=True)
    _set_session_context("sess-1", workspace, session_dir)
    try:
        yield workspace, session_dir
    finally:
        _clear_session_context()


def test_exec_cli_reuses_persistent_terminal_across_calls(monkeypatch, session_ctx):
    """实际场景：同一会话内连续调用 exec_cli 必须复用同一个持久终端。

    复刻 server 请求上下文后，通过 Runtime 连续执行 exec_cli 两次。guard
    启用（worker 线程执行）时，若 worker 丢失 session_id，_exec_cli 会绕过
    get_or_create_terminal 走 subprocess 兜底 —— 每次都新建一个 shell
    （即本回归）。测试断言持久终端路径被命中且只创建一次（第二次复用）。
    """
    from runtime.builtin_tools import CLI_TOOL_CONFIG, _exec_cli
    from runtime.registry import ModelRegistry, ToolRegistry
    from runtime.runtime import Runtime

    workspace, _session_dir = session_ctx
    monkeypatch.setenv("TOOL_EXEC_TIMEOUT", "30")  # 强制走 worker 线程路径

    looked_up_sessions: list = []   # get_or_create_terminal 收到的 session_id
    created_sessions: list = []     # 首次创建的终端

    def fake_get_or_create_terminal(sid, cols=80, rows=24):
        looked_up_sessions.append(sid)
        if sid not in created_sessions:
            created_sessions.append(sid)
        return {"session_id": sid, "fake": True}

    def fake_execute_command(sid, command, timeout=300, **kwargs):
        looked_up_sessions.append(sid)
        return "fake-terminal-output"

    # 兜底 subprocess 路径绝不应被触发：worker 丢失 session_id 时会走到这里
    def boom_run(*args, **kwargs):
        raise AssertionError(
            "exec_cli 走了 subprocess 兜底：worker 线程丢失了会话上下文，"
            "持久终端未被复用")

    import runtime.builtin_tools_misc as _bt_misc

    monkeypatch.setattr(_server, "get_or_create_terminal", fake_get_or_create_terminal)
    monkeypatch.setattr(_bt_misc, "execute_command_in_terminal", fake_execute_command)
    monkeypatch.setattr(_bt.subprocess, "run", boom_run)

    registry = ToolRegistry()
    registry.register(CLI_TOOL_CONFIG, callable_fn=_exec_cli)
    runtime = Runtime(model_registry=ModelRegistry(), tool_registry=registry)

    for _ in range(2):
        result = runtime._execute_function_tool(
            CLI_TOOL_CONFIG, {"command": "echo hi"})
        assert result == "fake-terminal-output", result

    assert looked_up_sessions.count("sess-1") >= 2, (
        f"exec_cli 未通过 session_id 定位持久终端: {looked_up_sessions!r}")
    assert created_sessions == ["sess-1"], (
        f"持久终端未按会话复用，每次调用都新建: {created_sessions!r}")


def test_exec_shell_runs_in_session_workspace(monkeypatch, session_ctx, tmp_path):
    """实际场景：exec_shell 必须在会话 workspace 下执行。

    即使进程环境变量 AGENTS_WORKSPACE 指向别处，请求线程通过
    set_request_context 指定的 workspace 也必须传到 worker 线程，
    否则 exec_shell 会退化为在错误的目录执行。
    """
    from runtime.builtin_tools import EXEC_SHELL_TOOL_CONFIG, _exec_shell
    from runtime.registry import ModelRegistry, ToolRegistry
    from runtime.runtime import Runtime

    workspace, _session_dir = session_ctx
    monkeypatch.setenv("TOOL_EXEC_TIMEOUT", "30")
    # 让环境变量指向一个"错误"的工作目录，若 worker 丢失上下文就会走到这里
    monkeypatch.setenv("AGENTS_WORKSPACE", str(tmp_path / "wrong-env-ws"))

    spawned_cwd: list = []

    class FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return ("", "")

    def fake_popen(*args, **kwargs):
        spawned_cwd.append(kwargs.get("cwd"))
        return FakeProc()

    monkeypatch.setattr(_bt.subprocess, "Popen", fake_popen)

    registry = ToolRegistry()
    registry.register(EXEC_SHELL_TOOL_CONFIG, callable_fn=_exec_shell)
    runtime = Runtime(model_registry=ModelRegistry(), tool_registry=registry)

    result = runtime._execute_function_tool(
        EXEC_SHELL_TOOL_CONFIG, {"command": "pwd"})
    assert "error" not in json.loads(result), result

    assert spawned_cwd == [workspace], (
        f"exec_shell 未在会话 workspace 下执行: {spawned_cwd!r} (期望 {workspace!r})")


def test_write_file_journal_scoped_to_session(monkeypatch, session_ctx, tmp_path):
    """实际场景：write_file 的文件 journal 必须归属当前会话。

    worker 若丢失 session_dir，journal 会退化为 stateless/no_session_dir，
    undo 将无法按会话定位快照 —— 文件虽写入成功但失去了可恢复性。
    """
    from runtime.builtin_tools import WRITE_FILE_TOOL_CONFIG, _write_file
    from runtime.registry import ModelRegistry, ToolRegistry
    from runtime.runtime import Runtime

    workspace, _session_dir = session_ctx
    monkeypatch.setenv("TOOL_EXEC_TIMEOUT", "30")
    monkeypatch.setenv("DISABLE_FILE_JOURNAL", "false")
    # 防止退化为环境变量目录时污染真实 workspace / 仓库
    monkeypatch.setenv("AGENTS_WORKSPACE", str(tmp_path / "wrong-env-ws"))

    registry = ToolRegistry()
    registry.register(WRITE_FILE_TOOL_CONFIG, callable_fn=_write_file)
    runtime = Runtime(model_registry=ModelRegistry(), tool_registry=registry)

    result = runtime._execute_function_tool(
        WRITE_FILE_TOOL_CONFIG, {"path": "hello.txt", "content": "hi"})
    payload = json.loads(result)
    journal = payload.get("journal") or {}
    assert journal.get("skipped") is not True, (
        f"write_file journal 未归属会话（退化为 stateless）: {journal!r}")
    assert str(payload.get("journal_id", "")).startswith("sess-1/"), (
        f"journal 未绑定会话 session_id: {payload!r}")
