"""Tests for the mid-loop context compression trigger.

The session-completed trigger (``MAX_TOKENS_IN_CONTEXT``) only runs when an
inference finishes, but a single inference can run hundreds of tool rounds while
the whole message list is re-sent on every round.  ``Runtime.infer_stream`` now
calls an optional ``on_round_complete`` hook after every completed tool round;
:class:`InLoopContextCompressor` compresses the session once that round's input +
output tokens reach 90 % of the running model's ``ModelConfig.max_context``, and
rebuilds the in-flight message list exactly like a new turn is assembled (system
prompt + rolling summary + memory + recent verbatim window).

Covers:
  - ``ModelConfig.max_context`` default (0) and JSON round-trip
  - ``compress_context_in_loop`` threshold behaviour (90 % of max_context; no
    LLM call below it, no check at all when max_context is 0/unknown)
  - the in-loop trigger fires even when ``MAX_TOKENS_IN_CONTEXT`` is larger
  - failure backoff still applies to in-loop compression
  - ``InLoopContextCompressor`` rebuild keeps the pending tool round verbatim
  - the rebuild is skipped while the stream is not fully persisted
  - ``IncrementalConversationPersister.is_current`` semantics
  - ``Runtime.infer_stream`` invokes the hook after a tool round, continues with
    the returned messages, passes the model's max_context on, and survives
    None / empty / raising hooks
"""

from __future__ import annotations

import io
import json
import logging
import os
import tempfile
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import pytest

from runtime.context_manager import ContextManager, ConversationTurn
from runtime.models import InferenceRequest, Message, ModelConfig, ToolConfig
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server_state import (
    IncrementalConversationPersister,
    InLoopContextCompressor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cm(
    tmp_dir: str,
    *,
    infer_fn: Callable | None = None,
    recent_turns_k: int = 2,
    max_tokens_in_context: int = 1000,
    summary_model_id: str | None = "test-model",
) -> ContextManager:
    if infer_fn is None:
        infer_fn = lambda req: SimpleNamespace(  # noqa: E731
            content="<summary>\nsummary text\n</summary>\n<memory>\n[]\n</memory>"
        )
    return ContextManager(
        infer_fn=infer_fn,
        chats_dir=tmp_dir,
        recent_turns_k=recent_turns_k,
        summary_model_id=summary_model_id,
        max_tokens_in_context=max_tokens_in_context,
    )


def _make_turn(
    role: str = "user",
    content: str = "hello",
    ts: str = "2026-01-01T00:00:00",
) -> ConversationTurn:
    return ConversationTurn(role=role, content=content, timestamp=ts)


def _capturing_infer(store: dict) -> Callable:
    """infer_fn that records every prompt and returns a valid tagged output."""

    def infer(req: Any) -> SimpleNamespace:
        store["calls"] = store.get("calls", 0) + 1
        store.setdefault("prompts", []).append(req.messages[0].content)
        return SimpleNamespace(
            content="<summary>\nsummary text\n</summary>\n<memory>\n[]\n</memory>"
        )

    return infer


def _memory_output(summary: str, entries: list[dict]) -> SimpleNamespace:
    """Build a compress_context LLM response carrying memory entries."""
    return SimpleNamespace(
        content=(
            f"<summary>\n{summary}\n</summary>\n"
            f"<memory>\n{json.dumps(entries, ensure_ascii=False)}\n</memory>"
        )
    )


def _pending_tool_round_turns() -> list[ConversationTurn]:
    """Turns of a session that is in the middle of a tool round.

    The last two turns form the pending round (assistant tool call + its result)
    and must survive the rebuild verbatim; the first two are old enough to be
    summarized (``recent_turns_k=2``).
    """
    return [
        _make_turn(content="OLDER-USER-TURN"),
        _make_turn(role="assistant", content="OLDER-ASSISTANT-TURN", ts="2026-01-01T00:00:01"),
        ConversationTurn(
            role="assistant",
            content="",
            timestamp="2026-01-01T00:00:02",
            tool_calls=[{"id": "call_1", "name": "dummy_tool", "arguments": "{}"}],
        ),
        ConversationTurn(
            role="tool",
            content="PENDING-TOOL-RESULT",
            timestamp="2026-01-01T00:00:03",
            name="dummy_tool",
            tool_use_id="call_1",
        ),
    ]


# ---------------------------------------------------------------------------
# ModelConfig.max_context
# ---------------------------------------------------------------------------


def test_model_config_max_context_defaults_to_zero() -> None:
    config = ModelConfig(model_id="m", api_base="http://x", model_name="n")
    assert config.max_context == 0


def _max_context_from(value: Any) -> int:
    """Deserialize a ModelConfig carrying *value* as its max_context."""
    return ModelConfig.from_dict(
        {
            "model_id": "m",
            "api_base": "http://x",
            "model_name": "n",
            "max_context": value,
        }
    ).max_context


def test_model_config_max_context_round_trips_through_json() -> None:
    config = ModelConfig(
        model_id="m", api_base="http://x", model_name="n", max_context=1048576
    )
    assert config.to_dict()["max_context"] == 1048576
    assert ModelConfig.from_dict(config.to_dict()).max_context == 1048576
    # Strings (hand edited models.json / older clients) are accepted, including
    # the same K/M notation the Setup form displays.
    assert _max_context_from("131072") == 131072
    assert _max_context_from("1M") == 1048576
    assert _max_context_from("1.5M") == 1572864
    assert _max_context_from("384K") == 393216
    assert _max_context_from(1048576.0) == 1048576
    # Absent / malformed / non-positive values all mean "unknown" (0).
    assert _max_context_from(None) == 0
    assert _max_context_from(0) == 0
    assert _max_context_from(-5) == 0
    assert _max_context_from("not-a-number") == 0
    assert _max_context_from("nan") == 0
    assert _max_context_from(True) == 0


# ---------------------------------------------------------------------------
# compress_context_in_loop
# ---------------------------------------------------------------------------


def test_in_loop_check_is_disabled_without_a_known_context_window() -> None:
    """max_context=0 means the window is unknown: no check, no LLM call."""
    store: dict = {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=_capturing_infer(store))
        sid = cm.create_session()
        turns = [_make_turn(content=f"turn {i}") for i in range(4)]

        assert cm.compress_context_in_loop(sid, turns, 5_000_000, 0) is False
        assert cm.compress_context_in_loop(sid, turns, 5_000_000, None) is False
        assert cm.compress_context_in_loop(sid, turns, 5_000_000, -1) is False
        assert store.get("calls", 0) == 0
        assert cm.get_summary(sid)[0] == ""


def test_in_loop_below_threshold_sends_nothing_to_the_model() -> None:
    """The threshold is 90 % of max_context; 900 tokens of 1000 is still fine."""
    store: dict = {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=_capturing_infer(store))
        sid = cm.create_session()
        turns = [_make_turn(content=f"turn {i}") for i in range(4)]

        assert cm.compress_context_in_loop(sid, turns, 900, 1000) is False
        assert store.get("calls", 0) == 0
        assert cm.get_summary(sid)[0] == ""


def test_in_loop_above_threshold_compresses_incrementally() -> None:
    store: dict = {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=_capturing_infer(store))
        sid = cm.create_session()
        turns = [_make_turn(content=f"turn {i}") for i in range(4)]

        assert cm.compress_context_in_loop(sid, turns, 901, 1000) is True
        assert store["calls"] == 1
        text, front_matter = cm.get_summary(sid)
        assert "summary text" in text
        assert front_matter.get("summary_version") == 1
        # The most recent K turns stay unsummarized so the pending round survives.
        assert front_matter.get("summarized_up_to_turn") == len(turns) - 2 - 1


def test_in_loop_threshold_scales_with_the_model_window() -> None:
    store: dict = {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=_capturing_infer(store))
        sid = cm.create_session()
        turns = [_make_turn(content=f"turn {i}") for i in range(4)]

        # 1M-token window: 9001 tokens is nowhere near 90 % (921600).
        assert cm.compress_context_in_loop(sid, turns, 9001, 1_048_576) is False
        assert store.get("calls", 0) == 0
        assert cm.compress_context_in_loop(sid, turns, 943_719, 1_048_576) is True
        assert store["calls"] == 1


def test_in_loop_trigger_is_not_suppressed_by_a_larger_context_threshold() -> None:
    store: dict = {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(
            tmp_dir,
            infer_fn=_capturing_infer(store),
            max_tokens_in_context=10**9,
        )
        sid = cm.create_session()
        turns = [_make_turn(content=f"turn {i}") for i in range(4)]

        # Far above the in-loop threshold but below MAX_TOKENS_IN_CONTEXT: the
        # in-loop trigger must still compress.
        assert cm.compress_context_in_loop(sid, turns, 5000, 1000) is True
        assert store["calls"] == 1


def test_in_loop_compression_respects_failure_backoff() -> None:
    """A repeatedly failing compression is not re-sent on every round."""
    store: dict = {}

    def failing_infer(req: Any) -> SimpleNamespace:
        store["calls"] = store.get("calls", 0) + 1
        return SimpleNamespace(
            success=False, messages=list(req.messages), error="HTTP 500", error_code="500"
        )

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=failing_infer)
        sid = cm.create_session()
        turns = [_make_turn(content=f"turn {i}") for i in range(4)]

        for _ in range(3):
            cm.compress_context_in_loop(sid, turns, 5000, 1000)
        assert store["calls"] == 3

        # Threshold still exceeded, but the session is in failure backoff: the
        # caller may rebuild (the summary is unchanged), yet no new LLM call is
        # made until the token count grows by 25% or the window expires.
        assert cm.compress_context_in_loop(sid, turns, 5000, 1000) is True
        assert store["calls"] == 3
        assert cm.get_summary(sid)[0] == ""


def test_in_loop_threshold_none_tokens_is_a_noop() -> None:
    store: dict = {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=_capturing_infer(store))
        sid = cm.create_session()
        turns = [_make_turn(content=f"turn {i}") for i in range(4)]

        assert cm.compress_context_in_loop(sid, turns, None, 1000) is False
        assert store.get("calls", 0) == 0


# ---------------------------------------------------------------------------
# InLoopContextCompressor
# ---------------------------------------------------------------------------


def test_in_loop_compressor_rebuilds_like_a_new_turn() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        sid = cm.create_session()
        turns = _pending_tool_round_turns()
        cm.save_conversation(sid, turns)

        compressor = InLoopContextCompressor(context_manager=cm, session_id=sid)
        rebuilt = compressor([], 5000, max_context=1000)

        assert rebuilt is not None
        # 0. The window (last K messages) is a pure assistant/tool tail, so the
        #    context must be anchored to the last real user turn — providers
        #    such as Ollama reject requests without any user message.
        assert [m.role for m in rebuilt] == ["system", "user", "assistant", "tool"]
        assert rebuilt[1].content == "OLDER-USER-TURN"
        # 1. Merged system message: rolling summary (+ memory) and the session
        #    system prompt come from the same code path as a new turn.
        assert "## Summary" in rebuilt[0].content
        assert "summary text" in rebuilt[0].content
        # 2. The pending tool round is kept verbatim, so the next round still
        #    sees the assistant tool call and its result.
        assert rebuilt[2].tool_calls
        assert rebuilt[2].tool_calls[0]["id"] == "call_1"
        assert rebuilt[3].content == "PENDING-TOOL-RESULT"
        assert rebuilt[3].tool_use_id == "call_1"
        # 3. Summarized (non-anchor) turns are gone from the verbatim window.
        joined = "\n".join((m.content or "") for m in rebuilt)
        assert "OLDER-ASSISTANT-TURN" not in joined
        # The summary advanced to cover exactly the compressible turns.
        _, front_matter = cm.get_summary(sid)
        assert front_matter.get("summarized_up_to_turn") == len(turns) - 2 - 1


def test_in_loop_compressor_rebuild_includes_memory_entries() -> None:
    entries = [
        {
            "entry_type": "fact",
            "content": "REMEMBER-ME",
            "source_turn_index": 0,
            "confidence": 0.9,
            "created_at": "2026-01-01T00:00:00",
        }
    ]
    infer_fn = lambda req: _memory_output("summary text", entries)  # noqa: E731
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=infer_fn)
        sid = cm.create_session()
        cm.save_conversation(sid, _pending_tool_round_turns())

        compressor = InLoopContextCompressor(context_manager=cm, session_id=sid)
        rebuilt = compressor([], 5000, max_context=1000)

        assert rebuilt is not None
        assert "## Memory" in rebuilt[0].content
        assert "REMEMBER-ME" in rebuilt[0].content
        assert len(cm.get_memory_entries(sid)) == 1


def test_in_loop_compressor_is_noop_below_the_threshold() -> None:
    store: dict = {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=_capturing_infer(store))
        sid = cm.create_session()
        cm.save_conversation(sid, _pending_tool_round_turns())

        compressor = InLoopContextCompressor(context_manager=cm, session_id=sid)
        assert compressor([], 900, max_context=1000) is None
        assert store.get("calls", 0) == 0
        assert cm.get_summary(sid)[0] == ""


def test_in_loop_compressor_skips_when_the_model_window_is_unknown() -> None:
    """max_context=0 (unset) disables mid-loop compression entirely."""
    store: dict = {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=_capturing_infer(store))
        sid = cm.create_session()
        cm.save_conversation(sid, _pending_tool_round_turns())

        compressor = InLoopContextCompressor(context_manager=cm, session_id=sid)
        assert compressor([], 5_000_000, 0) is None  # explicitly unknown
        assert compressor([], 5_000_000) is None  # default is unknown
        assert store.get("calls", 0) == 0
        assert cm.get_summary(sid)[0] == ""


def test_in_loop_compressor_skips_while_the_stream_is_not_persisted() -> None:
    """Rebuilding from disk would drop an unpersisted pending tool round."""
    store: dict = {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=_capturing_infer(store))
        sid = cm.create_session()
        cm.save_conversation(sid, _pending_tool_round_turns())

        compressor = InLoopContextCompressor(
            context_manager=cm, session_id=sid, ready=lambda: False
        )
        assert compressor([], 5000, max_context=1000) is None
        assert store.get("calls", 0) == 0
        assert cm.get_summary(sid)[0] == ""


def test_in_loop_compressor_is_disabled_without_a_session() -> None:
    assert InLoopContextCompressor(context_manager=None, session_id=None)([], 5000, 1000) is None

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        assert InLoopContextCompressor(context_manager=cm, session_id=None)([], 5000, 1000) is None
        assert InLoopContextCompressor(context_manager=cm, session_id="")([], 5000, 1000) is None


def test_in_loop_compressor_without_persisted_turns_is_a_noop() -> None:
    store: dict = {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=_capturing_infer(store))
        sid = cm.create_session()  # session exists, conversation.json does not

        compressor = InLoopContextCompressor(context_manager=cm, session_id=sid)
        assert compressor([], 5000, max_context=1000) is None
        assert store.get("calls", 0) == 0


# ---------------------------------------------------------------------------
# IncrementalConversationPersister.is_current
# ---------------------------------------------------------------------------


def test_persister_is_current_tracks_persisted_state() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        sid = cm.create_session()
        persister = IncrementalConversationPersister(context_manager=cm, session_id=sid)

        pending = [Message(role="user", content="hi")]
        assert persister.is_current(pending) is False
        assert persister.is_current([]) is True

        # A protocol-complete batch (assistant tool call + matching result) is
        # written through, which makes the same list current.
        batch = [
            Message(
                role="assistant",
                content="",
                tool_calls=[{"id": "call_1", "name": "dummy_tool", "arguments": "{}"}],
            ),
            Message(role="tool", content="result", name="dummy_tool", tool_use_id="call_1"),
        ]
        assert persister.persist_completed(batch) is None
        assert persister.is_current(batch) is True
        # Anything newer than the persisted prefix is not on disk yet.
        assert persister.is_current(batch + [Message(role="user", content="next")]) is False


def test_disabled_persister_is_never_current() -> None:
    persister = IncrementalConversationPersister(context_manager=None, session_id=None)
    assert persister.is_current([]) is False


# ---------------------------------------------------------------------------
# Runtime.infer_stream hook
# ---------------------------------------------------------------------------


def _make_model_registry(
    protocol: str = "openai",
    max_context: int = 0,
    generate_params: dict | None = None,
) -> ModelRegistry:
    registry = ModelRegistry()
    registry.register(
        ModelConfig(
            model_id="test-model",
            api_base="http://localhost:9999",
            model_name="test",
            api_protocol=protocol,
            max_context=max_context,
            generate_params=dict(generate_params or {}),
        )
    )
    return registry


def _tool_registry(tool_result: str = "PENDING-TOOL-RESULT") -> ToolRegistry:
    registry = ToolRegistry()

    def dummy_tool() -> str:
        return tool_result

    registry.register(
        ToolConfig(
            tool_id="dummy_tool",
            tool_type="function",
            name="dummy_tool",
            description="A dummy tool",
            parameters={"type": "object", "properties": {}, "required": []},
        ),
        callable_fn=dummy_tool,
    )
    return registry


def _stream_response(stream: io.BytesIO):
    resp = MagicMock()
    resp.__iter__ = lambda self: iter(stream.readlines())
    resp.read = stream.read
    resp.close = MagicMock()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _tool_call_sse(tool_name: str, tool_use_id: str) -> io.BytesIO:
    chunk = json.dumps(
        {
            "choices": [
                {
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": tool_use_id,
                                "type": "function",
                                "function": {"name": tool_name, "arguments": "{}"},
                            }
                        ],
                    }
                }
            ]
        }
    )
    return io.BytesIO(f"data: {chunk}\n\ndata: [DONE]\n\n".encode("utf-8"))


def _content_sse(text: str) -> io.BytesIO:
    chunk = json.dumps({"choices": [{"delta": {"role": "assistant", "content": text}}]})
    return io.BytesIO(f"data: {chunk}\n\ndata: [DONE]\n\n".encode("utf-8"))


def _run_tool_loop(
    hook,
    *,
    first_round_sse=None,
    max_context: int = 0,
    tool_result: str = "PENDING-TOOL-RESULT",
    overflow_hook=None,
    generate_params: dict | None = None,
    pause_gate=None,
):
    """Run a two-round tool loop, returning (collected, bodies, round_count)."""
    runtime = Runtime(
        model_registry=_make_model_registry(
            max_context=max_context, generate_params=generate_params
        ),
        tool_registry=_tool_registry(tool_result=tool_result),
    )
    bodies: list[dict] = []
    rounds = [0]

    def mock_urlopen(req, **kwargs):
        rounds[0] += 1
        bodies.append(json.loads(req.data.decode("utf-8")))
        stream = (
            first_round_sse()
            if rounds[0] == 1
            else _content_sse("final answer")
        )
        return _stream_response(stream)

    request = InferenceRequest(
        model_id="test-model",
        tool_ids=["dummy_tool"],
        text="ORIGINAL-USER-TURN",
        stream=True,
        max_tool_rounds=5,
    )
    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        collected = list(runtime.infer_stream(
            request,
            on_round_complete=hook,
            on_context_overflow=overflow_hook,
            pause_gate=pause_gate,
        ))
    return collected, bodies, rounds[0]


def test_infer_stream_calls_hook_after_tool_round_and_uses_returned_messages() -> None:
    hook_calls: list[tuple[int, list]] = []
    rebuilt = [
        Message(role="system", content="REBUILT-SYSTEM"),
        Message(role="assistant", content="REBUILT-TAIL"),
    ]

    def hook(messages, round_total_tokens, max_context):
        hook_calls.append((round_total_tokens, list(messages)))
        return rebuilt

    collected, bodies, rounds = _run_tool_loop(
        hook, first_round_sse=lambda: _tool_call_sse("dummy_tool", "call_1")
    )

    # Only the tool round is reported; the terminal round is left to the
    # session-completed trigger.
    assert rounds == 2
    assert len(hook_calls) == 1
    round_tokens, seen_messages = hook_calls[0]
    assert round_tokens > 0
    assert any(m.role == "tool" for m in seen_messages)

    # Round 2 continued from the rebuilt context instead of the accumulated one.
    round2_contents = [m.get("content") or "" for m in bodies[1]["messages"]]
    assert "REBUILT-SYSTEM" in round2_contents
    assert "REBUILT-TAIL" in round2_contents
    assert "ORIGINAL-USER-TURN" not in round2_contents
    assert "PENDING-TOOL-RESULT" not in round2_contents

    # The final answer is still streamed to the caller.
    assert any(m.role == "assistant" and m.content == "final answer" for m in collected)


def test_infer_stream_forwards_the_model_max_context_to_the_hook() -> None:
    """The hook needs the running model's window to size its own threshold."""
    seen: list[int] = []

    def hook(messages, round_total_tokens, max_context):
        seen.append(max_context)
        return None

    collected, bodies, rounds = _run_tool_loop(
        hook,
        first_round_sse=lambda: _tool_call_sse("dummy_tool", "call_1"),
        max_context=1_048_576,
    )
    assert rounds == 2
    assert seen == [1_048_576]

    # Unset model window (0) is forwarded as-is: the hook then skips its check.
    collected, bodies, rounds = _run_tool_loop(
        hook, first_round_sse=lambda: _tool_call_sse("dummy_tool", "call_1")
    )
    assert seen == [1_048_576, 0]


def test_infer_stream_keeps_accumulated_context_when_hook_returns_none() -> None:
    def hook(messages, round_total_tokens, max_context):
        return None

    collected, bodies, rounds = _run_tool_loop(
        hook, first_round_sse=lambda: _tool_call_sse("dummy_tool", "call_1")
    )

    assert rounds == 2
    round2_contents = [m.get("content") or "" for m in bodies[1]["messages"]]
    assert "ORIGINAL-USER-TURN" in round2_contents
    assert "PENDING-TOOL-RESULT" in round2_contents
    assert any(m.role == "assistant" and m.content == "final answer" for m in collected)


def test_infer_stream_ignores_an_empty_hook_replacement(caplog) -> None:
    def hook(messages, round_total_tokens, max_context):
        return []

    with caplog.at_level(logging.WARNING):
        collected, bodies, rounds = _run_tool_loop(
            hook, first_round_sse=lambda: _tool_call_sse("dummy_tool", "call_1")
        )

    assert rounds == 2
    round2_contents = [m.get("content") or "" for m in bodies[1]["messages"]]
    assert "ORIGINAL-USER-TURN" in round2_contents
    assert any(
        "context refresh returned no messages" in record.getMessage()
        for record in caplog.records
    )
    assert any(m.role == "assistant" and m.content == "final answer" for m in collected)


def test_infer_stream_survives_a_failing_hook(caplog) -> None:
    def hook(messages, round_total_tokens, max_context):
        raise RuntimeError("compression backend exploded")

    with caplog.at_level(logging.ERROR):
        collected, bodies, rounds = _run_tool_loop(
            hook, first_round_sse=lambda: _tool_call_sse("dummy_tool", "call_1")
        )

    assert rounds == 2
    assert any(
        "mid-loop context refresh failed" in record.getMessage()
        for record in caplog.records
    )
    # The loop fell back to the accumulated context and finished normally.
    round2_contents = [m.get("content") or "" for m in bodies[1]["messages"]]
    assert "PENDING-TOOL-RESULT" in round2_contents
    assert any(m.role == "assistant" and m.content == "final answer" for m in collected)


def test_infer_stream_does_not_call_hook_for_a_terminal_round() -> None:
    hook_calls: list[int] = []

    def hook(messages, round_total_tokens, max_context):
        hook_calls.append(round_total_tokens)
        return None

    collected, bodies, rounds = _run_tool_loop(
        hook, first_round_sse=lambda: _content_sse("no tools here")
    )

    assert rounds == 1
    assert hook_calls == []
    assert any(m.role == "assistant" and m.content == "no tools here" for m in collected)


def test_infer_stream_without_hook_is_unchanged() -> None:
    """The default (no hook) keeps the previous behaviour byte-for-byte."""
    collected, bodies, rounds = _run_tool_loop(
        None, first_round_sse=lambda: _tool_call_sse("dummy_tool", "call_1")
    )

    assert rounds == 2
    round2_contents = [m.get("content") or "" for m in bodies[1]["messages"]]
    assert "ORIGINAL-USER-TURN" in round2_contents
    assert "PENDING-TOOL-RESULT" in round2_contents
    assert any(m.role == "assistant" and m.content == "final answer" for m in collected)


def test_production_wiring_compresses_and_rebuilds_mid_loop(tmp_path) -> None:
    """End-to-end wiring as used by the stream handler.

    Mirrors ``handler_infer``: a real ContextManager, an incremental persister
    fed on every ``usage``/``tool`` frame, the ``ready`` guard derived from that
    persister, and ``Runtime.infer_stream`` with the compressor as the
    ``on_round_complete`` hook.
    """
    summary_calls = {"count": 0}

    def summary_infer(req: Any) -> SimpleNamespace:
        summary_calls["count"] += 1
        return SimpleNamespace(
            content=(
                "<summary>\nCOMPRESSED-SUMMARY\n</summary>\n<memory>\n[]\n</memory>"
            )
        )

    cm = ContextManager(
        infer_fn=summary_infer,
        chats_dir=str(tmp_path),
        recent_turns_k=2,
        summary_model_id="test-model",
        max_tokens_in_context=10,
    )
    sid = cm.create_session()

    collected: list[Message] = []
    original_messages = [Message(role="user", content="ORIGINAL-USER-TURN")]
    persister = IncrementalConversationPersister(
        context_manager=cm,
        session_id=sid,
        original_messages=original_messages,
    )
    assert persister.pre_persist() is None
    compressor = InLoopContextCompressor(
        context_manager=cm,
        session_id=sid,
        ready=lambda: persister.is_current(collected),
    )

    # A tiny declared window (20 tokens) makes the trigger deterministic: the
    # round is far above 90 % of it.
    runtime = Runtime(
        model_registry=_make_model_registry(max_context=20),
        tool_registry=_tool_registry(),
    )
    bodies: list[dict] = []
    rounds = [0]

    def mock_urlopen(req, **kwargs):
        rounds[0] += 1
        bodies.append(json.loads(req.data.decode("utf-8")))
        stream = (
            _tool_call_sse("dummy_tool", "call_1")
            if rounds[0] == 1
            else _content_sse("final answer")
        )
        return _stream_response(stream)

    request = InferenceRequest(
        model_id="test-model",
        tool_ids=["dummy_tool"],
        messages=list(original_messages),
        stream=True,
        max_tool_rounds=5,
    )
    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        for msg in runtime.infer_stream(request, on_round_complete=compressor):
            collected.append(msg)
            # Exactly the incremental persistence the stream handler performs.
            if msg.role in ("usage", "tool"):
                assert persister.persist_completed(collected) is None

    # 1. The pending tool round was already on disk when the hook ran, so the
    #    guard held and the session was compressed (and memory refreshed).
    assert summary_calls["count"] == 1
    summary_text, front_matter = cm.get_summary(sid)
    assert "COMPRESSED-SUMMARY" in summary_text
    turns_on_disk = cm.load_conversation(sid)
    # At hook time the session held exactly [user, assistant(tool_calls), tool];
    # the tool round was already persisted, which is what lets the rebuild keep it.
    assert [t.role for t in turns_on_disk[:3]] == ["user", "assistant", "tool"]
    assert turns_on_disk[1].tool_calls
    assert turns_on_disk[2].content == "PENDING-TOOL-RESULT"
    # Only the older user turn was summarized; the pending round stays verbatim.
    assert front_matter.get("summarized_up_to_turn") == 0
    assert os.path.isfile(os.path.join(str(tmp_path), sid, "memory.md"))

    # 2. Round 2 continued on the rebuilt context: rolling summary + user
    #    anchor + the pending tool round, not the accumulated history.  The
    #    user turn itself was summarized away, but the pure assistant/tool tail
    #    still needs its user anchor or providers like Ollama reject the
    #    request with "no user query found in messages".
    assert rounds[0] == 2
    round2_messages = bodies[1]["messages"]
    assert [m["role"] for m in round2_messages] == ["system", "user", "assistant", "tool"]
    assert "COMPRESSED-SUMMARY" in round2_messages[0]["content"]
    assert round2_messages[1]["content"] == "ORIGINAL-USER-TURN"
    assert round2_messages[3]["content"] == "PENDING-TOOL-RESULT"
    assert any(m.role == "assistant" and m.content == "final answer" for m in collected)



# ---------------------------------------------------------------------------
# Pre-send context guard: the check that runs BEFORE the request is sent
# ---------------------------------------------------------------------------
#
# ``on_round_complete`` measures the round that just ENDED (exact token counts,
# and no user waiting time spent on the compression it triggers).  The context
# guard measures the request that is about to be SENT, which is the only
# measurement that can still prevent a provider rejection: this round's tool
# results and a user message injected at a pause boundary both join AFTER the
# post-round compression.


def _budget_estimate(budget: int):
    """The estimate callable the loop hands to the overflow hook."""
    from runtime.common import estimate_chat_prompt_tokens_capped

    return lambda msgs: estimate_chat_prompt_tokens_capped(msgs, None, budget)


def test_send_budget_tokens_reserves_room_for_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """budget = max_context minus the output reserve, derived then kept sane."""
    monkeypatch.delenv("CONTEXT_OUTPUT_RESERVE_TOKENS", raising=False)
    budget_of = Runtime._context_send_budget_tokens

    def cfg(max_context: int, **params: int) -> ModelConfig:
        return ModelConfig(
            model_id="m",
            api_base="http://x",
            model_name="m",
            max_context=max_context,
            generate_params=dict(params),
        )

    assert budget_of(cfg(0)) == 0, "unknown window: the guard stays off"
    assert budget_of(cfg(10_000, max_completion_tokens=4_000)) == 6_000
    assert budget_of(cfg(10_000, max_tokens=50_000)) == 5_000, "reserve clamped to half"
    assert budget_of(cfg(200_000)) == 200_000 - 8192, "default reserve"
    assert budget_of(cfg(1_500)) == 750, "a tiny window keeps half for the reply"

    monkeypatch.setenv("CONTEXT_OUTPUT_RESERVE_TOKENS", "1000")
    assert budget_of(cfg(10_000, max_tokens=4_000)) == 9_000, "env wins over the model cap"
    monkeypatch.setenv("CONTEXT_OUTPUT_RESERVE_TOKENS", "not-a-number")
    assert budget_of(cfg(10_000, max_tokens=4_000)) == 6_000, "invalid env falls back"


def test_guard_measures_the_request_that_is_about_to_be_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fat tool result is caught before round 2 although round 1 was small."""
    monkeypatch.setenv("CONTEXT_OUTPUT_RESERVE_TOKENS", "1024")
    window = 4000
    seen: list[tuple] = []

    def overflow(messages, budget, estimate, max_context):
        seen.append((budget, max_context, estimate(messages)))
        compact = [Message(role="user", content="REBUILT-COMPACT")]
        return compact, estimate(compact)

    collected, bodies, rounds = _run_tool_loop(
        None,
        first_round_sse=lambda: _tool_call_sse("dummy_tool", "call_1"),
        max_context=window,
        tool_result="fat " * 20_000,
        overflow_hook=overflow,
    )

    assert rounds == 2
    assert len(seen) == 1, "only the oversized round is acted on"
    budget, seen_window, measured = seen[0]
    assert (seen_window, budget) == (window, window - 1024)
    assert measured > budget, "the tool result alone exceeds the ceiling"

    round2_contents = [m.get("content") or "" for m in bodies[1]["messages"]]
    assert "REBUILT-COMPACT" in round2_contents, "the loop continued on the rebuilt list"
    assert all("fat fat" not in text for text in round2_contents)
    assert any(m.role == "assistant" and m.content == "final answer" for m in collected)


def test_guard_refuses_a_request_that_cannot_be_made_to_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A remedy that does not help ends in an Error: instead of a rejected call."""
    monkeypatch.setenv("CONTEXT_OUTPUT_RESERVE_TOKENS", "1024")

    def overflow(messages, budget, estimate, max_context):
        return None, estimate(messages)  # nothing helped

    collected, bodies, rounds = _run_tool_loop(
        None,
        first_round_sse=lambda: _tool_call_sse("dummy_tool", "call_1"),
        max_context=4000,
        tool_result="fat " * 20_000,
        overflow_hook=overflow,
    )

    assert rounds == 1, "the doomed second request must not be sent"
    errors = [
        m.content for m in collected
        if isinstance(m.content, str) and m.content.startswith("Error:")
    ]
    assert len(errors) == 1 and "context too large" in errors[0], errors


def test_guard_is_inert_while_the_model_window_is_unknown() -> None:
    """No window configured: nothing is measured and nothing is refused."""
    calls: list[object] = []
    collected, bodies, rounds = _run_tool_loop(
        None,
        first_round_sse=lambda: _tool_call_sse("dummy_tool", "call_1"),
        max_context=0,
        tool_result="fat " * 20_000,
        overflow_hook=lambda *args: calls.append(args),
    )
    assert calls == []
    assert rounds == 2, "behaviour is unchanged when the window is unknown"


def test_guard_without_a_session_to_compress_only_warns(caplog) -> None:
    """A stateless request has no remedy: report it, but still send (as before)."""
    with caplog.at_level(logging.WARNING, logger="runtime.runtime"):
        collected, bodies, rounds = _run_tool_loop(
            None,
            first_round_sse=lambda: _tool_call_sse("dummy_tool", "call_1"),
            max_context=4000,
            tool_result="fat " * 20_000,
        )
    assert rounds == 2
    assert any("no session context" in record.getMessage() for record in caplog.records)


def test_guard_sees_a_user_message_injected_at_a_pause_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hole the post-round check cannot see: an interjection joins after it."""
    monkeypatch.setenv("CONTEXT_OUTPUT_RESERVE_TOKENS", "1024")
    gate_calls = [0]

    def pause_gate():
        gate_calls[0] += 1
        if gate_calls[0] == 2:  # reached once the tool round has completed
            return {
                "append": [{"role": "user", "content": "INTERJECTED " * 6_000}],
                "stop": False,
            }
        return None

    seen: list[bool] = []

    def overflow(messages, budget, estimate, max_context):
        seen.append(
            any("INTERJECTED" in (getattr(m, "content", "") or "") for m in messages)
        )
        compact = [Message(role="user", content="REBUILT-COMPACT")]
        return compact, estimate(compact)

    collected, bodies, rounds = _run_tool_loop(
        None,
        first_round_sse=lambda: _tool_call_sse("dummy_tool", "call_1"),
        max_context=4000,
        pause_gate=pause_gate,
        overflow_hook=overflow,
    )

    assert rounds == 2
    assert seen == [True], "the interjection was part of the measured request"


def _fat_pending_round(fat: str) -> list:
    """The in-flight message list of a tool round whose result is oversized."""
    return [
        Message(role="user", content="OLDER-USER-TURN"),
        Message(role="assistant", content="OLDER-ASSISTANT-TURN"),
        Message(
            role="assistant",
            content="",
            tool_calls=[{"id": "call_1", "name": "dummy_tool", "arguments": "{}"}],
        ),
        Message(role="tool", content=fat, name="dummy_tool", tool_use_id="call_1"),
    ]


def _pending_round_turns_with_fat(fat: str) -> list:
    """A persisted session whose pending round produced an oversized result."""
    from runtime.context_manager import ConversationTurn

    turns = _pending_tool_round_turns()
    turns[-1] = ConversationTurn(
        role="tool", content=fat, timestamp="2026-01-01T00:00:03",
        name="dummy_tool", tool_use_id="call_1",
    )
    return turns


def test_ensure_within_budget_compresses_then_offloads(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Least lossy first: summary, then deeper summary, then file offload.

    The pending round is never dropped; only its oversized result leaves the
    context, and it leaves as a file reference the agent can read back.
    """
    spill_dir = str(tmp_path / "spill")
    monkeypatch.setenv("CONTEXT_SPILL_DIR", spill_dir)
    store: dict = {}
    cm = _make_cm(str(tmp_path), infer_fn=_capturing_infer(store), recent_turns_k=2)
    sid = cm.create_session()

    fat = "TOOLDUMP " * 30_000
    cm.save_conversation(sid, _pending_round_turns_with_fat(fat))

    in_flight = _fat_pending_round(fat)
    compressor = InLoopContextCompressor(
        context_manager=cm, session_id=sid, inference_model_id="test-model",
    )
    budget = 2_000
    replacement, after = compressor.ensure_within_budget(
        in_flight, budget, _budget_estimate(budget)
    )

    assert replacement is not None, "the guard must change something"
    assert after <= budget, f"still {after} tokens over a {budget} budget"

    # The shallow pass could not free enough (the pending round alone is
    # oversized), so a second, deeper pass ran before the offload step.
    assert store["calls"] == 2, f"expected a shallow and a deep pass, got {store['calls']}"
    assert cm.get_summary(sid)[0].strip(), "the compression produced a summary"

    # The pending round survived, with its oversized result offloaded.
    tool_messages = [m for m in replacement if m.role == "tool"]
    assert tool_messages, "the tool result the model waits for must stay"
    assert "output offloaded to file:" in tool_messages[-1].content
    assert len(tool_messages[-1].content) < 5_000, "only an excerpt stays inline"

    # The full text is on disk: nothing was lost, only moved out of the context.
    offloaded = os.listdir(os.path.join(spill_dir, sid))
    assert offloaded
    with open(os.path.join(spill_dir, sid, offloaded[0]), encoding="utf-8") as fh:
        assert fh.read() == fat

    # The caller's own Message objects are untouched: persistence and the UI
    # transcript keep the verbatim tool output.
    assert in_flight[-1].content == fat


def test_ensure_within_budget_offloads_when_compression_is_unavailable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No summary model and no inference model id: offload is what remains."""
    spill_dir = str(tmp_path / "spill")
    monkeypatch.setenv("CONTEXT_SPILL_DIR", spill_dir)
    # No compression model at all: neither a summary model nor an inference
    # model id, which is what makes the deeper-summary step a no-op.
    monkeypatch.setenv("SUMMARY_MODEL_ID", "")
    cm = _make_cm(str(tmp_path), recent_turns_k=2, summary_model_id="")
    sid = cm.create_session()

    fat = "TOOLDUMP " * 30_000
    cm.save_conversation(sid, _pending_round_turns_with_fat(fat))

    in_flight = _fat_pending_round(fat)
    compressor = InLoopContextCompressor(context_manager=cm, session_id=sid)
    budget = 2_000
    replacement, after = compressor.ensure_within_budget(
        in_flight, budget, _budget_estimate(budget)
    )

    assert cm.get_summary(sid)[0] == "", "no compression model: nothing to summarize"
    assert replacement is not None and after <= budget
    assert "output offloaded to file:" in replacement[-1].content
    assert in_flight[-1].content == fat


def test_ensure_within_budget_is_inert_when_the_request_already_fits() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        sid = cm.create_session()
        cm.save_conversation(sid, _pending_tool_round_turns())
        compressor = InLoopContextCompressor(context_manager=cm, session_id=sid)
        messages = _fat_pending_round("small result")
        estimate = _budget_estimate(2_000)
        replacement, after = compressor.ensure_within_budget(messages, 20_000, estimate)
        assert replacement is None and after <= 20_000
        assert cm.get_summary(sid)[0] == "", "nothing was compressed"


def test_ensure_within_budget_skips_rebuild_but_still_offloads_when_not_persisted(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unpersisted round: rebuilding would lose it, so only offload is available."""
    spill_dir = str(tmp_path / "spill")
    monkeypatch.setenv("CONTEXT_SPILL_DIR", spill_dir)
    cm = _make_cm(str(tmp_path), recent_turns_k=2)
    sid = cm.create_session()
    cm.save_conversation(sid, _pending_tool_round_turns())

    fat = "TOOLDUMP " * 30_000
    compressor = InLoopContextCompressor(
        context_manager=cm, session_id=sid, ready=lambda: False,
    )
    budget = 2_000
    replacement, after = compressor.ensure_within_budget(
        _fat_pending_round(fat), budget, _budget_estimate(budget)
    )
    assert replacement is not None and after <= budget
    assert "output offloaded to file:" in replacement[-1].content





def test_guard_never_offloads_the_summary_and_memory_message(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spilling the system message would spend the compression it just asked for.

    The merged system message is where ``assemble_context`` carries the agent
    prompt, the rolling summary and the structured memory.  A real run answered
    ``context guard: refusing to send`` after offloading that very message to a
    file: the request was still oversized, and it had also become amnesic.
    """
    spill_dir = str(tmp_path / "spill")
    monkeypatch.setenv("CONTEXT_SPILL_DIR", spill_dir)
    cm = _make_cm(str(tmp_path), recent_turns_k=2)
    sid = cm.create_session()
    cm.save_conversation(sid, _pending_tool_round_turns())

    # Big enough to be an offload candidate (~38 000 estimated tokens), and the
    # single biggest message in the request: offloading it would have "fixed"
    # the budget by throwing the summary and the memory away.
    in_flight = [Message(role="system", content="SUMMARY-AND-MEMORY " * 8_000)]
    for index in range(20):
        role = "user" if index % 2 == 0 else "assistant"
        in_flight.append(Message(role=role, content=f"{role.upper()}-{index} " + "MEDIUM-TURN-" * 300))
    in_flight.append(Message(role="assistant", content="NEWEST-ANSWER"))

    compressor = InLoopContextCompressor(
        context_manager=cm, session_id=sid, ready=lambda: False,
    )
    budget = 42_000
    replacement, after = compressor.ensure_within_budget(
        in_flight, budget, _budget_estimate(budget)
    )

    assert replacement is not None, "the guard must change something"
    assert after <= budget, f"still {after} tokens over a {budget}-token budget"
    assert replacement[0].role == "system"
    assert "output offloaded to file:" not in replacement[0].content, (
        "the summary/memory message is not an offload candidate"
    )
    assert replacement[0].content.startswith("SUMMARY-AND-MEMORY")
    assert not os.path.isdir(os.path.join(spill_dir, sid)), "nothing was spilled"


def test_guard_drops_oldest_turns_when_offloading_cannot_help(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overflow spread over many medium turns is cleared by dropping the oldest.

    Reproduced from a real refusal: 222 messages / ~189 000 tokens of
    unsummarized window, of which only four reached the offload threshold.  The
    guard offloaded four, could not shed the rest, and refused to send -- an
    inference that never ran at all, for a session that still had its summary.
    """
    spill_dir = str(tmp_path / "spill")
    monkeypatch.setenv("CONTEXT_SPILL_DIR", spill_dir)
    cm = _make_cm(str(tmp_path), recent_turns_k=2)
    sid = cm.create_session()
    cm.save_conversation(sid, _pending_tool_round_turns())

    medium = "MEDIUM-TURN-" * 400  # ~2 400 tokens: under the 4 000-token threshold
    in_flight = [Message(role="system", content="SUMMARY-AND-MEMORY")]
    for index in range(60):
        role = "user" if index % 2 == 0 else "assistant"
        in_flight.append(Message(role=role, content=f"{role.upper()}-{index} {medium}"))
    in_flight.append(
        Message(
            role="assistant", content="NEWEST-ROUND",
            tool_calls=[{"id": "call_1", "name": "dummy_tool", "arguments": "{}"}],
        )
    )
    in_flight.append(
        Message(role="tool", content="PENDING-TOOL-RESULT", name="dummy_tool",
                tool_use_id="call_1")
    )
    untouched = len(in_flight)

    compressor = InLoopContextCompressor(
        context_manager=cm, session_id=sid, ready=lambda: False,
    )
    budget = 20_000
    replacement, after = compressor.ensure_within_budget(
        in_flight, budget, _budget_estimate(budget)
    )

    assert replacement is not None, "the guard must change something"
    assert after <= budget, f"still {after} tokens over a {budget}-token budget"
    assert len(in_flight) == untouched, "the caller's list is never mutated"
    assert len(replacement) < untouched
    assert replacement[0].role == "system"
    assert "oldest message(s) of this session were dropped" in replacement[0].content, (
        "the model is told its context was trimmed, not shown a silently "
        "shorter past"
    )
    assert replacement[-1].content == "PENDING-TOOL-RESULT", (
        "the round the model is waiting for survives"
    )
    assert replacement[-2].content == "NEWEST-ROUND"
    assert not any(m.role == "tool" for m in replacement[:2]), (
        "the window never opens on an orphaned tool result"
    )
    assert any(m.role == "user" for m in replacement), (
        "a request without a user message is rejected by some providers"
    )
    assert not os.path.isdir(os.path.join(spill_dir, sid)), (
        "medium messages are not written to files one by one; they are dropped"
    )


def test_guard_drops_nothing_when_the_request_only_needs_nothing_dropped(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The oldest-drops step is a last resort, not a routine trim."""
    monkeypatch.setenv("CONTEXT_SPILL_DIR", str(tmp_path / "spill"))
    cm = _make_cm(str(tmp_path), recent_turns_k=2)
    sid = cm.create_session()
    cm.save_conversation(sid, _pending_tool_round_turns())
    in_flight = [
        Message(role="system", content="SUMMARY"),
        Message(role="user", content="OLDER-USER-TURN"),
        Message(role="assistant", content="ANSWER"),
    ]
    compressor = InLoopContextCompressor(
        context_manager=cm, session_id=sid, ready=lambda: False,
    )
    replacement, after = compressor.ensure_within_budget(
        in_flight, 20_000, _budget_estimate(20_000)
    )
    assert replacement is None and after <= 20_000
# ---------------------------------------------------------------------------
# Offload helpers
# ---------------------------------------------------------------------------


def test_offload_is_content_addressed_excerpted_and_pruned(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.context_manager import (
        _CONTEXT_SPILL_KEEP_FILES,
        is_spilled_content,
        spill_reference,
    )

    spill_dir = tmp_path / "spill"
    monkeypatch.setenv("CONTEXT_SPILL_DIR", str(spill_dir))

    fat = "z" * 40_000
    first = spill_reference("s1", fat, label="tool")
    assert first and "output offloaded to file:" in first
    assert len(first) < 5_000, "the replacement is bounded whatever the input size"
    assert first == spill_reference("s1", fat, label="tool"), "same content, same file"
    assert len(os.listdir(str(spill_dir / "s1"))) == 1

    assert spill_reference("s1", "short one", label="tool") is None, "short stays short"
    assert is_spilled_content(first)
    assert spill_reference("s1", first, label="tool") == first, "never re-offloaded"

    for index in range(_CONTEXT_SPILL_KEEP_FILES + 10):
        spill_reference("s2", f"{index} " + "y" * 40_000, label="tool")
    assert len(os.listdir(str(spill_dir / "s2"))) == _CONTEXT_SPILL_KEEP_FILES

    # A session id must stay one path segment.
    assert spill_reference("../escape", fat, label="tool") is None
