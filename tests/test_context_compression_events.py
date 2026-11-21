"""Observability of context compression: SSE lifecycle events + audit trail.

Compressing a session's context is invisible work.  It stops a running
inference for seconds while the summary model writes ``summary.md`` /
``memory.md``, produces no token, and leaves nothing behind in
``conversation.json`` saying it happened -- which is exactly why
"why did this session go quiet?" and "what was the model told about its own
history around turn N?" used to be unanswerable after the fact (each new
summary version overwrites the previous one).

It now reports itself the way a tool call does:

* two SSE events per attempt that reaches the model -- ``phase=started``
  before the request, ``phase=completed`` with the outcome after it (an
  attempt the guards declined emits NOTHING, so a ``started`` can never hang);
* one audit entry appended to the assistant turn that was current, in
  ``conversation.json`` under ``messages[i].meta.compressions`` (persistence
  only: ``meta`` never reaches a provider).

Covers:
  - the event pair, its fields and its ordering
  - guards (below threshold / nothing new / failure backoff) stay silent
  - a failed attempt still closes the pair and is still recorded
  - a raising event sink cannot break compression
  - the marker lands on the last assistant turn, preserves the turn's own
    ``meta``, appends across rounds, and is capped; the events and the persisted
    entry name the same ``attempt`` so a client can show one marker per
    compression however many times a frame reaches it
  - the marker never enters the model-facing context
  - every trigger labels itself (``post_turn`` / ``in_loop`` / ``pre_send`` /
    ``manual``)
  - the SSE transport chooses the starter callback, the retained broker, or the
    global session-events channel -- and never invents a broker generation
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch

import pytest

from runtime import common, server_state
from runtime.context_manager import (
    ContextManager,
    ConversationTurn,
    _COMPRESSION_MARKER_CAP,
    serialize_summary,
    turn_to_message_dict,
)
from runtime.models import InferenceResult, Message, ModelConfig
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer
from runtime.server_state import persist_conversation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OK_OUTPUT = "<summary>\nsummary text\n</summary>\n<memory>\n[]\n</memory>"


def _make_cm(
    tmp_dir: str,
    *,
    infer_fn: Callable | None = None,
    events: list | None = None,
    recent_turns_k: int = 1,
    max_tokens_in_context: int = 100,
) -> ContextManager:
    if infer_fn is None:
        infer_fn = lambda req: SimpleNamespace(content=_OK_OUTPUT)  # noqa: E731
    cm = ContextManager(
        infer_fn=infer_fn,
        chats_dir=tmp_dir,
        recent_turns_k=recent_turns_k,
        summary_model_id="test-model",
        max_tokens_in_context=max_tokens_in_context,
    )
    if events is not None:
        cm.set_compression_event_sink(
            lambda sid, payload: events.append((sid, payload))
        )
    return cm


def _session(cm: ContextManager, session_id: str, n_turns: int = 6) -> list[ConversationTurn]:
    """Create a session directory and persist ``n_turns`` alternating turns."""
    os.makedirs(os.path.join(cm._chats_dir, session_id), exist_ok=True)
    turns: list[ConversationTurn] = []
    for i in range(n_turns):
        role = "user" if i % 2 == 0 else "assistant"
        turns.append(ConversationTurn(
            role=role,
            content=f"turn {i} " + ("x" * 40),
            timestamp=f"2026-01-01T00:00:{i:02d}",
        ))
    cm.save_conversation(session_id, turns, last_total_tokens=9_999)
    return turns


def _read_messages(cm: ContextManager, session_id: str) -> list[dict]:
    with open(cm._conversation_path(session_id), encoding="utf-8") as fh:
        return json.load(fh)["messages"]


# ---------------------------------------------------------------------------
# The SSE event pair
# ---------------------------------------------------------------------------


def test_successful_compression_emits_started_then_completed() -> None:
    events: list = []
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp, events=events)
        sid = "s1"
        turns = _session(cm, sid)
        cm.compress_context(sid, cm.load_conversation(sid), trigger="post_turn")

    assert [payload["phase"] for _, payload in events] == ["started", "completed"]
    started, completed = (payload for _, payload in events)
    # ``started`` describes intent, not outcome: it carries no success/error.
    assert "success" not in started and "error" not in started
    assert completed["success"] is True
    assert completed["error"] == ""
    assert completed["duration_ms"] >= 0
    assert completed["summary_version"] == 1


def test_event_pair_shares_the_identity_of_the_attempt() -> None:
    """Both events describe ONE attempt: same session, trigger, model, budget."""
    events: list = []
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp, events=events)
        sid = "s1"
        _session(cm, sid)
        cm.compress_context(sid, cm.load_conversation(sid), trigger="post_turn")

    (sid_a, started), (sid_b, completed) = events
    assert sid_a == sid_b == "s1"
    for key in ("attempt", "trigger", "model_id", "tokens", "threshold",
                "delta_start", "delta_turns", "prompt_chars"):
        assert started[key] == completed[key], key
    assert started["trigger"] == "post_turn"
    assert started["delta_turns"] > 0
    assert started["prompt_chars"] > 0


def test_started_reports_the_plan_and_completed_the_result() -> None:
    """``summary_version`` in ``started`` is the state BEFORE, and the field
    named ``*_target`` is the plan; ``completed`` reports what is on disk."""
    events: list = []
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp, events=events)
        sid = "s1"
        _session(cm, sid)
        cm.compress_context(sid, cm.load_conversation(sid), trigger="post_turn")

    started = events[0][1]
    completed = events[1][1]
    assert started["summary_version"] == 0
    assert started["summarized_up_to_turn_target"] == completed["summarized_up_to_turn"]
    assert completed["summary_version"] == 1
    assert completed["memory_entries"] == 0
    assert completed["summary_chars"] > 0


# ---------------------------------------------------------------------------
# Guards stay silent: no ``started`` without its ``completed``
# ---------------------------------------------------------------------------


def test_compression_below_threshold_emits_nothing() -> None:
    events: list = []
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp, events=events, max_tokens_in_context=100_000)
        sid = "s1"
        _session(cm, sid)
        cm.compress_context(sid, cm.load_conversation(sid), last_total_tokens=50)

    assert events == []


def test_compression_with_nothing_new_to_compress_emits_nothing() -> None:
    events: list = []
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp, events=events)
        sid = "s1"
        turns = _session(cm, sid)
        cm.compress_context(sid, cm.load_conversation(sid))
        assert len(events) == 2
        # The summary now covers every compressible turn: a second call must
        # not even reach the model, and therefore must not announce itself.
        cm.compress_context(sid, cm.load_conversation(sid))
        assert len(events) == 2
        assert events[-1][1]["phase"] == "completed"


def test_compression_in_failure_backoff_emits_nothing() -> None:
    """A session that keeps failing compressions is skipped silently: no
    request goes out, so there is nothing to wait for."""
    events: list = []

    def _failing(req: Any) -> SimpleNamespace:
        return SimpleNamespace(
            success=False, messages=list(req.messages), error="HTTP 400 oversized"
        )

    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp, infer_fn=_failing, events=events)
        sid = "s1"
        _session(cm, sid)
        loaded = cm.load_conversation(sid)
        # Three consecutive failures are what engages the backoff.
        for tokens in (500, 700, 900):
            cm.compress_context(sid, loaded, last_total_tokens=tokens)
        assert [p["phase"] for _, p in events] == ["started", "completed"] * 3
        # Inside the backoff window: nothing is sent, so nothing is announced.
        cm.compress_context(sid, loaded, last_total_tokens=1000)
        assert len(events) == 6
        # Past it (tokens grew by more than 25 %) the attempt -- and the pair --
        # comes back.
        cm.compress_context(sid, loaded, last_total_tokens=2000)
        assert len(events) == 8


# ---------------------------------------------------------------------------
# Failures are reported, not swallowed
# ---------------------------------------------------------------------------


def test_failed_compression_still_closes_the_pair_and_is_recorded() -> None:
    events: list = []

    def _failing(req: Any) -> SimpleNamespace:
        return SimpleNamespace(
            success=False, messages=list(req.messages), error="HTTP 400 oversized"
        )

    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp, infer_fn=_failing, events=events)
        sid = "s1"
        _session(cm, sid)
        cm.compress_context(sid, cm.load_conversation(sid), trigger="in_loop")

        assert [p["phase"] for _, p in events] == ["started", "completed"]
        completed = events[1][1]
        assert completed["success"] is False
        assert "oversized" in completed["error"]
        assert completed["summary_version"] == 0
        assert completed["summarized_up_to_turn"] is None
        # The audit trail records the failure: that is the answer to
        # "why did this session's context keep growing".
        entry = _read_messages(cm, sid)[-1]["meta"]["compressions"][0]
        assert entry["success"] is False and entry["trigger"] == "in_loop"


def test_placeholder_echo_is_reported_as_a_failure() -> None:
    """The model echoing the prompt's own format example is not a summary."""
    events: list = []

    def _echo(req: Any) -> SimpleNamespace:
        return SimpleNamespace(
            content="<summary>\n(concise summary prose, typically 2–5 paragraphs)\n</summary>\n"
                    "<memory>\n[]\n</memory>"
        )

    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp, infer_fn=_echo, events=events)
        sid = "s1"
        _session(cm, sid)
        cm.compress_context(sid, cm.load_conversation(sid))

        assert [p["phase"] for _, p in events] == ["started", "completed"]
        assert events[1][1]["success"] is False
        assert "format example" in events[1][1]["error"]


def test_a_raising_event_sink_cannot_break_compression() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp)
        boom_called = []

        def _boom(sid: str, payload: dict) -> None:
            boom_called.append(payload["phase"])
            raise RuntimeError("client gone")

        cm.set_compression_event_sink(_boom)
        sid = "s1"
        _session(cm, sid)
        cm.compress_context(sid, cm.load_conversation(sid))

        assert boom_called == ["started", "completed"]
        # The summary itself was still written.
        text, fm = cm.get_summary(sid)
        assert fm["summary_version"] == 1 and "summary text" in text
        # ... and so was the audit entry: the marker is written independently
        # of the transport, not from inside the sink.
        entry = _read_messages(cm, sid)[-1]["meta"]["compressions"]
        assert len(entry) == 1


def test_span_close_answers_a_body_that_died_mid_request() -> None:
    """``compress_context`` closes its span in a ``finally``: whatever raises
    inside the body after the request was announced still gets a ``completed``.
    """
    events: list = []
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp, events=events)
        sid = "s1"
        _session(cm, sid)

        def _explode(*args: Any, **kwargs: Any) -> str:
            raise OSError("disk gone")

        cm._atomic_write = staticmethod(_explode)  # type: ignore[assignment]
        with pytest.raises(OSError):
            cm.compress_context(sid, cm.load_conversation(sid))

    assert [p["phase"] for _, p in events] == ["started", "completed"]
    assert events[1][1]["success"] is False
    assert "without an outcome" in events[1][1]["error"]


# ---------------------------------------------------------------------------
# The audit trail inside conversation.json
# ---------------------------------------------------------------------------


def test_marker_lands_on_the_last_assistant_turn_and_keeps_its_meta() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp)
        sid = "s1"
        os.makedirs(os.path.join(cm._chats_dir, sid), exist_ok=True)
        turns = [
            ConversationTurn(role="user", content="q " + "y" * 50,
                              timestamp="2026-01-01T00:00:00",
                              meta={"model_id": "m1", "tool_ids": ["read_file"]}),
            ConversationTurn(role="assistant", content="a " + "y" * 50,
                             timestamp="2026-01-01T00:00:01"),
        ]
        cm.save_conversation(sid, turns, last_total_tokens=9999)
        cm.compress_context(sid, cm.load_conversation(sid))

        saved = _read_messages(cm, sid)
        # The user turn's own snapshot is untouched...
        assert saved[0]["meta"]["model_id"] == "m1"
        assert "compressions" not in saved[0]["meta"]
        # ... and the compression is recorded on the assistant that was current.
        entries = saved[1]["meta"]["compressions"]
        assert len(entries) == 1
        assert entries[0]["turn_index"] == 1
        assert entries[0]["turn_timestamp"] == "2026-01-01T00:00:01"


def test_the_audit_entry_names_the_attempt_its_events_announced() -> None:
    """The UI shows one marker per attempt and deduplicates the live frames by
    ``attempt``; the record persisted in conversation.json has to name the same
    attempt, or a reopened session could not tell which event pair it is the
    restored marker came from (and a replayed frame would double the marker).
    """
    events: list = []
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp, events=events)
        sid = "s1"
        _session(cm, sid)
        cm.compress_context(sid, cm.load_conversation(sid))
        # New turns, so the second pass has a delta of its own to summarise.
        later = cm.load_conversation(sid)
        later.append(ConversationTurn(
            role="user", content="more " + "z" * 60, timestamp="2026-01-02T00:00:00",
        ))
        cm.save_conversation(sid, later, last_total_tokens=99_999)
        cm.compress_context(sid, cm.load_conversation(sid), forced=True)

        recorded = [
            e["attempt"]
            for m in _read_messages(cm, sid)
            for e in (m.get("meta") or {}).get("compressions", [])
        ]

    announced = [p["attempt"] for _, p in events if p["phase"] == "started"]
    assert [p["attempt"] for _, p in events if p["phase"] == "completed"] == announced
    # One id per attempt, all distinct, and the file names the very attempts the
    # stream announced.
    assert len(set(announced)) == 2
    assert recorded == announced


def test_marker_appends_across_rounds_and_is_capped() -> None:
    """One long tool chain compresses again and again: the trail accumulates on
    the turn that is still current, and the cap bounds it without pretending
    the history started there."""
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp)
        sid = "s1"
        _session(cm, sid)          # turns 0..5, the last assistant is turn 5
        rounds = _COMPRESSION_MARKER_CAP + 3
        for i in range(rounds):
            loaded = cm.load_conversation(sid)
            # Only user turns are appended, so the compression of every round
            # is recorded on the SAME assistant turn -- and every round still
            # has a delta to summarise.
            loaded.append(ConversationTurn(
                role="user", content="more " + "z" * 60,
                timestamp=f"2026-01-02T00:00:{i:02d}",
            ))
            cm.save_conversation(sid, loaded, last_total_tokens=99_999)
            cm.compress_context(sid, cm.load_conversation(sid), forced=True)

        saved = _read_messages(cm, sid)
        marked = saved[5]
        assert marked["role"] == "assistant"
        entries = marked["meta"]["compressions"]
        trimmed = marked["meta"].get("compressions_trimmed", 0)

    assert len(entries) == _COMPRESSION_MARKER_CAP
    assert trimmed == rounds - _COMPRESSION_MARKER_CAP
    # Oldest first, newest kept: the last round is always representable.
    starts = [e["delta_start"] for e in entries]
    assert starts == sorted(starts)
    assert all(e["turn_index"] == 5 for e in entries)


def test_marker_falls_back_to_the_last_message_without_an_assistant() -> None:
    """A restored session whose first request is oversized may not have an
    assistant turn yet; the trail is worth keeping regardless."""
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp)
        sid = "s1"
        os.makedirs(os.path.join(cm._chats_dir, sid), exist_ok=True)
        cm.save_conversation(
            sid,
            [
                ConversationTurn(role="user", content="q " + "y" * 200,
                                 timestamp="2026-01-01T00:00:00"),
                ConversationTurn(role="user", content="q2 " + "y" * 200,
                                 timestamp="2026-01-01T00:00:01"),
            ],
            last_total_tokens=9999,
        )
        cm.compress_context(sid, cm.load_conversation(sid), forced=True)

        saved = _read_messages(cm, sid)
        assert [m["role"] for m in saved] == ["user", "user"]
        entry = saved[-1]["meta"]["compressions"][0]
        assert entry["turn_index"] == len(saved) - 1


def test_marker_never_reaches_the_model() -> None:
    """``meta`` is persistence-only bookkeeping; the trail must not inflate or
    pollute the context of the next request."""
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp)
        sid = "s1"
        _session(cm, sid)
        cm.compress_context(sid, cm.load_conversation(sid))

        turns = cm.load_conversation(sid)
        assert turns[-1].meta and "compressions" in turns[-1].meta
        for turn in turns:
            assert "compressions" not in json.dumps(turn_to_message_dict(turn))
        context = json.dumps(cm.assemble_context(
            sid, [{"role": "user", "content": "next question"}]
        ))
        assert "compressions" not in context
        assert "summarized_up_to_turn_target" not in context


def test_marker_survives_a_later_persistence_of_the_session() -> None:
    """Append-only history: a later round must not erase what came before."""
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp)
        sid = "s1"
        _session(cm, sid)
        cm.compress_context(sid, cm.load_conversation(sid))
        before = _read_messages(cm, sid)

        # The next inference round persists on top of it (no compression).
        persist_conversation(
            context_manager=cm,
            session_id=sid,
            original_messages=[Message(role="user", content="next question",
                                       timestamp="2026-01-03T00:00:00")],
            collected_messages=[Message(role="assistant", content="answer",
                                        timestamp="2026-01-03T00:00:01")],
            compress=False,
        )
        after = _read_messages(cm, sid)

    # Turn 5 is the assistant turn the compression was recorded on.
    assert after[5]["meta"] == before[5]["meta"]
    assert len(after) == len(before) + 2


# ---------------------------------------------------------------------------
# Every trigger labels itself
# ---------------------------------------------------------------------------


def test_persist_conversation_labels_the_trigger_post_turn() -> None:
    events: list = []
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp, events=events)
        sid = "s1"
        os.makedirs(os.path.join(cm._chats_dir, sid), exist_ok=True)
        cm.save_conversation(sid, [], last_total_tokens=9_999)
        exc = persist_conversation(
            context_manager=cm,
            session_id=sid,
            original_messages=[Message(role="user", content="hello " + "y" * 80,
                                       timestamp="2026-01-01T00:00:00")],
            collected_messages=[
                Message(role="assistant", content="hi " + "y" * 80,
                        timestamp="2026-01-01T00:00:01"),
            ],
        )
        assert exc is None
    assert events and all(p["trigger"] == "post_turn" for _, p in events)


def test_in_loop_trigger_labels_itself() -> None:
    events: list = []
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp, events=events)
        sid = "s1"
        _session(cm, sid)
        turns = cm.load_conversation(sid)
        # 90 % of max_context is the in-loop threshold; a round below it must
        # not announce anything at all.
        assert cm.compress_context_in_loop(sid, turns, 10, max_context=200_000) is False
        assert events == []
        assert cm.compress_context_in_loop(sid, turns, 190_000, max_context=200_000) is True
    assert [p["phase"] for _, p in events] == ["started", "completed"]
    assert all(p["trigger"] == "in_loop" for _, p in events)


def test_forced_regeneration_labels_itself_manual() -> None:
    events: list = []
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp, events=events)
        sid = "s1"
        _session(cm, sid)
        cm.compress_context_forced(sid)
    assert [p["phase"] for _, p in events] == ["started", "completed"]
    assert events[0][1]["trigger"] == "manual"
    assert events[0][1]["forced"] is True


def test_pre_send_guard_labels_itself_and_marks_the_deep_pass() -> None:
    events: list = []
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp, events=events)
        sid = "s1"
        _session(cm, sid)
        cm.compress_context(
            sid, cm.load_conversation(sid),
            last_total_tokens=200_000, trigger_threshold=100, deep=True,
            trigger="pre_send",
        )
    assert events[0][1]["trigger"] == "pre_send"
    assert events[0][1]["deep"] is True


def test_unknown_trigger_degrades_to_auto() -> None:
    events: list = []
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp, events=events)
        sid = "s1"
        _session(cm, sid)
        cm.compress_context(sid, cm.load_conversation(sid), trigger="typo")
    assert events[0][1]["trigger"] == "auto"


# ---------------------------------------------------------------------------
# The SSE transport: which channel carries the frame
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_state():
    """Keep the module-level broker / event registries from leaking."""
    with server_state._session_stream_lock:
        server_state._session_streams.clear()
    with server_state._session_state_lock:
        server_state._session_event_subscribers.clear()
    common.clear_request_context(["sse_callback", "session_id"])
    yield
    with server_state._session_stream_lock:
        server_state._session_streams.clear()
    with server_state._session_state_lock:
        server_state._session_event_subscribers.clear()
    common.clear_request_context(["sse_callback", "session_id"])


def test_frame_shape_is_a_status_control_frame() -> None:
    frame = server_state.compression_stream_frame(
        "s1", {"phase": "started", "trigger": "in_loop"}
    )
    assert frame["role"] == "status"
    assert frame["status"] == "context_compression"
    assert frame["session_id"] == "s1"
    assert frame["phase"] == "started"
    assert frame["trigger"] == "in_loop"


def test_starter_callback_of_the_owning_session_carries_the_frame() -> None:
    written: list = []
    common.set_request_context(
        sse_callback=lambda frame, event=None: written.append(frame) or True,
        session_id="s1",
    )
    server_state.broadcast_compression_event("s1", {"phase": "started"})
    assert written and written[0]["session_id"] == "s1"
    # Nothing else was needed: no broker was invented, no global broadcast.
    assert "s1" not in server_state._session_streams


def test_a_callback_belonging_to_another_session_is_not_used() -> None:
    """The delegate / talk_to case: a nested inference keeps the PARENT's SSE
    callback while its thread-local session is the sub-session.  The
    sub-session's compression must not be announced on the parent's stream."""
    written: list = []
    common.set_request_context(
        sse_callback=lambda frame, event=None: written.append(frame) or True,
        session_id="parent-session",
    )
    server_state.broadcast_compression_event("parent-session-sub_1", {"phase": "started"})
    assert written == []


def test_a_live_generation_receives_the_frame_through_its_broker() -> None:
    frames: list = []

    def _send(envelope) -> bool:
        if envelope:
            frames.append(envelope["frame"])
        return True

    server_state.begin_session_stream("s1", threading.Event())
    with server_state._session_stream_lock:
        server_state._session_streams["s1"]["subscribers"].append(_send)
    server_state.broadcast_compression_event("s1", {"phase": "completed", "success": True})
    assert frames == [{"role": "status", "source": "system",
                       "status": "context_compression", "session_id": "s1",
                       "phase": "completed", "success": True}]


def test_a_session_without_a_generation_never_invents_one() -> None:
    """A manual regeneration of an idle session has no broker to write into:
    creating one would report a starter that never connected and retain frames
    for a generation that does not exist."""
    server_state.broadcast_compression_event("idle-session", {"phase": "started"})
    assert "idle-session" not in server_state._session_streams


def test_an_idle_session_falls_back_to_the_global_events_channel() -> None:
    received: list = []
    server_state._session_event_subscribers.append(
        lambda frame: received.append(frame) or True
    )
    server_state.broadcast_compression_event("idle-session", {"phase": "started"})
    assert len(received) == 1
    assert '"event": "compression"' in received[0]
    assert '"phase": "started"' in received[0]


def test_broker_without_subscribers_still_gets_the_retained_frame() -> None:
    """Replay matters: a tab that opens the session mid-generation reads the
    retained frames, and its indicator depends on the pair being there."""
    server_state.begin_session_stream("s1", threading.Event())
    server_state.broadcast_compression_event("s1", {"phase": "started"})
    server_state.broadcast_compression_event("s1", {"phase": "completed", "success": True})
    with server_state._session_stream_lock:
        frames = list(server_state._session_streams["s1"]["frames"])
    assert [f["frame"]["phase"] for f in frames] == ["started", "completed"]
    # Broker sequences start at 1, and the pair keeps its order: a replaying
    # tab sees "started" before the "completed" that closes it.
    assert [f["seq"] for f in frames] == [1, 2]


# ---------------------------------------------------------------------------
# End to end: the production wiring, minus the HTTP layer
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _live_server(tmp_path, monkeypatch):
    """A real ``RuntimeHTTPServer`` with a stubbed inference pipeline.

    ``POST /v1/infer/stream`` runs the production handler -- SSE writing,
    incremental persistence and the post-turn compression included -- while the
    model itself is stubbed: the streamed round just reports a large usage, and
    the compression call answers with a valid ``<summary>``/``<memory>`` block.
    """
    monkeypatch.setenv("SUMMARY_MODEL_ID", "summary-model")
    monkeypatch.setenv("MAX_TOKENS_IN_CONTEXT", "100")
    for target, value in (
        ("runtime.server._MODELS_PATH", "models.json"),
        ("runtime.server._TOOLS_PATH", "tools.json"),
        ("runtime.server._PROMPT_TEMPLATES_PATH", "prompt_templates.json"),
        ("runtime.server._DATA_DIR", "."),
    ):
        monkeypatch.setattr(target, str(tmp_path / value), raising=False)

    model_reg = ModelRegistry()
    model_reg.register(ModelConfig(
        model_id="stub-model", api_base="http://localhost:1",
        model_name="stub", api_protocol="openai",
    ))
    model_reg.register(ModelConfig(
        model_id="summary-model", api_base="http://localhost:1",
        model_name="summary", api_protocol="openai",
    ))
    runtime = Runtime(model_reg, ToolRegistry())

    def _infer_stream(request, **_pipeline_kwargs):
        for msg in (request.messages or []):
            yield msg
        yield Message(role="assistant", content="the answer")
        yield Message(role="usage", content=json.dumps({
            "prompt_tokens": 4000, "completion_tokens": 1000,
            "total_tokens": 5000,
        }))

    runtime.infer_stream = _infer_stream
    # ContextManager is constructed with ``runtime.infer`` bound into it, so the
    # compression stub has to be in place before the server is built.  A real
    # InferenceResult (not a namespace) because the title generator shares this
    # call path and reads ``.messages``.
    runtime.infer = lambda request: InferenceResult(
        success=True, messages=[Message(role="assistant", content=_OK_OUTPUT)]
    )
    srv = RuntimeHTTPServer(runtime, chats_dir=str(tmp_path / "chats"))
    srv.start_background(host="127.0.0.1", port=0)
    try:
        yield srv
    finally:
        srv.stop()


def _sse_data_lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("data: ") and line.strip() != "data: [DONE]":
            try:
                out.append(json.loads(line[6:]))
            except ValueError:
                pass
    return out


def test_the_pair_is_delivered_over_the_real_sse_response(tmp_path, monkeypatch) -> None:
    """A streamed inference whose round crosses the compression threshold puts
    both events on the very response the client is reading, and leaves the
    audit entry on the assistant turn in ``conversation.json``."""
    with _live_server(tmp_path, monkeypatch) as srv:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.port}/v1/infer/stream",
            data=json.dumps({
                "model_id": "stub-model",
                "session_id": "new",
                "messages": [{"role": "user", "content": "hello " + "y" * 200}],
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")

        frames = _sse_data_lines(body)
        pair = [f for f in frames if f.get("status") == "context_compression"]
        assert [f["phase"] for f in pair] == ["started", "completed"]
        assert pair[1]["success"] is True
        assert pair[1]["trigger"] == "post_turn"
        # [DONE] closes the turn, so it must not precede the compression this
        # turn triggered: a client that stops reading at [DONE] (a TUI, an API
        # caller, or a web tab whose frame-channel handoff failed) would
        # otherwise never learn that the context was rewritten.
        assert body.index('"phase": "completed"') < body.index("data: [DONE]")
        # The compression is a control frame: no message of the conversation is
        # fabricated out of it.
        assert all(f.get("role") != "assistant" or "status" not in f for f in frames)

        sessions = [
            d for d in (tmp_path / "chats").iterdir()
            if (d / "conversation.json").is_file()
        ]
        assert len(sessions) == 1
        with open(sessions[0] / "conversation.json", encoding="utf-8") as fh:
            saved = json.load(fh)["messages"]
        assert any(
            (m.get("meta") or {}).get("compressions") for m in saved
        ), "the assistant turn carries no compression audit entry"


def test_compression_wired_to_the_real_sink_lands_on_the_session_stream() -> None:
    """``Server`` installs :func:`broadcast_compression_event` as the ContextManager's
    sink; with a generation live, a real compression therefore leaves its pair
    in the session's retained frames, ordered against the assistant frames
    around it."""
    with tempfile.TemporaryDirectory() as tmp:
        cm = _make_cm(tmp)
        cm.set_compression_event_sink(server_state.broadcast_compression_event)
        sid = "session-e2e"
        _session(cm, sid)

        cancel = threading.Event()
        server_state.begin_session_stream(sid, cancel)
        server_state.publish_session_stream_frame(sid, {"role": "assistant", "content": "part"})
        assert cm.compress_context_in_loop(
            sid, cm.load_conversation(sid), 190_000,
            max_context=200_000, inference_model_id="big-model",
        ) is True
        server_state.publish_session_stream_frame(sid, {"role": "assistant", "content": "rest"})

        with server_state._session_stream_lock:
            frames = [e["frame"] for e in server_state._session_streams[sid]["frames"]]
        # And the same attempt left its audit entry behind on disk.
        entry = _read_messages(cm, sid)[-1]["meta"]["compressions"][0]

    assert [f.get("phase") for f in frames] == [None, "started", "completed", None]
    assert [f["content"] for f in frames if f.get("role") == "assistant"] == ["part", "rest"]
    completed = frames[2]
    assert completed["role"] == "status" and completed["status"] == "context_compression"
    assert completed["trigger"] == "in_loop" and completed["success"] is True
    assert entry["trigger"] == "in_loop" and entry["success"] is True
