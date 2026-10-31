"""End-to-end tests for the POST → tab frame-channel handoff.

Why this exists: web/src/lib/api.js releases the ``POST /v1/infer/stream``
connection as soon as the backend confirms the session is carried by the tab's
frame channel, so the browser can watch a session it started with one shared
connection instead of two. That release looks exactly like a browser close from
the server side, and the broker's "a non-flight inference nobody is watching is
an orphan" rule used to fire on it: the short gap between closing the POST and
registering the channel subscription cancelled healthy generations, which the
user saw as ``Error: user interrupted.`` without ever pressing stop.

These tests drive the real HTTP handler: a real POST closed mid-generation, a
real frame channel with replay, and a scripted model whose tokens arrive slowly
enough for the handoff race to be observable.
"""

import http.client
import json
import socket
import threading
import time
import urllib.request
from unittest.mock import MagicMock

import pytest

from runtime import server_state

# Reuse the real-server harness (RuntimeHTTPServer + scripted model API) from
# the persistence tests; test_infer_pause_resume.py imports from here too.
from tests.test_incremental_persist_and_continue import (
    _get_session,
    _patch_model_urlopen,
    _url,
    continue_server,
)

ANSWER_CHUNKS = ["alpha ", "beta ", "gamma ", "delta"]
ANSWER_TEXT = "".join(ANSWER_CHUNKS)



@pytest.fixture(autouse=True)
def clean_stream_and_channel_state():
    def _reset():
        server_state.frame_channel_reset()
        with server_state._session_stream_lock:
            server_state._session_streams.clear()
            server_state._flight_sessions.clear()
        with server_state._session_state_lock:
            server_state._session_statuses.clear()
            server_state._unread_sessions.clear()
            server_state._session_event_subscribers.clear()

    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# Fakes: a model that streams slowly enough for a handoff to happen mid-flight
# ---------------------------------------------------------------------------


def _slow_model_resp(chunks, delay):
    """A streaming OpenAI SSE response that yields one chunk per `delay`."""
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"role": "assistant", "content": chunk}}]}) + "\n\n"
        for chunk in chunks
    ]
    lines.append("data: " + json.dumps({
        "choices": [],
        "usage": {"prompt_tokens": 10, "completion_tokens": len(chunks), "total_tokens": 10 + len(chunks)},
    }) + "\n\n")
    lines.append("data: [DONE]\n\n")
    encoded = [line.encode("utf-8") for line in lines]
    body = "".join(lines).encode("utf-8")

    resp = MagicMock()
    resp.status = 200

    def _iter(self):
        for raw in encoded:
            time.sleep(delay)
            yield raw

    resp.__iter__ = _iter
    resp.read = lambda *a, **k: body
    resp.close = MagicMock()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _model_urlopen(request, **kwargs):
    return _slow_model_resp(ANSWER_CHUNKS, delay=0.15)


# ---------------------------------------------------------------------------
# Clients: the two connections a browser tab holds during a handoff
# ---------------------------------------------------------------------------


class FrameChannel:
    """One tab's frame-channel SSE: reads every event pushed for this conn.

    Uses http.client rather than urllib so close() can shutdown() the socket
    directly. A urllib response's makefile stays blocked in readline() until the
    server notices the dead peer on its next keepalive, which would make the
    harness wait ~15s per test — and would not look like a browser abort anyway.
    """

    def __init__(self, server, conn_id):
        self._events = []
        self._closed = threading.Event()
        self._conn_id = conn_id
        self._conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=20)
        self._conn.request("GET", f"/v1/sessions/frames?conn={conn_id}",
                           headers={"Accept": "text/event-stream"})
        self._resp = self._conn.getresponse()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        assert self.wait_for(lambda e: e.get("kind") == "chan_init", timeout=5), (
            "the frame channel never confirmed chan_init"
        )

    def _pump(self):
        event_name = None
        try:
            while not self._closed.is_set():
                raw = self._resp.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if text.startswith("event: "):
                    event_name = text[7:].strip()
                elif text.startswith("data: "):
                    try:
                        payload = json.loads(text[6:])
                    except json.JSONDecodeError:
                        event_name = None
                        continue
                    # `event:` names channel control (chan_init / sub_init / done);
                    # inference frames travel as `event: frame` with the original
                    # stream event name inside the payload.
                    payload["kind"] = event_name or "frame"
                    self._events.append(payload)
                    event_name = None
        except OSError:
            # close() shuts the socket down while readline() is parked.
            pass

    def wait_for(self, predicate, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for event in list(self._events):
                if predicate(event):
                    return event
            time.sleep(0.05)
        return None

    def close(self):
        """Drop the tab's channel the way a browser does: kill the socket.

        Reading the response to EOF is not an option — the server only notices a
        vanished client when it next writes, which for an idle channel is the
        keepalive up to 15s later. shutdown() wakes our own reader immediately
        and makes the server's next write fail.
        """
        self._closed.set()
        try:
            if self._conn.sock is not None:
                self._conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._conn.close()
        except Exception:
            pass
        self._thread.join(timeout=2.0)


class StreamPoster(threading.Thread):
    """POSTs /v1/infer/stream, waits for the ``init`` frame, then closes the
    connection — exactly what inferStream() does once the handoff is confirmed."""

    def __init__(self, server, payload):
        super().__init__(daemon=True)
        self._server = server
        self._payload = payload
        self.session_id = None
        self.init_seen = threading.Event()
        self.closed = threading.Event()
        self.error = None

    def run(self):
        conn = http.client.HTTPConnection("127.0.0.1", self._server.port, timeout=20)
        try:
            conn.request(
                "POST",
                "/v1/infer/stream",
                body=json.dumps(self._payload),
                headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            )
            resp = conn.getresponse()
            while True:
                line = resp.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text.startswith("data: "):
                    continue
                try:
                    event = json.loads(text[6:])
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "init":
                    self.session_id = event.get("session_id")
                    self.init_seen.set()
                    break
            # Handoff: this tab no longer needs the POST, and the backend said
            # the frames may be fetched over the channel.
            conn.close()
        except Exception as exc:  # pragma: no cover - surfaced through assertions
            self.error = exc
            self.init_seen.set()
        finally:
            self.closed.set()

    def wait(self, timeout=15.0):
        assert self.init_seen.wait(timeout=timeout), "no init frame from infer/stream"
        assert self.error is None, f"POST failed: {self.error}"
        assert self.session_id, "init frame carried no session_id"
        return self.session_id

    def wait_closed(self, timeout=15.0):
        assert self.closed.wait(timeout=timeout), "the POST was never released"


def _subscribe(server, conn_id, session_id, after=-1):
    req = urllib.request.Request(
        _url(server, "/v1/sessions/frames/subscribe"),
        data=json.dumps({"conn": conn_id, "session_id": session_id, "after": after}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _wait_for_stream_entry(session_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with server_state._session_stream_lock:
            stream = server_state._session_streams.get(session_id)
            if stream is not None:
                return dict(stream)
        time.sleep(0.02)
    raise AssertionError(f"no broker state for {session_id}")


def _stream_field(session_id, field):
    with server_state._session_stream_lock:
        stream = server_state._session_streams.get(session_id)
        return None if stream is None else stream.get(field)


def _wait_until(predicate, timeout=10.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def _start_generation(server):
    """Kick off an inference and return (poster, session_id, stream entry)."""
    poster = StreamPoster(server, {
        "session_id": "new",
        "model_id": "test-model",
        "tool_ids": [],
        "messages": [{"role": "user", "content": "count to four"}],
    })
    poster.start()
    session_id = poster.wait()
    return poster, session_id


def _content_seen(events):
    parts = []
    for event in events:
        if event.get("kind") != "frame":
            continue
        frame = event.get("frame") or {}
        content = frame.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "".join(parts)


def _errors_seen(events):
    texts = []
    for event in events:
        if event.get("kind") != "frame":
            continue
        frame = event.get("frame") or {}
        for key in ("content", "error", "message"):
            value = frame.get(key)
            if isinstance(value, str) and "Error:" in value:
                texts.append(value)
    return texts


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_handoff_abort_after_subscribe_does_not_cancel(continue_server):
    """The steady state: the channel watches before the POST is released.

    The starter disconnect must not arm any cancellation at all — the
    generation is observed.
    """
    srv = continue_server
    with _patch_model_urlopen(srv, _model_urlopen):
        channel = FrameChannel(srv, "tab-ordered")
        try:
            poster, session_id = _start_generation(srv)
            reply = _subscribe(srv, "tab-ordered", session_id)
            assert reply["registered"] is True
            assert channel.wait_for(lambda e: e.get("kind") == "sub_init", timeout=5), (
                "the subscribe command never confirmed the watch"
            )

            poster.wait_closed()
            _wait_until(
                lambda: _stream_field(session_id, "starter_connected") is False,
                what="the server to notice the released POST",
            )
            entry = _wait_for_stream_entry(session_id)
            assert entry["unobserved_timer"] is None, (
                "an observed generation must not be scheduled for cancellation"
            )
            assert entry["cancel_event"].wait(timeout=2.0) is False, (
                "the handoff must not cancel the generation"
            )

            assert channel.wait_for(lambda e: e.get("kind") == "done", timeout=20), (
                "the channel never saw the generation finish"
            )
        finally:
            channel.close()

    status, session = _get_session(srv, session_id)
    assert status == 200
    assistant_turns = [m for m in session["messages"] if m.get("role") == "assistant"]
    assert ANSWER_TEXT in (assistant_turns[-1].get("content") or "")


def test_channel_takeover_race_does_not_cancel_inference(continue_server, monkeypatch):
    """The reported regression, timed on purpose.

    The POST is released *before* the subscribe command lands, so for a moment
    the generation has no observer at all. That window used to be fatal; now it
    is a grace period, and a subscription that arrives inside it rescues the
    run.
    """
    srv = continue_server
    monkeypatch.setenv("SESSION_UNOBSERVED_GRACE_S", "5")
    with _patch_model_urlopen(srv, _model_urlopen):
        channel = FrameChannel(srv, "tab-racing")
        try:
            poster, session_id = _start_generation(srv)
            poster.wait_closed()  # starter gone before the subscribe command lands

            entry = _wait_for_stream_entry(session_id)
            _wait_until(
                lambda: _stream_field(session_id, "unobserved_timer") is not None,
                what="the unobserved cancellation to be scheduled",
            )
            time.sleep(0.4)  # the gap that used to be long enough to kill the run

            reply = _subscribe(srv, "tab-racing", session_id)
            assert reply["registered"] is True
            assert reply["active"] is True

            assert _stream_field(session_id, "unobserved_timer") is None, (
                "a returning watcher must disarm the pending cancellation"
            )
            assert entry["cancel_event"].wait(timeout=3.0) is False, (
                "a subscription that arrived inside the grace window must rescue the generation"
            )
            assert channel.wait_for(lambda e: e.get("kind") == "done", timeout=20), (
                "the rescued generation never finished on the channel"
            )
            events = list(channel._events)
            assert _errors_seen(events) == [], f"interrupted during handoff: {_errors_seen(events)}"
            assert ANSWER_CHUNKS[-1] in _content_seen(events), (
                "the channel never received the tail of the rescued answer"
            )
        finally:
            channel.close()

    status, session = _get_session(srv, session_id)
    assert status == 200
    assistant_turns = [m for m in session["messages"] if m.get("role") == "assistant"]
    assert ANSWER_TEXT in (assistant_turns[-1].get("content") or "")
    assert not any("user interrupted" in (m.get("content") or "") for m in assistant_turns)


def test_starter_released_with_no_watcher_is_still_cancelled(continue_server, monkeypatch):
    """The orphan rule survives: only the delay is new.

    A channel that is attached but subscribes to nothing proves the rule is not
    being softened into "any open connection counts" — the generation still
    goes, it just gets the grace window first.
    """
    srv = continue_server
    monkeypatch.setenv("SESSION_UNOBSERVED_GRACE_S", "0.5")
    with _patch_model_urlopen(srv, _model_urlopen):
        channel = FrameChannel(srv, "tab-idle")  # attached, zero subscriptions
        try:
            poster, session_id = _start_generation(srv)
            poster.wait_closed()
            _wait_until(
                lambda: _stream_field(session_id, "starter_connected") is False,
                what="the server to notice the released POST",
            )
            entry = _wait_for_stream_entry(session_id)
            assert not entry["subscribers"], (
                "an attached channel that watches no session must not count as an observer"
            )
            assert entry["cancel_event"].wait(timeout=5.0), (
                "a generation nobody watches must still be cancelled once the window passes"
            )
        finally:
            channel.close()


def test_zero_grace_restores_the_legacy_interruption(continue_server, monkeypatch):
    """What the window buys, stated as its opposite.

    With SESSION_UNOBSERVED_GRACE_S=0 the broker is back to the strict rule that
    produced the reported symptom: releasing the POST in the middle of the
    handoff — before the subscribe command lands — cancels a healthy generation
    the instant the server notices the broken pipe. Kept as a test so the escape
    hatch stays honest and the window is never removed by accident.
    """
    srv = continue_server
    monkeypatch.setenv("SESSION_UNOBSERVED_GRACE_S", "0")
    with _patch_model_urlopen(srv, _model_urlopen):
        channel = FrameChannel(srv, "tab-strict")
        try:
            poster, session_id = _start_generation(srv)
            poster.wait_closed()

            entry = _wait_for_stream_entry(session_id)
            _wait_until(
                lambda: entry["cancel_event"].is_set(),
                what="the legacy immediate cancellation",
            )
            assert _stream_field(session_id, "unobserved_timer") is None, (
                "with no window there is nothing to schedule"
            )
        finally:
            channel.close()
