"""Tests for Runtime.infer_stream() streaming inference method.

Verifies that infer_stream() correctly yields Message objects incrementally
for both OpenAI SSE and Ollama newline-delimited JSON streaming protocols.
"""

import datetime
import io
import json
import logging
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


def _make_openai_sse_response(
    chunks: list[str], *, field: str = "content",
) -> io.BytesIO:
    """Build a fake OpenAI SSE response from content or thinking strings.

    Each string becomes a separate SSE data line with a delta chunk. A final
    ``data: [DONE]`` line is appended. ``field`` may be ``content`` or an
    OpenAI-compatible reasoning field such as ``reasoning_content``.
    """
    lines = []
    for content in chunks:
        chunk_json = json.dumps(
            {
                "choices": [
                    {
                        "delta": {"role": "assistant", field: content},
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


def test_infer_stream_openai_empty_stream_is_error() -> None:
    """A 200/[DONE] response without model output is not a successful turn."""
    model_registry = _make_model_registry("openai")
    tool_registry = ToolRegistry()
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)

    # Only [DONE] marker, no content/thinking/tool-call chunks.
    stream = io.BytesIO(b"data: [DONE]\n\n")
    request = InferenceRequest(model_id="test-model", text="hi", stream=True)

    with patch.dict("os.environ", {"MODEL_API_MAX_RETRIES": "0"}), patch(
        "urllib.request.urlopen",
        side_effect=_mock_urlopen_with_stream(stream),
    ):
        messages = list(runtime.infer_stream(request))

    assert len(messages) == 1
    assert messages[0].role == "assistant"
    assert messages[0].content == "Error: model API returned an empty response."


def test_infer_stream_retries_empty_response_before_output() -> None:
    """A prematurely closed 200 stream is retried like other pre-output failures."""
    runtime = Runtime(
        model_registry=_make_model_registry("openai"),
        tool_registry=ToolRegistry(),
    )
    calls = 0

    def mock_urlopen(request, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _mock_urlopen_with_stream(
                io.BytesIO(b"data: [DONE]\n\n")
            )(request, **kwargs)
        return _mock_urlopen_with_stream(
            _make_openai_sse_response(["recovered"])
        )(request, **kwargs)

    request = InferenceRequest(model_id="test-model", text="hi", stream=True)
    with patch.dict("os.environ", {
        "MODEL_API_MAX_RETRIES": "2", "MODEL_API_RETRY_DELAY": "0",
    }), patch("urllib.request.urlopen", side_effect=mock_urlopen):
        messages = list(runtime.infer_stream(request))

    assert calls == 2
    assert [m.content for m in messages if m.role == "assistant"] == ["recovered"]


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


def test_model_infer_timeout_defaults_disabled(monkeypatch) -> None:
    """MODEL_INFER_TIMEOUT defaults to 0, which disables the per-round guard."""
    from runtime.runtime import _get_model_infer_timeout

    monkeypatch.delenv("MODEL_INFER_TIMEOUT", raising=False)
    assert _get_model_infer_timeout() is None


def test_model_infer_timeout_parses_seconds(monkeypatch) -> None:
    """MODEL_INFER_TIMEOUT accepts fractional seconds."""
    from runtime.runtime import _get_model_infer_timeout

    monkeypatch.setenv("MODEL_INFER_TIMEOUT", "15.5")
    assert _get_model_infer_timeout() == 15.5


def test_model_infer_timeout_zero_and_invalid_disable(monkeypatch) -> None:
    """Zero and invalid MODEL_INFER_TIMEOUT values disable the guard."""
    from runtime.runtime import _get_model_infer_timeout

    monkeypatch.setenv("MODEL_INFER_TIMEOUT", "0")
    assert _get_model_infer_timeout() is None

    monkeypatch.setenv("MODEL_INFER_TIMEOUT", "not-a-number")
    assert _get_model_infer_timeout() is None


def test_find_repetitive_output_tail_requires_three_occurrences() -> None:
    """Loop detection needs two earlier matches of the final 100 chars."""
    from runtime.runtime import _find_repetitive_output_tail

    # Only two occurrences total: the live tail plus one previous block.
    block = "A" * 100
    assert _find_repetitive_output_tail(block * 2) is None

    # Three occurrences total: the second-last match starts at index 0.
    repeated = _find_repetitive_output_tail(block * 3)
    assert repeated == block * 3

    # Different text should not be classified as a loop.
    assert _find_repetitive_output_tail(("A" * 99) + "B" + ("C" * 100)) is None


def test_infer_stream_model_infer_timeout_aborts_round(caplog) -> None:
    """A continuous stream longer than MODEL_INFER_TIMEOUT logs diagnostics."""
    model_registry = _make_model_registry("openai")
    tool_registry = ToolRegistry()
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)

    stream = _make_openai_sse_response(["Hello"])
    request = InferenceRequest(model_id="test-model", text="hi", stream=True)

    # monotonic is used for: overall_start, round_start, the round guard, and
    # finally the stream-end measurement after abort.
    with caplog.at_level(logging.INFO, logger="runtime.runtime"), patch.dict(
        "os.environ", {
            "MODEL_INFER_TIMEOUT": "10",
            "MODEL_API_MAX_RETRIES": "0",
        },
    ), patch(
        "runtime.runtime.time.monotonic",
        side_effect=[0.0, 1.0, 20.0, 30.0],
    ), patch(
        "urllib.request.urlopen",
        side_effect=_mock_urlopen_with_stream(stream),
    ):
        messages = list(runtime.infer_stream(request))

    assert len(messages) == 1
    assert messages[0].role == "assistant"
    assert "model inference timed out" in messages[0].content
    assert "limit: 10.0s" in messages[0].content
    timeout_logs = [
        record.message for record in caplog.records
        if "infer_stream inference timeout" in record.message
    ]
    assert len(timeout_logs) == 1
    assert "first_output=19.000s" in timeout_logs[0]
    assert "last_output=19.000s" in timeout_logs[0]
    assert "last_output_gap=0.000s" in timeout_logs[0]
    assert "chunks=1" in timeout_logs[0]
    assert "content_chars=5" in timeout_logs[0]
    assert "thinking_chars=0" in timeout_logs[0]
    assert "recent_10s_chars=5" in timeout_logs[0]
    assert "repetitive_content=False" in timeout_logs[0]
    assert "repetitive_thinking=False" in timeout_logs[0]
    tail_logs = [
        record.message for record in caplog.records
        if "infer_stream timeout output tail" in record.message
    ]
    assert len(tail_logs) == 2
    assert any(
        "channel=content" in message and "last_500_chars='Hello'" in message
        for message in tail_logs
    )
    assert any(
        "channel=thinking" in message and "last_500_chars=''" in message
        for message in tail_logs
    )


def test_infer_stream_does_not_scan_repetition_before_timeout(caplog) -> None:
    """Normal streaming does not run or log repetitive-output inspection."""
    runtime = Runtime(
        model_registry=_make_model_registry("openai"),
        tool_registry=ToolRegistry(),
    )
    content = ("A" * 100) * 3
    stream = _make_openai_sse_response([content])
    request = InferenceRequest(model_id="test-model", text="hi", stream=True)

    with caplog.at_level(logging.INFO, logger="runtime.runtime"), \
         patch("urllib.request.urlopen", side_effect=_mock_urlopen_with_stream(stream)):
        messages = list(runtime.infer_stream(request))

    assert [m.content for m in messages if m.role == "assistant"] == [content]
    assert not any(
        "repetitive output detected" in record.message
        or "timeout output tail" in record.message
        for record in caplog.records
    )


def test_infer_stream_timeout_inspects_content_and_thinking(caplog) -> None:
    """Timeout scans both channels and logs tails for non-repetitive output."""
    runtime = Runtime(
        model_registry=_make_model_registry("openai"),
        tool_registry=ToolRegistry(),
    )
    repeated_thinking = ("think-step-" * 10) * 3
    content = "".join(chr(0x4E00 + index) for index in range(600))
    lines = []
    for field, value in (
        ("reasoning_content", repeated_thinking),
        ("content", content),
    ):
        chunk = {"choices": [{"delta": {"role": "assistant", field: value}}]}
        lines.append(f"data: {json.dumps(chunk)}\n\n")
    lines.append("data: [DONE]\n\n")
    stream = io.BytesIO("".join(lines).encode("utf-8"))
    request = InferenceRequest(model_id="test-model", text="hi", stream=True)

    with caplog.at_level(logging.INFO, logger="runtime.runtime"), patch.dict(
        "os.environ", {"MODEL_INFER_TIMEOUT": "10", "MODEL_API_MAX_RETRIES": "0"},
    ), patch(
        "runtime.runtime.time.monotonic",
        side_effect=[0.0, 1.0, 2.0, 20.0, 30.0],
    ), patch(
        "urllib.request.urlopen", side_effect=_mock_urlopen_with_stream(stream),
    ):
        messages = list(runtime.infer_stream(request))

    assert messages[0].thinking == repeated_thinking
    assert "model inference timed out" in messages[-1].content
    loop_logs = [
        record.message for record in caplog.records
        if "repetitive output detected" in record.message
    ]
    assert len(loop_logs) == 1
    assert "channel=thinking" in loop_logs[0]
    tail_logs = [
        record.message for record in caplog.records
        if "infer_stream timeout output tail" in record.message
    ]
    assert len(tail_logs) == 1
    assert "channel=content" in tail_logs[0]
    assert f"last_500_chars={content[-500:]!r}" in tail_logs[0]


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
    assistant_msgs = [m for m in collected if m.role == "assistant"]
    assert len(usage_stats) == 2
    assert assistant_msgs
    # Every real model round emits at least one assistant delta carrying the
    # request-send time. Synthetic notes (such as max-rounds text) may not.
    assistant_starts = {m.started_at for m in assistant_msgs if m.started_at}
    assert {stat["request_started_at"] for stat in usage_stats}.issubset(
        assistant_starts
    )
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
