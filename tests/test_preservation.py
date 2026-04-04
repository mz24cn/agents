"""Preservation Property Tests — Single and No Tool Call Behavior Unchanged.

**Property 2: Preservation** — Single and No Tool Call Behavior Unchanged

These tests capture the EXISTING correct behavior on UNFIXED code for scenarios
where the bug condition does NOT hold (len(tool_calls) <= 1). They must PASS
on the current unfixed code to establish a baseline, and continue to PASS
after the fix is applied (no regressions).

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**
"""

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from runtime.models import Message
from runtime.protocols import OpenAIProtocol, OllamaProtocol


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Tool names: valid identifiers
_tool_name_st = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz"),
    min_size=2,
    max_size=15,
)

# Simple argument values
_arg_value_st = st.text(min_size=0, max_size=50)

# Arguments dict
_arguments_st = st.dictionaries(
    keys=st.text(
        alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz_"),
        min_size=1,
        max_size=10,
    ),
    values=_arg_value_st,
    min_size=0,
    max_size=3,
)

# Assistant content (may be empty for tool call responses)
_content_st = st.text(min_size=0, max_size=200)

# Non-empty content for no-tool-call responses
_nonempty_content_st = st.text(min_size=1, max_size=200)


# ---------------------------------------------------------------------------
# Helpers to construct protocol-specific response payloads
# ---------------------------------------------------------------------------

def _build_openai_single_tool_call_response(
    name: str, arguments: dict, content: str = ""
) -> bytes:
    """Build an OpenAI response with exactly 1 tool_call."""
    return json.dumps({
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": [{
                    "id": "call_0",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }).encode("utf-8")


def _build_openai_no_tool_call_response(content: str) -> bytes:
    """Build an OpenAI response with 0 tool_calls (plain text)."""
    return json.dumps({
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
            },
            "finish_reason": "stop",
        }],
    }).encode("utf-8")


def _build_ollama_single_tool_call_response(
    name: str, arguments: dict, content: str = ""
) -> bytes:
    """Build an Ollama response with exactly 1 tool_call."""
    return json.dumps({
        "model": "test-model",
        "message": {
            "role": "assistant",
            "content": content,
            "tool_calls": [{
                "function": {
                    "name": name,
                    "arguments": arguments,  # Ollama uses dict, not JSON string
                },
            }],
        },
        "done": True,
    }).encode("utf-8")


def _build_ollama_no_tool_call_response(content: str) -> bytes:
    """Build an Ollama response with 0 tool_calls (plain text)."""
    return json.dumps({
        "model": "test-model",
        "message": {
            "role": "assistant",
            "content": content,
        },
        "done": True,
    }).encode("utf-8")


# ---------------------------------------------------------------------------
# PBT 1: OpenAI single tool_call → tool_calls with correct name/arguments
# ---------------------------------------------------------------------------

# **Validates: Requirements 3.1**
@given(name=_tool_name_st, arguments=_arguments_st, content=_content_st)
@settings(max_examples=100)
def test_openai_single_tool_call_preserves_tool_calls(
    name: str, arguments: dict, content: str
) -> None:
    """For any OpenAI response with exactly 1 tool_call, the parsed Message
    has tool_calls with the correct name and arguments (JSON string)."""
    response_data = _build_openai_single_tool_call_response(name, arguments, content)
    proto = OpenAIProtocol()
    messages = proto._parse_non_stream_response(response_data)

    assert len(messages) == 1
    msg = messages[0]
    assert msg.role == "assistant"

    # tool_calls must be set
    assert msg.tool_calls is not None, (
        "tool_calls should not be None for a single tool_call response"
    )
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0]["name"] == name, (
        f"Expected tool_calls[0] name '{name}', got '{msg.tool_calls[0]['name']}'"
    )
    # OpenAI protocol stores arguments as a JSON string
    parsed_args = json.loads(msg.tool_calls[0]["arguments"])
    assert parsed_args == arguments, (
        f"Expected arguments {arguments}, got {parsed_args}"
    )


# ---------------------------------------------------------------------------
# PBT 2: OpenAI 0 tool_calls → tool_calls=None, content preserved
# ---------------------------------------------------------------------------

# **Validates: Requirements 3.2**
@given(content=_nonempty_content_st)
@settings(max_examples=100)
def test_openai_no_tool_call_preserves_content(content: str) -> None:
    """For any OpenAI response with 0 tool_calls, the parsed Message has
    tool_calls=None and content is preserved."""
    response_data = _build_openai_no_tool_call_response(content)
    proto = OpenAIProtocol()
    messages = proto._parse_non_stream_response(response_data)

    assert len(messages) == 1
    msg = messages[0]
    assert msg.role == "assistant"
    assert msg.tool_calls is None, (
        f"tool_calls should be None for no-tool-call response, got {msg.tool_calls}"
    )
    assert msg.content == content, (
        f"Expected content '{content}', got '{msg.content}'"
    )


# ---------------------------------------------------------------------------
# PBT 3: Ollama single tool_call → tool_calls with correct name/arguments
# ---------------------------------------------------------------------------

# **Validates: Requirements 3.1**
@given(name=_tool_name_st, arguments=_arguments_st, content=_content_st)
@settings(max_examples=100)
def test_ollama_single_tool_call_preserves_tool_calls(
    name: str, arguments: dict, content: str
) -> None:
    """For any Ollama response with exactly 1 tool_call, the parsed Message
    has tool_calls with the correct name and arguments (JSON string of dict)."""
    response_data = _build_ollama_single_tool_call_response(name, arguments, content)
    proto = OllamaProtocol()
    messages = proto._parse_non_stream_response(response_data)

    assert len(messages) == 1
    msg = messages[0]
    assert msg.role == "assistant"

    # tool_calls must be set
    assert msg.tool_calls is not None, (
        "tool_calls should not be None for a single tool_call response"
    )
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0]["name"] == name, (
        f"Expected tool_calls[0] name '{name}', got '{msg.tool_calls[0]['name']}'"
    )
    # Ollama protocol converts arguments dict to JSON string
    parsed_args = json.loads(msg.tool_calls[0]["arguments"])
    assert parsed_args == arguments, (
        f"Expected arguments {arguments}, got {parsed_args}"
    )


# ---------------------------------------------------------------------------
# PBT 4: Ollama 0 tool_calls → tool_calls=None, content preserved
# ---------------------------------------------------------------------------

# **Validates: Requirements 3.2**
@given(content=_nonempty_content_st)
@settings(max_examples=100)
def test_ollama_no_tool_call_preserves_content(content: str) -> None:
    """For any Ollama response with 0 tool_calls, the parsed Message has
    tool_calls=None and content is preserved."""
    response_data = _build_ollama_no_tool_call_response(content)
    proto = OllamaProtocol()
    messages = proto._parse_non_stream_response(response_data)

    assert len(messages) == 1
    msg = messages[0]
    assert msg.role == "assistant"
    assert msg.tool_calls is None, (
        f"tool_calls should be None for no-tool-call response, got {msg.tool_calls}"
    )
    assert msg.content == content, (
        f"Expected content '{content}', got '{msg.content}'"
    )


# ---------------------------------------------------------------------------
# PBT 5: Message with single tool_calls round-trips via to_dict/from_dict
# ---------------------------------------------------------------------------

# **Validates: Requirements 3.4**
@given(
    content=_content_st,
    name=_tool_name_st,
    arguments=_arguments_st,
)
@settings(max_examples=100)
def test_message_single_tool_calls_round_trip(
    content: str, name: str, arguments: dict
) -> None:
    """For any Message with a single tool_calls entry, from_dict(to_dict(msg))
    preserves all fields including tool_calls name and arguments."""
    original = Message(
        role="assistant",
        content=content,
        tool_calls=[{
            "name": name,
            "arguments": json.dumps(arguments),
        }],
    )

    serialized = original.to_dict()
    restored = Message.from_dict(serialized)

    assert restored.role == original.role
    assert restored.content == original.content
    assert restored.tool_calls is not None
    assert len(restored.tool_calls) == 1
    assert restored.tool_calls[0]["name"] == name
    assert restored.tool_calls[0]["arguments"] == json.dumps(arguments)
    assert restored.name == original.name
    assert restored.images == original.images
    assert restored.audio == original.audio
    assert restored.thinking == original.thinking


# ---------------------------------------------------------------------------
# PBT 6: max_tool_rounds enforcement
# ---------------------------------------------------------------------------
# NOTE: max_tool_rounds enforcement is already covered by the existing
# test_tool_call_loop_terminates_at_max_rounds in tests/test_runtime.py.
# That test uses Hypothesis to verify for any max_rounds in [1, 10],
# the Runtime stops after exactly max_rounds tool executions.
# No additional test needed here — just noting this for completeness.
