"""Tests for the per-tab multiplexed session frame channel.

One browser tab watches every streaming session over a single SSE connection
(GET /v1/sessions/frames?conn=<tab id>), instead of one connection per session.
These tests cover both halves of the contract:

* the broker side  - a channel is one subscriber per watched session, so the
  "cancel the inference when nobody is watching" rule is unchanged;
* the HTTP side    - create-or-reuse by ``conn`` id, subscribe/unsubscribe
  commands, gap replay on re-attach.
"""

import json
import os
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer
from runtime import server_state


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    # These tests assert which connection counts as an observer. The window that
    # defers the cancellation (so a hand-off or a channel reconnect can rescue
    # the generation) is patched here to keep the policy assertions direct;
    # test_reattached_channel_rescues_a_pending_cancellation covers the window.
    monkeypatch.setenv("SESSION_UNOBSERVED_GRACE_S", "0")
    server_state.frame_channel_reset()
    with server_state._session_stream_lock:
        server_state._session_streams.clear()
        server_state._flight_sessions.clear()
    with server_state._session_state_lock:
        server_state._session_statuses.clear()
        server_state._unread_sessions.clear()
        server_state._session_event_subscribers.clear()
    yield
    server_state.frame_channel_reset()
    with server_state._session_stream_lock:
        server_state._session_streams.clear()
        server_state._flight_sessions.clear()
    with server_state._session_state_lock:
        server_state._session_statuses.clear()
        server_state._unread_sessions.clear()
        server_state._session_event_subscribers.clear()


# ---------------------------------------------------------------------------
# Broker level: connection accounting must not change cancellation policy
# ---------------------------------------------------------------------------


def test_channel_subscription_keeps_non_flight_inference_alive():
    cancel_event = threading.Event()
    server_state.begin_session_stream("session-1", cancel_event)

    registered, snapshot = server_state.frame_channel_subscribe("tab-a", "session-1")
    assert registered is True
    assert snapshot["active"] is True

    # The starter POST is gone; the channel is the only observer.
    assert server_state.disconnect_session_stream_starter("session-1") is False
    assert not cancel_event.is_set()

    assert server_state.frame_channel_unsubscribe("tab-a", "session-1") is True
    assert cancel_event.is_set()


def test_one_channel_observes_every_watched_session_with_one_connection():
    events_a, events_b = threading.Event(), threading.Event()
    server_state.begin_session_stream("session-a", events_a)
    server_state.begin_session_stream("session-b", events_b)

    server_state.frame_channel_subscribe("tab-a", "session-a")
    server_state.frame_channel_subscribe("tab-a", "session-b")

    snapshot = server_state.frame_channel_snapshot()
    assert list(snapshot) == ["tab-a"]
    assert snapshot["tab-a"]["sessions"] == ["session-a", "session-b"]

    with server_state._session_stream_lock:
        assert len(server_state._session_streams["session-a"]["subscribers"]) == 1
        assert len(server_state._session_streams["session-b"]["subscribers"]) == 1

    # Closing the tab releases both sessions. With the starter POST already
    # gone, both inferences become unobserved and are cancelled.
    assert server_state.disconnect_session_stream_starter("session-a") is False
    assert server_state.disconnect_session_stream_starter("session-b") is False
    assert not events_a.is_set() and not events_b.is_set()
    assert server_state.close_frame_channel("tab-a") is True
    assert events_a.is_set() and events_b.is_set()


def test_channel_receives_live_frames_for_every_session():
    channel = server_state.get_or_create_frame_channel("tab-a")
    server_state.begin_session_stream("session-a", threading.Event())
    server_state.begin_session_stream("session-b", threading.Event())
    server_state.frame_channel_subscribe("tab-a", "session-a")
    server_state.frame_channel_subscribe("tab-a", "session-b")

    server_state.publish_session_stream_frame("session-a", {"role": "assistant", "content": "a"})
    server_state.publish_session_stream_frame("session-b", {"role": "assistant", "content": "b"})

    items = []
    while True:
        try:
            items.append(channel["queue"].get_nowait())
        except Exception:
            break
        if len(items) >= 6:
            break

    kinds = [(item["type"], item.get("session_id")) for item in items]
    assert kinds == [
        ("sub_init", "session-a"),
        ("sub_init", "session-b"),
        ("frame", "session-a"),
        ("frame", "session-b"),
    ]
    assert items[2]["frame"]["content"] == "a"
    assert items[3]["frame"]["content"] == "b"


def test_detached_channel_grace_window_then_cancels_unobserved_inference():
    with patch.object(server_state, "FRAME_CHANNEL_GRACE_S", 0.2):
        cancel_event = threading.Event()
        server_state.begin_session_stream("session-1", cancel_event)
        server_state.frame_channel_subscribe("tab-a", "session-1")
        # The initiating POST is gone; the channel is the only observer.
        assert server_state.disconnect_session_stream_starter("session-1") is False

        # The tab's single connection dropped: reconnecting must not cancel.
        _channel, generation = server_state.attach_frame_channel("tab-a")
        assert server_state.detach_frame_channel("tab-a", generation) is True
        assert not cancel_event.is_set()

        # Re-attach with the same tab id reuses the channel.
        _channel2, generation2 = server_state.attach_frame_channel("tab-a")
        assert generation2 > generation
        assert server_state.frame_channel_snapshot()["tab-a"]["sessions"] == ["session-1"]
        assert server_state.detach_frame_channel("tab-a", generation2) is True

        # The tab is really gone: after the grace window the inference is
        # cancelled exactly as a dropped per-session stream would be.
        deadline = threading.Event()
        deadline.wait(0.5)
        assert cancel_event.is_set()


def test_reattached_channel_rescues_a_pending_cancellation():
    """A lost subscription inside the grace window must not kill the run.

    Real ordering: the POST releases itself, the tab's channel momentarily has no
    subscription for that session (a retried subscribe command), then re-registers
    a few hundred milliseconds later.
    """
    with patch.dict(os.environ, {"SESSION_UNOBSERVED_GRACE_S": "0.25"}):
        cancel_event = threading.Event()
        server_state.begin_session_stream("session-1", cancel_event)
        server_state.frame_channel_subscribe("tab-a", "session-1")
        assert server_state.disconnect_session_stream_starter("session-1") is False

        _channel, _generation = server_state.attach_frame_channel("tab-a")
        assert server_state.frame_channel_unsubscribe("tab-a", "session-1") is True
        assert not cancel_event.is_set()

        assert server_state.frame_channel_subscribe("tab-a", "session-1")[0] is True
        threading.Event().wait(0.4)
        assert not cancel_event.is_set()


def test_reattach_discards_stale_queue_and_replays_from_cursor():
    server_state.begin_session_stream("session-1", threading.Event())
    server_state.frame_channel_subscribe("tab-a", "session-1")
    server_state.publish_session_stream_frame("session-1", {"role": "assistant", "content": "1"})

    _channel, generation = server_state.attach_frame_channel("tab-a")
    server_state.detach_frame_channel("tab-a", generation)

    # Frames produced while detached stay queued, then a re-attach discards
    # them: the client re-subscribes with its cursor and the broker replays.
    server_state.publish_session_stream_frame("session-1", {"role": "assistant", "content": "2"})
    channel2, generation2 = server_state.attach_frame_channel("tab-a")
    assert channel2 is _channel
    assert channel2["queue"].qsize() == 1  # only the attach_flush wake-up marker

    registered, snapshot = server_state.frame_channel_subscribe("tab-a", "session-1", after_seq=1)
    assert registered is True
    assert [item["frame"]["content"] for item in snapshot["frames"]] == ["2"]
    assert generation2 > generation


# ---------------------------------------------------------------------------
# HTTP level: create-or-reuse per tab, subscribe commands, replay
# ---------------------------------------------------------------------------


@pytest.fixture()
def server(tmp_path):
    with patch("runtime.server._MODELS_PATH", str(tmp_path / "models.json")), \
         patch("runtime.server._TOOLS_PATH", str(tmp_path / "tools.json")), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", str(tmp_path / "prompt_templates.json")), \
         patch("runtime.server._DATA_DIR", str(tmp_path)):
        srv = RuntimeHTTPServer(Runtime(ModelRegistry(), ToolRegistry()))
        srv.start_background(host="127.0.0.1", port=0)
        yield srv
        srv.stop()


def _post(server, path, payload):
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _open_channel(server, conn):
    return urllib.request.urlopen(
        f"http://127.0.0.1:{server.port}/v1/sessions/frames?conn={conn}", timeout=5
    )


def _read_event(response, timeout=5.0):
    """Read one SSE block and return (event_name, payload)."""
    event, data = None, None
    while True:
        line = response.readline().decode("utf-8").rstrip("\n")
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
        elif not line:
            if data is None:
                event = None
                continue
            return event, data


def _read_frame(response):
    """Read SSE blocks until an inference frame arrives; return its payload."""
    while True:
        event, payload = _read_event(response)
        if event == "frame":
            return payload


def test_channel_carries_two_sessions_over_one_connection(server):
    server_state.begin_session_stream("session-a", threading.Event())
    server_state.begin_session_stream("session-b", threading.Event())

    response = _open_channel(server, "tab-http")
    try:
        event, init = _read_event(response)
        assert event == "chan_init" and init["conn"] == "tab-http"

        assert _post(server, "/v1/sessions/frames/subscribe",
                     {"conn": "tab-http", "session_id": "session-a", "after": -1})["registered"] is True
        assert _post(server, "/v1/sessions/frames/subscribe",
                     {"conn": "tab-http", "session_id": "session-b", "after": -1})["registered"] is True

        server_state.publish_session_stream_frame("session-a", {"role": "assistant", "content": "a"})
        server_state.publish_session_stream_frame("session-b", {"role": "assistant", "content": "b"})

        seen = []
        while len(seen) < 2:
            event, payload = _read_event(response)
            if event == "frame":
                seen.append((payload["session_id"], payload["frame"]["content"]))
        assert sorted(seen) == [("session-a", "a"), ("session-b", "b")]

        # One tab, one channel, one subscriber per session.
        snapshot = server_state.frame_channel_snapshot()
        assert snapshot["tab-http"]["sessions"] == ["session-a", "session-b"]
        with server_state._session_stream_lock:
            assert len(server_state._session_streams["session-a"]["subscribers"]) == 1
            assert len(server_state._session_streams["session-b"]["subscribers"]) == 1
    finally:
        response.close()


def test_second_tab_creates_its_own_channel(server):
    server_state.begin_session_stream("session-a", threading.Event())
    first = _open_channel(server, "tab-1")
    second = _open_channel(server, "tab-2")
    try:
        _read_event(first)
        _read_event(second)
        _post(server, "/v1/sessions/frames/subscribe", {"conn": "tab-1", "session_id": "session-a"})
        _post(server, "/v1/sessions/frames/subscribe", {"conn": "tab-2", "session_id": "session-a"})
        with server_state._session_stream_lock:
            assert len(server_state._session_streams["session-a"]["subscribers"]) == 2
        assert sorted(server_state.frame_channel_snapshot()) == ["tab-1", "tab-2"]
    finally:
        first.close()
        second.close()


def test_reconnect_same_conn_replays_gap(server):
    cancel_event = threading.Event()
    server_state.begin_session_stream("session-a", cancel_event)

    response = _open_channel(server, "tab-reconnect")
    try:
        _read_event(response)
        _post(server, "/v1/sessions/frames/subscribe",
              {"conn": "tab-reconnect", "session_id": "session-a", "after": -1})
        server_state.publish_session_stream_frame("session-a", {"role": "assistant", "content": "1"})
        payload = _read_frame(response)
        assert payload["frame"]["content"] == "1"
        applied_seq = payload["seq"]
    finally:
        response.close()

    # The tab's only connection is gone, but the inference must survive the
    # grace window so the reconnect can pick it up again.
    server_state.publish_session_stream_frame("session-a", {"role": "assistant", "content": "2"})
    assert not cancel_event.is_set()

    response = _open_channel(server, "tab-reconnect")
    try:
        event, init = _read_event(response)
        assert event == "chan_init" and init["generation"] >= 2
        result = _post(server, "/v1/sessions/frames/subscribe",
                       {"conn": "tab-reconnect", "session_id": "session-a", "after": applied_seq})
        assert result["replayed"] == 1
        event, payload = _read_event(response)
        assert event == "sub_init" and payload["session_id"] == "session-a"
        event, payload = _read_event(response)
        assert (event, payload["frame"]["content"]) == ("frame", "2")
        assert not cancel_event.is_set()
    finally:
        response.close()


def test_channel_requires_conn_parameter(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _open_channel(server, "")
    assert excinfo.value.code == 400
