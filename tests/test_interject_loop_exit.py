"""Interjections that race the end of an inference generation.

The rule the backend keeps:

* while the tool loop is still running — even at the very instant it would
  have ended, because the model produced no tool call — a user message that
  arrived forbids that ending.  The answer is not the last word: the same
  generation runs one more round, whose round boundary injects the message
  exactly like any other interjection.
* once the loop has left the picture, re-entering it is not on offer, so the
  message is not a successful submission: the endpoint refuses it
  (``not_active``) and the client re-sends it as a normal turn.

Both halves are decided under one lock, so a message is never accepted into a
buffer that no boundary will ever drain (the case where every browser showed
the bubble while the conversation never gained the turn).
"""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from runtime import server_state
from runtime.server_state import (
    begin_session_stream,
    finish_session_stream,
    get_infer_exit_reason,
    queue_infer_interjection,
    request_pause_infer,
    take_infer_boundary_action,
    try_close_infer_loop,
)

# Reuse the scripted-model server harness from the continue/persistence tests.
from tests.test_incremental_persist_and_continue import (
    _get_session,
    _make_resp,
    _openai_sse_text,
    _post_json,
    _post_stream,
    continue_server,
)
from tests.test_infer_pause_resume import _StubInterjectHandler


@pytest.fixture(autouse=True)
def clean_session_stream_state():
    with server_state._session_stream_lock:
        server_state._session_streams.clear()
        server_state._flight_sessions.clear()
    with server_state._session_state_lock:
        server_state._session_statuses.clear()
        server_state._unread_sessions.clear()
        server_state._session_event_subscribers.clear()
    yield
    with server_state._session_stream_lock:
        server_state._session_streams.clear()
        server_state._flight_sessions.clear()
    with server_state._session_state_lock:
        server_state._session_statuses.clear()
        server_state._unread_sessions.clear()
        server_state._session_event_subscribers.clear()


# ---------------------------------------------------------------------------
# 1. The decision itself (server_state, no HTTP)
# ---------------------------------------------------------------------------


def test_empty_buffer_closes_the_generation_to_late_interjections():
    """With nothing buffered the loop may finish, and finishing is what closes
    the queue: a later interjection comes back as "closing" instead of being
    parked past the last boundary."""
    cancel_event = threading.Event()
    begin_session_stream("s1", cancel_event)

    assert try_close_infer_loop("s1", cancel_event) is True
    assert queue_infer_interjection(
        "s1", cancel_event, {"role": "user", "content": "too late"}
    ) == "closing"
    # Idempotent: closing does not accumulate state.
    assert try_close_infer_loop("s1", cancel_event) is True


def test_buffered_message_forbids_the_loop_from_finishing():
    """A queued turn outranks the loop's own end: the message must be reasoned
    on, so the gate refuses to close and leaves the buffer for that next
    round's boundary to consume."""
    cancel_event = threading.Event()
    begin_session_stream("s1", cancel_event)

    assert queue_infer_interjection(
        "s1", cancel_event, {"role": "user", "content": "one"}
    ) == "queued"
    assert try_close_infer_loop("s1", cancel_event) is False

    # Still fully open while the loop is alive: more input is accepted, and a
    # pause arriving now loses to the buffered input rather than stopping it.
    assert request_pause_infer("s1", cancel_event) is True
    assert queue_infer_interjection(
        "s1", cancel_event, {"role": "user", "content": "two"}
    ) == "queued"
    action = take_infer_boundary_action("s1", cancel_event)
    assert [m["content"] for m in action["append"]] == ["one", "two"]
    assert action["stop"] is False

    # Buffer empty: now the loop may end, and the next message is refused.
    assert try_close_infer_loop("s1", cancel_event) is True
    assert queue_infer_interjection(
        "s1", cancel_event, {"role": "user", "content": "three"}
    ) == "closing"


def test_completed_answer_drops_a_pause_it_makes_moot():
    """A soft-pause still pending when the answer completed has nothing left to
    pause: it is dropped instead of being handed out as a "paused" exit at a
    round boundary that will never be reached."""
    cancel_event = threading.Event()
    begin_session_stream("s1", cancel_event)

    assert request_pause_infer("s1", cancel_event) is True
    assert try_close_infer_loop("s1", cancel_event) is True
    assert take_infer_boundary_action("s1", cancel_event) is None
    assert get_infer_exit_reason("s1", cancel_event) is None


def test_gates_are_generation_scoped():
    """Closing belongs to one generation: a replacement generation on the same
    session starts with an open queue again."""
    first = threading.Event()
    begin_session_stream("s1", first)
    assert try_close_infer_loop("s1", first) is True
    assert queue_infer_interjection(
        "s1", first, {"role": "user", "content": "x"}
    ) == "closing"

    second = threading.Event()
    begin_session_stream("s1", second)
    assert queue_infer_interjection(
        "s1", second, {"role": "user", "content": "fresh"}
    ) == "queued"
    # The retired generation cannot close the live one: its gate is a no-op
    # here, so input keeps being accepted.
    assert try_close_infer_loop("s1", first) is True
    assert queue_infer_interjection(
        "s1", second, {"role": "user", "content": "again"}
    ) == "queued"
    # Only its own gate can close it, and only once its buffer is consumed.
    assert try_close_infer_loop("s1", second) is False
    action = take_infer_boundary_action("s1", second)
    assert [m["content"] for m in action["append"]] == ["fresh", "again"]
    assert try_close_infer_loop("s1", second) is True
    assert queue_infer_interjection(
        "s1", second, {"role": "user", "content": "late"}
    ) == "closing"


# ---------------------------------------------------------------------------
# 2. The endpoint hands a refused message back to the client
# ---------------------------------------------------------------------------


def test_refused_interjection_is_answered_not_active_and_never_echoed():
    """A message that arrives after the loop closed must not be shown as if it
    had been taken: the response says not_active (the client then sends a
    normal turn) and nothing is broadcast to the session stream."""
    cancel_event = threading.Event()
    begin_session_stream("s1", cancel_event)
    assert try_close_infer_loop("s1", cancel_event) is True

    handler = _StubInterjectHandler({
        "session_id": "s1",
        "message": {
            "role": "user", "content": "too late",
            "timestamp": "2026-01-01 00:00:00",
        },
    })
    handler.server.active_infers = {"s1": cancel_event}

    handler._handle_infer_interject()

    assert not handler.errors
    assert handler.responses == [
        (200, {"ok": True, "status": "not_active", "session_id": "s1"}),
    ]
    with server_state._session_stream_lock:
        frames = [env["frame"] for env in server_state._session_streams["s1"]["frames"]]
    assert not any(f.get("content") == "too late" for f in frames)


# ---------------------------------------------------------------------------
# 3. End to end: the loop is still running, so it must not end
# ---------------------------------------------------------------------------


def _infer_stream(srv, session_id, side_effect):
    """POST /v1/infer/stream with the scripted model; returns the SSE body."""
    status, body = _post_stream(srv, {
        "session_id": session_id,
        "model_id": "test-model",
        "tool_ids": ["echo"],
        "messages": [{"role": "user", "content": "go"}],
        "stream": True,
    }, side_effect)
    assert status == 200, body
    return body


def _user_frames(body: str) -> list[dict]:
    frames = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            frame = json.loads(line[len("data:"):].strip())
        except ValueError:
            continue
        if isinstance(frame, dict) and frame.get("role") == "user":
            frames.append(frame)
    return frames


def test_interjection_during_the_closing_round_extends_that_generation(continue_server):
    """The message landed while the model's closing answer was streaming, so
    the generation is not over: it runs a second round that carries it, instead
    of the answer staying the last word and the message vanishing.

    Deterministic by construction — the interjection is POSTed from inside the
    scripted handler of the closing round, the latest point at which the loop
    can still be running, so the loop-exit gate is the only thing that decides.
    """
    srv = continue_server
    session_id = srv._context_manager.create_session()
    model_bodies: list[dict] = []
    accepted: dict = {}

    def side_effect(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        model_bodies.append(body)
        if len(model_bodies) > 1:
            return _make_resp(_openai_sse_text("Considered the insertion."))
        # The answer of the only round so far is being produced; the user
        # types while it streams.
        status, res = _post_json(srv, "/v1/infer/interject", {
            "session_id": session_id,
            "message": {"role": "user", "content": "also mind X"},
        })
        accepted.update(status=status, body=res)
        return _make_resp(_openai_sse_text("Closing answer."))

    stream_body = _infer_stream(srv, session_id, side_effect)

    assert accepted["status"] == 202, accepted
    assert accepted["body"].get("status") == "queued", accepted["body"]

    # Round 1 had no tool call, yet the loop did not end: round 2 exists and
    # already carries the insertion — round 1's request could not.
    assert len(model_bodies) == 2, "the loop finished despite buffered input"
    assert not any(
        "also mind X" in json.dumps(m) for m in model_bodies[0]["messages"]
    )
    assert any(
        "also mind X" in json.dumps(m) for m in model_bodies[1]["messages"]
    )

    # The turn joined the conversation in order, as a real user turn, and the
    # generation's last word is the answer that considered it.
    status, session = _get_session(srv, session_id)
    assert status == 200, session
    roles = [msg["role"] for msg in session["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"], session["messages"]
    assert session["messages"][2]["content"] == "also mind X"
    assert session["messages"][-1]["content"] == "Considered the insertion."

    # One user frame for it on this connection: the immediate echo goes to the
    # subscribing streams, the boundary injection is what the starter sees, and
    # the browser deduplicates them by the shared client timestamp — so exactly
    # one turn enters the conversation.
    assert sum(
        1 for f in _user_frames(stream_body) if f.get("content") == "also mind X"
    ) == 1


def test_interjection_after_the_generation_ended_is_returned(continue_server):
    """Out of the loop, re-entering is not on offer: the endpoint refuses the
    message so the client submits it as a normal turn — it is never a silent
    success whose text nothing reads."""
    srv = continue_server
    session_id = srv._context_manager.create_session()

    def side_effect(request, **kwargs):
        return _make_resp(_openai_sse_text("All done."))

    _infer_stream(srv, session_id, side_effect)

    status, body = _post_json(srv, "/v1/infer/interject", {
        "session_id": session_id,
        "message": {"role": "user", "content": "one more thing"},
    })
    assert status == 200, body
    assert body.get("status") == "not_active", body

    _, session = _get_session(srv, session_id)
    assert [msg["role"] for msg in session["messages"]] == ["user", "assistant"]
    assert not any(
        msg["content"] == "one more thing" for msg in session["messages"]
    )
