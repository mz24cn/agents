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
) -> ContextManager:
    if infer_fn is None:
        infer_fn = lambda req: SimpleNamespace(  # noqa: E731
            content="<summary>\nsummary text\n</summary>\n<memory>\n[]\n</memory>"
        )
    return ContextManager(
        infer_fn=infer_fn,
        chats_dir=tmp_dir,
        recent_turns_k=recent_turns_k,
        summary_model_id="test-model",
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
        assert [m.role for m in rebuilt] == ["system", "assistant", "tool"]
        # 1. Merged system message: rolling summary (+ memory) and the session
        #    system prompt come from the same code path as a new turn.
        assert "## Summary" in rebuilt[0].content
        assert "summary text" in rebuilt[0].content
        # 2. The pending tool round is kept verbatim, so the next round still
        #    sees the assistant tool call and its result.
        assert rebuilt[1].tool_calls
        assert rebuilt[1].tool_calls[0]["id"] == "call_1"
        assert rebuilt[2].content == "PENDING-TOOL-RESULT"
        assert rebuilt[2].tool_use_id == "call_1"
        # 3. The summarized turns are gone from the verbatim window.
        joined = "\n".join((m.content or "") for m in rebuilt)
        assert "OLDER-USER-TURN" not in joined
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
    protocol: str = "openai", max_context: int = 0
) -> ModelRegistry:
    registry = ModelRegistry()
    registry.register(
        ModelConfig(
            model_id="test-model",
            api_base="http://localhost:9999",
            model_name="test",
            api_protocol=protocol,
            max_context=max_context,
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


def _run_tool_loop(hook, *, first_round_sse=None, max_context: int = 0):
    """Run a two-round tool loop, returning (collected, bodies, round_count)."""
    runtime = Runtime(
        model_registry=_make_model_registry(max_context=max_context),
        tool_registry=_tool_registry(),
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
        collected = list(runtime.infer_stream(request, on_round_complete=hook))
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

    # 2. Round 2 continued on the rebuilt context: rolling summary + the pending
    #    tool round, not the accumulated history.
    assert rounds[0] == 2
    round2_messages = bodies[1]["messages"]
    assert [m["role"] for m in round2_messages] == ["system", "assistant", "tool"]
    assert "COMPRESSED-SUMMARY" in round2_messages[0]["content"]
    assert round2_messages[2]["content"] == "PENDING-TOOL-RESULT"
    assert all(
        "ORIGINAL-USER-TURN" not in (m.get("content") or "") for m in round2_messages
    )
    assert any(m.role == "assistant" and m.content == "final answer" for m in collected)
