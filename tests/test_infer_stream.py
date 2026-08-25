"""Tests for Runtime.infer_stream() streaming inference method.

Verifies that infer_stream() correctly yields Message objects incrementally
for both OpenAI SSE and Ollama newline-delimited JSON streaming protocols.
"""

import datetime
import io
import json
from unittest.mock import patch, MagicMock

from runtime.models import (
    InferenceRequest,
    Message,
    ModelConfig,
    ToolConfig,
)
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_model_registry(protocol: str = "openai") -> ModelRegistry:
    """Create a ModelRegistry with a test model using the given protocol."""
    registry = ModelRegistry()
    registry.register(
        ModelConfig(
            model_id="test-model",
            api_base="http://localhost:9999",
            model_name="test",
            api_protocol=protocol,
        )
    )
    return registry


def _make_openai_sse_response(chunks: list[str]) -> io.BytesIO:
    """Build a fake OpenAI SSE response from a list of content strings.

    Each string becomes a separate SSE data line with a delta content chunk.
    A final 'data: [DONE]' line is appended.
    """
    lines = []
    for content in chunks:
        chunk_json = json.dumps(
            {
                "choices": [
                    {
                        "delta": {"role": "assistant", "content": content},
                    }
                ]
            }
        )
        lines.append(f"data: {chunk_json}\n\n")
    lines.append("data: [DONE]\n\n")
    return io.BytesIO("".join(lines).encode("utf-8"))


def _make_ollama_stream_response(chunks: list[str]) -> io.BytesIO:
    """Build a fake Ollama newline-delimited JSON stream from content strings.

    Each string becomes a separate JSON line. The last line has done=true.
    """
    lines = []
    for i, content in enumerate(chunks):
        is_last = i == len(chunks) - 1
        obj = {
            "model": "test",
            "message": {"role": "assistant", "content": content},
            "done": is_last,
        }
        lines.append(json.dumps(obj) + "\n")
    return io.BytesIO("".join(lines).encode("utf-8"))


def _mock_urlopen_with_stream(stream: io.BytesIO):
    """Create a mock urlopen that returns the given stream."""

    def mock_urlopen(request, **kwargs):
        mock_resp = MagicMock()
        mock_resp.__iter__ = lambda self: iter(stream.readlines())
        mock_resp.read = stream.read
        mock_resp.close = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    return mock_urlopen


# ---------------------------------------------------------------------------
# Tests: OpenAI streaming
# ---------------------------------------------------------------------------


def test_infer_stream_openai_yields_messages() -> None:
    """infer_stream with OpenAI protocol yields one Message per SSE delta chunk."""
    model_registry = _make_model_registry("openai")
    tool_registry = ToolRegistry()
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)

    chunks = ["Hello", " world", "!"]
    stream = _make_openai_sse_response(chunks)

    request = InferenceRequest(
        model_id="test-model",
        text="hi",
        stream=True,
    )

    with patch(
        "urllib.request.urlopen",
        side_effect=_mock_urlopen_with_stream(stream),
    ):
        messages = list(runtime.infer_stream(request))

    assert len(messages) == 4
    # First 3 are content chunks
    for msg, expected_content in zip(messages[:3], chunks):
        assert isinstance(msg, Message)
        assert msg.role == "assistant"
        assert msg.content == expected_content
    # 4th is the usage stat message
    assert messages[3].role == "usage"
    # Every inference round records both request and first-output wall times.
    stat = json.loads(messages[3].content)
    assert "request_started_at" in stat
    assert "first_token_timestamp" in stat
    request_started = datetime.datetime.fromisoformat(stat["request_started_at"])
    first_token = datetime.datetime.fromisoformat(stat["first_token_timestamp"])
    assert abs((first_token - request_started).total_seconds() * 1000 - stat["ttft_ms"]) < 0.2


def test_infer_stream_openai_empty_stream() -> None:
    """infer_stream with an empty OpenAI SSE stream yields no messages."""
    model_registry = _make_model_registry("openai")
    tool_registry = ToolRegistry()
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)

    # Only [DONE] marker, no content chunks
    stream = io.BytesIO(b"data: [DONE]\n\n")

    request = InferenceRequest(model_id="test-model", text="hi", stream=True)

    with patch(
        "urllib.request.urlopen",
        side_effect=_mock_urlopen_with_stream(stream),
    ):
        messages = list(runtime.infer_stream(request))

    assert len(messages) == 1
    # usage stat message
    assert messages[0].role == "usage"
    stat = json.loads(messages[0].content)
    # The request time exists even when the stream emits no model output.
    assert "request_started_at" in stat
    assert "first_token_timestamp" not in stat
    assert "ttft_ms" not in stat


# ---------------------------------------------------------------------------
# Tests: Ollama streaming
# ---------------------------------------------------------------------------


def test_infer_stream_ollama_yields_messages() -> None:
    """infer_stream with Ollama protocol yields one Message per JSON line."""
    model_registry = _make_model_registry("ollama")
    tool_registry = ToolRegistry()
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)

    chunks = ["Hello", " world", "!"]
    stream = _make_ollama_stream_response(chunks)

    request = InferenceRequest(
        model_id="test-model",
        text="hi",
        stream=True,
    )

    with patch(
        "urllib.request.urlopen",
        side_effect=_mock_urlopen_with_stream(stream),
    ):
        messages = list(runtime.infer_stream(request))

    assert len(messages) == 4
    # First 3 are content chunks
    for msg, expected_content in zip(messages[:3], chunks):
        assert isinstance(msg, Message)
        assert msg.role == "assistant"
        assert msg.content == expected_content
    # 4th is the usage stat message
    assert messages[3].role == "usage"
    stat = json.loads(messages[3].content)
    assert "first_token_timestamp" in stat


# ---------------------------------------------------------------------------
# Tests: Error handling
# ---------------------------------------------------------------------------


def test_infer_stream_model_not_found() -> None:
    """infer_stream yields an error Message when model_id is not in registry."""
    model_registry = ModelRegistry()
    tool_registry = ToolRegistry()
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)

    request = InferenceRequest(model_id="nonexistent", text="hi", stream=True)
    messages = list(runtime.infer_stream(request))

    assert len(messages) == 1
    assert "not found" in messages[0].content.lower()


def test_infer_stream_http_error() -> None:
    """infer_stream yields an error Message on HTTP errors."""
    import urllib.error

    model_registry = _make_model_registry("openai")
    tool_registry = ToolRegistry()
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)

    def mock_urlopen(request, **kwargs):
        raise urllib.error.HTTPError(
            url="http://localhost:9999/v1/chat/completions",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=io.BytesIO(b"server error"),
        )

    request = InferenceRequest(model_id="test-model", text="hi", stream=True)

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        messages = list(runtime.infer_stream(request))

    assert len(messages) == 1
    assert "500" in messages[0].content


def test_infer_stream_connection_error() -> None:
    """infer_stream yields an error Message on connection errors."""
    import urllib.error

    model_registry = _make_model_registry("openai")
    tool_registry = ToolRegistry()
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)

    def mock_urlopen(request, **kwargs):
        raise urllib.error.URLError("Connection refused")

    request = InferenceRequest(model_id="test-model", text="hi", stream=True)

    with patch.dict("os.environ", {"MODEL_API_RETRY_DELAY": "0"}), \
         patch("urllib.request.urlopen", side_effect=mock_urlopen):
        messages = list(runtime.infer_stream(request))

    assert len(messages) == 1
    assert "connection refused" in messages[0].content.lower()


def test_infer_stream_retries_502_before_output() -> None:
    """A transient gateway error is retried before any stream output."""
    import urllib.error

    runtime = Runtime(
        model_registry=_make_model_registry("openai"),
        tool_registry=ToolRegistry(),
    )
    success_stream = _make_openai_sse_response(["recovered"])
    calls = 0

    def mock_urlopen(request, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(
                url=request.full_url, code=502, msg="Bad Gateway",
                hdrs={}, fp=io.BytesIO(b"temporary gateway failure"),
            )
        return _mock_urlopen_with_stream(success_stream)(request, **kwargs)

    request = InferenceRequest(model_id="test-model", text="hi", stream=True)
    with patch.dict("os.environ", {
        "MODEL_API_MAX_RETRIES": "2", "MODEL_API_RETRY_DELAY": "0",
    }), patch("urllib.request.urlopen", side_effect=mock_urlopen):
        messages = list(runtime.infer_stream(request))

    assert calls == 2
    assert [m.content for m in messages if m.role == "assistant"] == ["recovered"]


def test_infer_stream_retries_read_timeout_before_first_output() -> None:
    """A connected stream that times out before its first item is reopened."""
    import socket

    runtime = Runtime(
        model_registry=_make_model_registry("openai"),
        tool_registry=ToolRegistry(),
    )
    calls = 0

    class TimeoutBeforeOutput:
        def __iter__(self):
            raise socket.timeout("timed out waiting for first token")

        def close(self):
            pass

    def mock_urlopen(request, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return TimeoutBeforeOutput()
        return _mock_urlopen_with_stream(
            _make_openai_sse_response(["second connection"])
        )(request, **kwargs)

    request = InferenceRequest(model_id="test-model", text="hi", stream=True)
    with patch.dict("os.environ", {
        "MODEL_API_MAX_RETRIES": "2", "MODEL_API_RETRY_DELAY": "0",
    }), patch("urllib.request.urlopen", side_effect=mock_urlopen):
        messages = list(runtime.infer_stream(request))

    assert calls == 2
    assert [m.content for m in messages if m.role == "assistant"] == ["second connection"]


def test_infer_stream_does_not_retry_after_first_output() -> None:
    """Once output is visible, a read failure is returned without replay."""
    import socket

    runtime = Runtime(
        model_registry=_make_model_registry("openai"),
        tool_registry=ToolRegistry(),
    )
    calls = 0
    first_chunk = json.dumps({"choices": [{"delta": {"content": "partial"}}]})

    class PartialThenTimeout:
        def __iter__(self):
            yield f"data: {first_chunk}\n\n".encode()
            raise socket.timeout("timed out mid-stream")

        def close(self):
            pass

    def mock_urlopen(request, **kwargs):
        nonlocal calls
        calls += 1
        return PartialThenTimeout()

    request = InferenceRequest(model_id="test-model", text="hi", stream=True)
    with patch.dict("os.environ", {
        "MODEL_API_MAX_RETRIES": "2", "MODEL_API_RETRY_DELAY": "0",
    }), patch("urllib.request.urlopen", side_effect=mock_urlopen):
        messages = list(runtime.infer_stream(request))

    assert calls == 1
    assert messages[0].content == "partial"
    assert "stream parse" in messages[1].content.lower()


def test_infer_stream_returns_iterator() -> None:
    """infer_stream returns an Iterator (generator), not a list."""
    import types

    model_registry = _make_model_registry("openai")
    tool_registry = ToolRegistry()
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)

    chunks = ["Hello"]
    stream = _make_openai_sse_response(chunks)

    request = InferenceRequest(model_id="test-model", text="hi", stream=True)

    with patch(
        "urllib.request.urlopen",
        side_effect=_mock_urlopen_with_stream(stream),
    ):
        result = runtime.infer_stream(request)
        assert isinstance(result, types.GeneratorType)
        # Consume to avoid resource warnings
        list(result)


def test_infer_stream_unsupported_protocol() -> None:
    """infer_stream yields an error Message for unsupported api_protocol."""
    model_registry = ModelRegistry()
    model_registry.register(
        ModelConfig(
            model_id="test-model",
            api_base="http://localhost:9999",
            model_name="test",
            api_protocol="unsupported_proto",
        )
    )
    tool_registry = ToolRegistry()
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)

    request = InferenceRequest(model_id="test-model", text="hi", stream=True)
    messages = list(runtime.infer_stream(request))

    assert len(messages) == 1
    assert "unsupported" in messages[0].content.lower()


def _make_openai_tool_calls_sse(tool_name: str, tool_use_id: str) -> io.BytesIO:
    """Build a fake OpenAI SSE stream containing a single tool_call delta."""
    chunk_json = json.dumps({
        "choices": [{
            "delta": {
                "role": "assistant",
                "tool_calls": [{
                    "index": 0,
                    "id": tool_use_id,
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }],
            }
        }]
    })
    lines = [f"data: {chunk_json}\n\n", "data: [DONE]\n\n"]
    return io.BytesIO("".join(lines).encode("utf-8"))


def test_infer_stream_max_rounds_yields_assistant_note_not_fake_tool() -> None:
    """When max_tool_rounds is reached, infer_stream must yield a plain assistant
    note (with the tool_calls_dropped marker) instead of a fabricated tool reply.

    Regression test: the old code fabricated a role='tool' error message and
    yielded it BEFORE the usage/stat message, which made merge_stream_messages
    persist [tool, assistant] in the wrong order and 400 on the next request.
    """
    model_registry = _make_model_registry("openai")
    tool_registry = ToolRegistry()

    def dummy_tool() -> str:
        return "tool_result"

    tool_registry.register(
        ToolConfig(
            tool_id="dummy_tool",
            tool_type="function",
            name="dummy_tool",
            description="A dummy tool",
            parameters={"type": "object", "properties": {}, "required": []},
        ),
        callable_fn=dummy_tool,
    )
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)

    request = InferenceRequest(
        model_id="test-model",
        tool_ids=["dummy_tool"],
        text="hi",
        stream=True,
        max_tool_rounds=1,
    )

    call_count = [0]

    def mock_urlopen(request, **kwargs):
        call_count[0] += 1
        stream = _make_openai_tool_calls_sse("dummy_tool", f"call_{call_count[0]}")
        mock_resp = MagicMock()
        mock_resp.__iter__ = lambda self: iter(stream.readlines())
        mock_resp.read = stream.read
        mock_resp.close = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        collected = list(runtime.infer_stream(request))

    # Round 1 executed the tool (tool result), round 2 hit the limit.
    assert call_count[0] == 2

    # Each assistant inference round has its own complete timing record. The
    # client can use the first round's first_token_timestamp as the loop output
    # start, and the last round's completed_at as the loop completion time.
    usage_stats = [json.loads(m.content) for m in collected if m.role == "usage"]
    assert len(usage_stats) == 2
    assert all("request_started_at" in s for s in usage_stats)
    assert all("first_token_timestamp" in s for s in usage_stats)
    assert all("ttft_ms" in s for s in usage_stats)
    assert all("completed_at" in s for s in usage_stats)
    assert "overall_ms" not in usage_stats[0]
    assert "overall_ms" in usage_stats[1]
    for stat in usage_stats:
        request_started = datetime.datetime.fromisoformat(stat["request_started_at"])
        first_token = datetime.datetime.fromisoformat(stat["first_token_timestamp"])
        assert abs((first_token - request_started).total_seconds() * 1000 - stat["ttft_ms"]) < 0.2

    # Only one real tool-role message (the executed result) — no fabricated
    # "Error: maximum tool-call rounds" tool reply.
    tool_msgs = [m for m in collected if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content == "tool_result"

    # The limit round ends with a plain assistant note carrying the marker.
    note_msgs = [m for m in collected if getattr(m, "tool_calls_dropped", False)]
    assert len(note_msgs) == 1
    assert "maximum tool-call rounds" in note_msgs[0].content
    assert note_msgs[0].role == "assistant"
