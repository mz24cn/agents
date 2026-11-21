"""A session that is inference is closed to another request -- streamed or not.

The user's mental model is simple: pressing send starts a run, and the only ways
to end one are the things that look like ending one -- the stop button, pause,
letting it finish, or restarting agentsvc. Until now a second request was an
unlisted third way. ``begin_session_stream`` replaced the session's broker, which
is the single ``active_infers`` slot the session has, so the run it displaced
lost the only handle ``/v1/infer/abort`` reaches it through: its frames were
refused and its status overwritten -- it looked finished -- while it kept calling
the model and executing tools, mutating the workspace, for its whole remaining
round budget. No button in any browser could stop it.

So the backend answers a request that reached a running session instead of
letting it through, and it does so for both entry points alike. They share one
pipeline (``POST /v1/infer`` passes a buffered sink to ``_handle_infer_stream``),
which is exactly why the rule has to be one rule: an agent driving a session
through the non-streaming API is as capable of silently killing a browser's run
as a second tab of the same conversation is.

These tests drive the real HTTP handler: a real streamed generation, then a real
second request of each kind against it -- streamed, buffered, and a ``resume``
(the README once exempted a resume from this rule; a resume against a live
generation is the very takeover being prevented, so it is refused too), and one
after the generation ended to prove the gate is not a lock.
"""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from runtime import server_state

# Reuse the real-server harness (RuntimeHTTPServer + scripted model API) and the
# streamed-generation helpers built by the handoff tests.
from tests.test_incremental_persist_and_continue import (
    _get_session,
    _patch_model_urlopen,
    _url,
    continue_server,
)
from tests.test_infer_stream_handoff import (
    ANSWER_TEXT,
    _model_urlopen,
    _start_generation,
    _wait_for_stream_entry,
)


@pytest.fixture(autouse=True)
def clean_stream_and_channel_state():
    def _reset():
        server_state.frame_channel_reset()
        with server_state._session_stream_lock:
            server_state._session_streams.clear()
        with server_state._session_state_lock:
            server_state._session_statuses.clear()
            server_state._unread_sessions.clear()
            server_state._session_event_subscribers.clear()

    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post(server, path, payload):
    """POST and report the status plus decoded body, including error responses."""
    req = urllib.request.Request(
        _url(server, path),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    else:
        status = resp.status if resp.status else 200
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, {"raw": raw}


def _second_attempt_body(session_id):
    return {
        "session_id": session_id,
        "model_id": "test-model",
        "tool_ids": [],
        "messages": [{"role": "user", "content": "second attempt at the same session"}],
    }


def _broker(session_id):
    with server_state._session_stream_lock:
        stream = server_state._session_streams.get(session_id)
        return dict(stream) if stream is not None else None


def _wait_until_done(session_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        stream = _broker(session_id)
        if stream is not None and stream.get("done"):
            return stream
        time.sleep(0.05)
    raise AssertionError(f"generation for {session_id} never finished")


def _retained_content(stream):
    parts = []
    for envelope in stream.get("frames", []):
        frame = envelope.get("frame") or {}
        if isinstance(frame.get("content"), str):
            parts.append(frame["content"])
    return "".join(parts)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_a_streamed_request_reaching_a_running_session_is_refused(continue_server):
    """409 for a second tab, and the run it reached is not touched."""
    srv = continue_server
    with _patch_model_urlopen(srv, _model_urlopen):
        _poster, session_id = _start_generation(srv)
        incumbent = _wait_for_stream_entry(session_id)["cancel_event"]

        status, payload = _post(srv, "/v1/infer/stream", _second_attempt_body(session_id))

        assert status == 409, f"expected a refusal, got {status}: {payload}"
        assert payload.get("code") == "session_inferring", payload
        assert payload.get("session_id") == session_id
        assert not incumbent.is_set(), "refusing must not stop the run it reached"
        assert _broker(session_id)["cancel_event"] is incumbent, (
            "the session still belongs to the generation that was already running"
        )

        finished = _wait_until_done(session_id)
        assert ANSWER_TEXT in _retained_content(finished), (
            "the generation that was reached plays out exactly as if nothing arrived"
        )


def test_a_non_streamed_request_reaching_a_running_session_is_refused(continue_server):
    """The buffered API is held to the same rule as the browser.

    ``POST /v1/infer`` runs the very same pipeline with a buffered sink, so it
    would otherwise be a way for an agent -- or a script -- to displace a run a
    browser is watching and leave it unstoppable.
    """
    srv = continue_server
    with _patch_model_urlopen(srv, _model_urlopen):
        _poster, session_id = _start_generation(srv)
        incumbent = _wait_for_stream_entry(session_id)["cancel_event"]

        status, payload = _post(srv, "/v1/infer", _second_attempt_body(session_id))

        assert status == 409, f"expected a refusal, got {status}: {payload}"
        assert payload.get("code") == "session_inferring", payload
        assert not incumbent.is_set()
        assert _broker(session_id)["cancel_event"] is incumbent

        finished = _wait_until_done(session_id)
        assert ANSWER_TEXT in _retained_content(finished)


def test_a_resume_reaching_a_running_session_is_refused_too(continue_server):
    """``resume`` is not a spare key into a session that is already running.

    The README once promised that a request reaching a running session was
    refused *except* a resume.  A resume against a live generation is precisely
    the takeover this gate exists to stop: it would take the session's single
    broker and ``active_infers`` slot, and the run already in flight would go
    unreachable to stop/pause while it kept calling the model.  A resume is
    addressed to a resting session -- one whose pause settled, or a run left
    behind by a killed process (whose marker the next instance closes) -- and
    that is exactly what the gate lets through.
    """
    srv = continue_server
    with _patch_model_urlopen(srv, _model_urlopen):
        _poster, session_id = _start_generation(srv)
        incumbent = _wait_for_stream_entry(session_id)["cancel_event"]

        status, payload = _post(srv, "/v1/infer/stream", {
            "session_id": session_id,
            "model_id": "test-model",
            "tool_ids": [],
            "messages": [],
            "resume": True,
        })

        assert status == 409, f"expected a refusal, got {status}: {payload}"
        assert payload.get("code") == "session_inferring", payload
        assert not incumbent.is_set(), "refusing a resume must not stop the run it reached"
        assert _broker(session_id)["cancel_event"] is incumbent, (
            "the session still belongs to the generation that was already running"
        )

        finished = _wait_until_done(session_id)
        assert ANSWER_TEXT in _retained_content(finished), (
            "a refused resume leaves the running turn untouched"
        )


def test_a_request_after_the_generation_ended_is_accepted(continue_server):
    """The gate protects a run, it does not lock a session."""
    srv = continue_server
    with _patch_model_urlopen(srv, _model_urlopen):
        _poster, session_id = _start_generation(srv)
        _wait_until_done(session_id)

        status, payload = _post(srv, "/v1/infer", _second_attempt_body(session_id))

        assert status == 200, f"a resting session must accept a new turn: {payload}"
        assert payload.get("success") is True, payload

    code, session = _get_session(srv, session_id)
    assert code == 200
    assistant_turns = [m for m in session["messages"] if m.get("role") == "assistant"]
    assert len(assistant_turns) == 2, "the new turn belongs to the same conversation"


def test_a_stop_requested_run_does_not_hold_the_gate_shut(continue_server):
    """Asking a run to stop must not strand the session behind the gate.

    A generation whose cancel event is set is on its way out; refusing requests
    over it would leave a session that looks stopped with no way forward until
    agentsvc restarted.
    """
    srv = continue_server
    with _patch_model_urlopen(srv, _model_urlopen):
        _poster, session_id = _start_generation(srv)
        _wait_until_done(session_id)

        # A new turn started and the user pressed stop: the run is unwinding.
        stopping = threading.Event()
        server_state.begin_session_stream(session_id, stopping)
        stopping.set()
        assert not server_state.session_stream_is_live(session_id)

        status, payload = _post(srv, "/v1/infer", _second_attempt_body(session_id))

        assert status != 409, f"a stopping run must not hold the gate: {payload}"
