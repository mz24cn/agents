"""Bug Condition Exploration Tests — Parallel Tool Calls Silently Dropped.

**Property 1: Bug Condition** — Multiple Tool Calls Silently Dropped

These tests encode the EXPECTED (correct) behavior: when an LLM response
contains N > 1 tool_calls, the parser should extract ALL of them into
Message.tool_calls as a list of length N.

On UNFIXED code, these tests are EXPECTED TO FAIL because:
- Protocol parsers only extract tool_calls[0]
- Message has no tool_calls field (only function_call: Optional[dict])
- OpenAI stream parser concatenates all tool call names/args into one

Failure confirms the bug exists and provides counterexamples.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6**
"""

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from runtime.protocols import OpenAIProtocol, OllamaProtocol


# ---------------------------------------------------------------------------
# Hypothesis strategies for generating tool calls
# ---------------------------------------------------------------------------

# Tool names: valid identifiers (letters + digits + underscores, starts with letter)
_tool_name_st = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz"),
    min_size=2,
    max_size=15,
)

# Simple argument values
_arg_value_st = st.text(min_size=0, max_size=50)

# A single tool call dict: {"name": ..., "arguments": ...}
_tool_call_st = st.fixed_dictionaries({
    "name": _tool_name_st,
    "arguments": st.dictionaries(
        keys=st.text(
            alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz_"),
            min_size=1,
            max_size=10,
        ),
        values=_arg_value_st,
        min_size=0,
        max_size=3,
    ),
})

# Multiple tool calls (N > 1): the bug condition
_multi_tool_calls_st = st.lists(_tool_call_st, min_size=2, max_size=5)


# ---------------------------------------------------------------------------
# Helpers to construct protocol-specific response payloads
# ---------------------------------------------------------------------------

def _build_openai_non_stream_response(tool_calls: list[dict]) -> bytes:
    """Build an OpenAI Chat Completions JSON response with multiple tool_calls."""
    api_tool_calls = []
    for i, tc in enumerate(tool_calls):
        api_tool_calls.append({
            "id": f"call_{i}",
            "type": "function",
            "function": {
                "name": tc["name"],
                "arguments": json.dumps(tc["arguments"]),
            },
        })
    return json.dumps({
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": api_tool_calls,
            },
            "finish_reason": "tool_calls",
        }],
    }).encode("utf-8")


def _build_ollama_non_stream_response(tool_calls: list[dict]) -> bytes:
    """Build an Ollama /api/chat JSON response with multiple tool_calls."""
    api_tool_calls = []
    for tc in tool_calls:
        api_tool_calls.append({
            "function": {
                "name": tc["name"],
                "arguments": tc["arguments"],  # Ollama uses dict, not JSON string
            },
        })
    return json.dumps({
        "model": "test-model",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": api_tool_calls,
        },
        "done": True,
    }).encode("utf-8")


def _build_ollama_stream_response(tool_calls: list[dict]) -> bytes:
    """Build an Ollama streaming response (newline-delimited JSON).

    Ollama streaming puts tool_calls in the final chunk.
    """
    # A content chunk first
    chunks = [
        json.dumps({
            "model": "test-model",
            "message": {"role": "assistant", "content": ""},
            "done": False,
        }),
    ]
    # Final chunk with all tool_calls
    api_tool_calls = []
    for tc in tool_calls:
        api_tool_calls.append({
            "function": {
                "name": tc["name"],
                "arguments": tc["arguments"],
            },
        })
    chunks.append(json.dumps({
        "model": "test-model",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": api_tool_calls,
        },
        "done": True,
    }))
    return "\n".join(chunks).encode("utf-8")


def _build_openai_stream_response(tool_calls: list[dict]) -> bytes:
    """Build an OpenAI SSE streaming response with multiple tool_call deltas.

    Each tool call gets its own index in the delta, as per OpenAI spec.
    """
    lines = []
    # Initial role delta
    lines.append("data: " + json.dumps({
        "choices": [{"delta": {"role": "assistant"}}],
    }))
    # One delta per tool call, each with a distinct index
    for i, tc in enumerate(tool_calls):
        # First delta for this index: name
        lines.append("data: " + json.dumps({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": i,
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": "",
                        },
                    }],
                },
            }],
        }))
        # Second delta for this index: arguments
        lines.append("data: " + json.dumps({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": i,
                        "function": {
                            "arguments": json.dumps(tc["arguments"]),
                        },
                    }],
                },
            }],
        }))
    lines.append("data: [DONE]")
    return "\n".join(lines).encode("utf-8")


# ---------------------------------------------------------------------------
# Bug Condition Exploration Tests
# ---------------------------------------------------------------------------

# **Validates: Requirements 1.1**
@given(tool_calls=_multi_tool_calls_st)
@settings(max_examples=50)
def test_openai_non_stream_extracts_all_tool_calls(tool_calls: list[dict]) -> None:
    """OpenAI non-stream: when response has N>1 tool_calls, the parsed Message
    should have a tool_calls list of length N.

    On UNFIXED code this FAILS because:
    - Message has no tool_calls attribute
    - Only tool_calls[0] is extracted into function_call
    """
    response_data = _build_openai_non_stream_response(tool_calls)
    proto = OpenAIProtocol()
    messages = proto._parse_non_stream_response(response_data)

    assert len(messages) == 1
    msg = messages[0]

    # The Message MUST have a tool_calls field with ALL tool calls
    assert hasattr(msg, "tool_calls"), (
        "Message has no 'tool_calls' attribute — data model cannot hold multiple tool calls"
    )
    assert msg.tool_calls is not None, (
        f"tool_calls is None — expected {len(tool_calls)} tool calls, got None"
    )
    assert len(msg.tool_calls) == len(tool_calls), (
        f"Expected {len(tool_calls)} tool_calls, got {len(msg.tool_calls)}"
    )

    # Verify each tool call matches
    for i, (actual, expected) in enumerate(zip(msg.tool_calls, tool_calls)):
        assert actual["name"] == expected["name"], (
            f"tool_calls[{i}].name: expected '{expected['name']}', got '{actual['name']}'"
        )


# **Validates: Requirements 1.2**
@given(tool_calls=_multi_tool_calls_st)
@settings(max_examples=50)
def test_ollama_non_stream_extracts_all_tool_calls(tool_calls: list[dict]) -> None:
    """Ollama non-stream: when response has N>1 tool_calls, the parsed Message
    should have a tool_calls list of length N.

    On UNFIXED code this FAILS because:
    - Message has no tool_calls attribute
    - Only tool_calls[0] is extracted into function_call
    """
    response_data = _build_ollama_non_stream_response(tool_calls)
    proto = OllamaProtocol()
    messages = proto._parse_non_stream_response(response_data)

    assert len(messages) == 1
    msg = messages[0]

    assert hasattr(msg, "tool_calls"), (
        "Message has no 'tool_calls' attribute — data model cannot hold multiple tool calls"
    )
    assert msg.tool_calls is not None, (
        f"tool_calls is None — expected {len(tool_calls)} tool calls, got None"
    )
    assert len(msg.tool_calls) == len(tool_calls), (
        f"Expected {len(tool_calls)} tool_calls, got {len(msg.tool_calls)}"
    )

    for i, (actual, expected) in enumerate(zip(msg.tool_calls, tool_calls)):
        assert actual["name"] == expected["name"], (
            f"tool_calls[{i}].name: expected '{expected['name']}', got '{actual['name']}'"
        )


# **Validates: Requirements 1.3**
@given(tool_calls=_multi_tool_calls_st)
@settings(max_examples=50)
def test_ollama_stream_extracts_all_tool_calls(tool_calls: list[dict]) -> None:
    """Ollama stream: when the final chunk has N>1 tool_calls, the parsed Message
    should have a tool_calls list of length N.

    On UNFIXED code this FAILS because:
    - Message has no tool_calls attribute
    - Only tool_calls[0] is extracted into function_call
    """
    response_data = _build_ollama_stream_response(tool_calls)
    proto = OllamaProtocol()
    messages = proto._parse_stream_response(response_data)

    assert len(messages) == 1
    msg = messages[0]

    assert hasattr(msg, "tool_calls"), (
        "Message has no 'tool_calls' attribute — data model cannot hold multiple tool calls"
    )
    assert msg.tool_calls is not None, (
        f"tool_calls is None — expected {len(tool_calls)} tool calls, got None"
    )
    assert len(msg.tool_calls) == len(tool_calls), (
        f"Expected {len(tool_calls)} tool_calls, got {len(msg.tool_calls)}"
    )

    for i, (actual, expected) in enumerate(zip(msg.tool_calls, tool_calls)):
        assert actual["name"] == expected["name"], (
            f"tool_calls[{i}].name: expected '{expected['name']}', got '{actual['name']}'"
        )


# **Validates: Requirements 1.4, 1.6**
@given(tool_calls=_multi_tool_calls_st)
@settings(max_examples=50)
def test_openai_stream_extracts_all_tool_calls(tool_calls: list[dict]) -> None:
    """OpenAI stream: when SSE response has N>1 tool_call deltas (each with
    distinct index), the parsed Message should have a tool_calls list of length N.

    On UNFIXED code this FAILS because:
    - All tool call names are concatenated into a single accumulated_fn_name
    - All arguments are concatenated into a single accumulated_fn_args
    - Result is one malformed function_call instead of N distinct tool calls
    """
    response_data = _build_openai_stream_response(tool_calls)
    proto = OpenAIProtocol()
    messages = proto._parse_stream_response(response_data)

    assert len(messages) == 1
    msg = messages[0]

    assert hasattr(msg, "tool_calls"), (
        "Message has no 'tool_calls' attribute — data model cannot hold multiple tool calls"
    )
    assert msg.tool_calls is not None, (
        f"tool_calls is None — expected {len(tool_calls)} tool calls, got None"
    )
    assert len(msg.tool_calls) == len(tool_calls), (
        f"Expected {len(tool_calls)} tool_calls, got {len(msg.tool_calls)}"
    )

    for i, (actual, expected) in enumerate(zip(msg.tool_calls, tool_calls)):
        assert actual["name"] == expected["name"], (
            f"tool_calls[{i}].name: expected '{expected['name']}', got '{actual['name']}'"
        )
