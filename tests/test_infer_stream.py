"""Tests for Runtime.infer_stream() streaming inference method.

Verifies that infer_stream() correctly yields Message objects incrementally
for both OpenAI SSE and Ollama newline-delimited JSON streaming protocols.
"""

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

    assert len(messages) == 3
    for msg, expected_content in zip(messages, chunks):
        assert isinstance(msg, Message)
        assert msg.role == "assistant"
        assert msg.content == expected_content


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

    assert len(messages) == 0


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

    assert len(messages) == 3
    for msg, expected_content in zip(messages, chunks):
        assert isinstance(msg, Message)
        assert msg.role == "assistant"
        assert msg.content == expected_content


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

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        messages = list(runtime.infer_stream(request))

    assert len(messages) == 1
    assert "connection refused" in messages[0].content.lower()


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
