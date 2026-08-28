"""Regression tests for examples/example_stream_as_infer.py."""

import io
import json
from dataclasses import asdict
from unittest.mock import patch

from examples.example_stream_as_infer import infer_via_stream
from runtime.models import Message
from runtime.server_state import merge_stream_messages


class _FakeSseResponse:
    def __init__(self, body: bytes):
        self._stream = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def __iter__(self):
        return iter(self._stream.readlines())


def test_stream_result_matches_direct_merge_semantics() -> None:
    """SSE reconstruction must match the merge used by POST /v1/infer."""
    first_stat = {
        "prompt_tokens": 10,
        "completion_tokens": 3,
        "total_tokens": 13,
        "cached_input_tokens": 4,
        "new_token_cache": 2,
        "completed_at": "2026-01-01T00:00:03",
    }
    final_stat = {
        "prompt_tokens": 20,
        "completion_tokens": 5,
        "total_tokens": 25,
        "cached_input_tokens": 8,
        "new_token_cache": 0,
        "completed_at": "2026-01-01T00:00:06",
        "overall_ms": 6000,
    }
    raw_messages = [
        Message(
            role="assistant",
            thinking="想",
            timestamp="2026-01-01T00:00:01",
            agent_id="agent-1",
            name="Agent",
        ),
        Message(
            role="assistant",
            tool_calls=[
                {"_index": 0, "id": "call_", "name": "get_", "arguments": '{"x":'}
            ],
            timestamp="2026-01-01T00:00:02",
            agent_id="agent-1",
            name="Agent",
        ),
        Message(
            role="assistant",
            tool_calls=[
                {"_index": 0, "id": "1", "name": "data", "arguments": "1}"}
            ],
            timestamp="2026-01-01T00:00:02",
            agent_id="agent-1",
            name="Agent",
        ),
        Message(role="usage", name="round", content=json.dumps(first_stat)),
        Message(
            role="tool",
            content="ok",
            timestamp="2026-01-01T00:00:04",
            name="get_data",
            tool_id="tool-1",
            tool_use_id="call_1",
            agent_id="agent-1",
        ),
        Message(
            role="assistant",
            content="结果",
            timestamp="2026-01-01T00:00:05",
            agent_id="agent-1",
            name="Agent",
        ),
        Message(
            role="assistant",
            content="如下",
            timestamp="2026-01-01T00:00:05",
            agent_id="agent-1",
            name="Agent",
        ),
        Message(role="usage", name="round", content=json.dumps(final_stat)),
    ]

    frames = [
        'event: init\ndata: {"type":"init","session_id":"session-1","stream_seq":0}\n\n'
    ]
    for seq, message in enumerate(raw_messages, 1):
        if message.role == "usage":
            usage = json.loads(message.content)
            frames.append(
                f"id: {seq}\nevent: usage\ndata: "
                f"{json.dumps(usage, ensure_ascii=False)}\n\n"
            )
        else:
            frames.append(
                f"id: {seq}\ndata: "
                f"{json.dumps(message.to_dict(), ensure_ascii=False)}\n\n"
            )
    frames.append("data: [DONE]\n\n")
    response = _FakeSseResponse("".join(frames).encode("utf-8"))

    with patch("urllib.request.urlopen", return_value=response):
        actual = infer_via_stream("http://test", "test-model")

    turns, last_stat = merge_stream_messages(raw_messages)
    expected = {
        "success": True,
        "messages": [
            {key: value for key, value in asdict(turn).items() if value is not None}
            for turn in turns
        ],
        "stat": last_stat,
    }

    assert actual == expected
