"""Tests for the per-tab multiplexed session frame channel.

One browser tab watches every streaming session over a single SSE connection
(GET /v1/sessions/frames?conn=<tab id>), instead of one connection per session.
These tests cover both halves of the contract:

* the broker side  - a channel is one subscriber per watched session, and that
  subscriber is a *delivery path only*: attaching, detaching, subscribing and
  closing a channel never decide whether an inference lives;
* the HTTP side    - create-or-reuse by ``conn`` id, subscribe/unsubscribe
  commands, gap replay on re-attach.
"""

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer
from runtime import server_state


@pytest.fixture(autouse=True)
def clean_state():
    server_state.frame_channel_reset()
    with server_state._session_stream_lock:
        server_state._session_streams.clear()
    with server_state._session_state_lock:
        server_state._session_statuses.clear()
        server_state._unread_sessions.clear()
        server_state._session_event_subscribers.clear()
    yield
    server_state.frame_channel_reset()
    with server_state._session_stream_lock:
        server_state._session_streams.clear()
    with server_state._session_state_lock:
        server_state._session_statuses.clear()
        server_state._unread_sessions.clear()
        server_state._session_event_subscribers.clear()


# ---------------------------------------------------------------------------
# Broker level: connection accounting never reaches the inference
# ---------------------------------------------------------------------------


def _watching_tab(conn: str):
    """Attach a reader to this conn's channel, which is what makes a watch real.

    A channel whose GET is not attached keeps the bookkeeping but cannot deliver
    a single frame, so it refuses subscriptions: answering "registered" there is
    what let a tab release the connection that *was* the observer. Tests attach
    first, exactly as a browser tab does.
    """
    return server_state.attach_frame_channel(conn)


def test_a_channel_subscription_is_exactly_one_subscriber():
    """Watching a session over the shared channel registers one broker subscriber.

    It is a delivery registration and nothing else: releasing it, and even closing
    the whole channel, leaves the run alone.
    """
    cancel_event = threading.Event()
    server_state.begin_session_stream("session-1", cancel_event)
    _watching_tab("tab-a")

    registered, snapshot = server_state.frame_channel_subscribe("tab-a", "session-1")
    assert registered is True
    assert snapshot["active"] is True
    with server_state._session_stream_lock:
        assert len(server_state._session_streams["session-1"]["subscribers"]) == 1

    # Closing that conversation while the tab keeps its channel attached changes
    # routing only.
    assert server_state.frame_channel_unsubscribe("tab-a", "session-1") is True
    with server_state._session_stream_lock:
        assert server_state._session_streams["session-1"]["subscribers"] == []
    assert server_state.close_frame_channel("tab-a") is True
    assert not cancel_event.is_set()


def test_one_channel_observes_every_watched_session_with_one_connection():
    events_a, events_b = threading.Event(), threading.Event()
    server_state.begin_session_stream("session-a", events_a)
    server_state.begin_session_stream("session-b", events_b)

    _watching_tab("tab-a")
    server_state.frame_channel_subscribe("tab-a", "session-a")
    server_state.frame_channel_subscribe("tab-a", "session-b")

    snapshot = server_state.frame_channel_snapshot()
    assert list(snapshot) == ["tab-a"]
    assert snapshot["tab-a"]["sessions"] == ["session-a", "session-b"]

    with server_state._session_stream_lock:
        assert len(server_state._session_streams["session-a"]["subscribers"]) == 1
        assert len(server_state._session_streams["session-b"]["subscribers"]) == 1

    # Closing the tab releases both deliveries and nothing else: both inferences
    # go on producing, which is what makes "open that conversation again later"
    # work at all.
    assert server_state.close_frame_channel("tab-a") is True
    assert not events_a.is_set() and not events_b.is_set()
    with server_state._session_stream_lock:
        assert server_state._session_streams["session-a"]["subscribers"] == []
        assert server_state._session_streams["session-b"]["subscribers"] == []
        assert server_state._session_streams["session-a"]["done"] is False
        assert server_state._session_streams["session-b"]["done"] is False


def test_channel_receives_live_frames_for_every_session():
    channel, _generation = server_state.attach_frame_channel("tab-a")
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


def test_a_detached_channel_is_dropped_without_disturbing_the_run():
    """The detach grace is resource housekeeping, not a liveness decision.

    Reaping a channel whose reader never came back releases its subscriptions --
    that is the whole point of the window. What must NOT happen is any effect on
    the generations it was carrying: they keep running and keep their frames.
    """
    cancel_event = threading.Event()
    server_state.begin_session_stream("session-1", cancel_event)
    _channel, generation = server_state.attach_frame_channel("tab-a")
    assert server_state.frame_channel_subscribe("tab-a", "session-1")[0] is True

    with patch.object(server_state, "FRAME_CHANNEL_GRACE_S", 0.05):
        assert server_state.detach_frame_channel("tab-a", generation) is True
        deadline = time.time() + 3
        while "tab-a" in server_state.frame_channel_snapshot() and time.time() < deadline:
            time.sleep(0.02)
        assert "tab-a" not in server_state.frame_channel_snapshot()

    with server_state._session_stream_lock:
        assert server_state._session_streams["session-1"]["subscribers"] == []
        assert server_state._session_streams["session-1"]["done"] is False
    assert not cancel_event.is_set()
    # Still producing, still replayable for the next reader.
    assert server_state.publish_session_stream_frame(
        "session-1", {"role": "assistant", "delta": "still working"}) == 1


def test_a_reconnect_inside_the_grace_window_reuses_the_channel():
    """Same ``conn`` id, same channel object, same subscriptions -- no re-registration.

    A reconnect used to be a moment of truth: subscriptions were released and
    re-taken, and any generation that fell through that gap looked unobserved.
    Now nothing is decided there, so reusing the channel is only about not
    disturbing the frames already in flight.
    """
    server_state.begin_session_stream("session-1", threading.Event())
    channel, first_generation = server_state.attach_frame_channel("tab-a")
    assert server_state.frame_channel_subscribe("tab-a", "session-1")[0] is True

    _channel, second_generation = server_state.attach_frame_channel("tab-a")
    with server_state._frame_channels_lock:
        reused = server_state._frame_channels["tab-a"]
    assert reused is channel
    assert second_generation == first_generation + 1
    assert sorted(reused["sessions"]) == ["session-1"]


def test_closing_a_conversation_keeps_its_running_inference():
    """Unsubscribing one session abandons nothing -- the run is not owned by a tab.

    Reported regression, repeatedly: a streaming session that goes to the
    background (the user opens another conversation) releases its frame-channel
    subscription, and every observer that had been left behind was then counted as
    "nobody is watching", cancelling an inference nobody aborted. There is no such
    count any more: releasing a subscription releases delivery.
    """
    cancel_event = threading.Event()
    server_state.begin_session_stream("session-1", cancel_event)
    server_state.attach_frame_channel("tab-a")
    assert server_state.frame_channel_subscribe("tab-a", "session-1")[0] is True

    # The user opens another conversation in the same tab.
    assert server_state.frame_channel_unsubscribe("tab-a", "session-1") is True
    threading.Event().wait(0.3)                   # several times the channel grace
    assert not cancel_event.is_set()

    # The generation keeps running, and keeps its frames for the next visit.
    server_state.publish_session_stream_frame(
        "session-1", {"role": "assistant", "delta": "still working"})
    snapshot = server_state.get_session_stream_snapshot("session-1")
    assert snapshot["active"] is True
    assert [item["frame"]["delta"] for item in snapshot["frames"]] == ["still working"]

    # Going back to the conversation re-watches it on the same channel.
    assert server_state.frame_channel_subscribe("tab-a", "session-1")[0] is True
    threading.Event().wait(0.3)
    assert not cancel_event.is_set()

    # And even the tab itself going away is not a reason to stop the work.
    assert server_state.close_frame_channel("tab-a") is True
    threading.Event().wait(0.3)
    assert not cancel_event.is_set()
    assert server_state.get_session_stream_snapshot("session-1")["active"] is True


def test_reattach_discards_stale_queue_and_replays_from_cursor():
    server_state.begin_session_stream("session-1", threading.Event())
    _channel, generation = _watching_tab("tab-a")
    assert server_state.frame_channel_subscribe("tab-a", "session-1")[0] is True
    server_state.publish_session_stream_frame("session-1", {"role": "assistant", "content": "1"})

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
# Hand-off order and generation identity: what a tab can trust it was told
# ---------------------------------------------------------------------------


def _drain(channel) -> list:
    """Everything this channel has queued for its reader, in delivery order."""
    items = []
    while True:
        try:
            items.append(channel["queue"].get_nowait())
        except Exception:
            return items


def test_a_frame_published_mid_handoff_cannot_overtake_its_own_sub_init(monkeypatch):
    """``sub_init``, then the replay, then live -- never live first.

    Registering a subscriber hands it everything published from that instant on,
    and at that instant the hand-off has not finished pushing ``sub_init`` and the
    replay. So a frame published in between used to reach the tab *before* its own
    ``sub_init``, ahead of the replay. A tab that tracks what it has applied (which
    is how it closes holes) then takes the replay for a duplicate and throws away
    the frames it was missing.
    """
    real = server_state.register_session_stream_with_snapshot

    def register_then_publish(session_id, send_fn, after_seq=-1):
        result = real(session_id, send_fn, after_seq)
        # The interleaving the broker permits: this frame is handed to the channel
        # while the hand-off is still pushing sub_init and the replay.
        server_state.publish_session_stream_frame(
            session_id, {"role": "assistant", "content": "mid hand-off"}
        )
        return result

    monkeypatch.setattr(
        server_state, "register_session_stream_with_snapshot", register_then_publish
    )
    channel, _generation = _watching_tab("tab-a")
    server_state.begin_session_stream("session-1", threading.Event())
    server_state.publish_session_stream_frame(
        "session-1", {"role": "assistant", "content": "first"}
    )

    assert server_state.frame_channel_subscribe("tab-a", "session-1")[0] is True

    items = _drain(channel)
    assert items[0]["type"] == "sub_init", items
    delivered = [item for item in items if item["type"] == "frame"]
    assert [item["seq"] for item in delivered] == [1, 2]
    assert [item["frame"]["content"] for item in delivered] == ["first", "mid hand-off"]


def test_a_retired_reader_cannot_cut_short_the_channel_that_replaced_it():
    """A channel generation is a token, and a reader answers to its own object.

    A channel is dropped and another built under the same ``conn`` once its grace
    expires. Both incarnations started their generation count at 1, so a reader of
    the retired one matched the new channel's number, marked that connection
    disconnected and started the countdown that releases the subscriptions of a tab
    which was streaming just fine.
    """
    retired, retired_generation = _watching_tab("tab-a")
    server_state.begin_session_stream("session-1", threading.Event())

    server_state.close_frame_channel("tab-a", reason="grace_expired")
    current, current_generation = _watching_tab("tab-a")
    assert current is not retired
    assert current_generation != retired_generation
    assert server_state.frame_channel_subscribe("tab-a", "session-1")[0] is True

    # The retired reader finally unwinds.
    assert server_state.detach_frame_channel("tab-a", retired_generation, retired) is False

    assert current["connected"] is True
    assert current["handlers"] == 1
    assert server_state.frame_channel_snapshot()["tab-a"]["sessions"] == ["session-1"]
    # ... and its own reader can still leave and come back.
    assert server_state.detach_frame_channel("tab-a", current_generation, current) is True
    assert current["connected"] is False


def test_a_frame_dropped_for_a_slow_tab_is_still_retained_and_askable(monkeypatch):
    """The premise of the tab's self-heal: a dropped live frame is not lost data.

    A tab that reads too slowly makes the channel drop frames rather than mark its
    subscriber dead (which would have cancelled an inference that was merely being
    read slowly). The tab notices -- the sequence jumped -- and re-subscribes from
    its cursor. That only works because everything stays retained here, and the
    drops are counted so the repair rate is measurable.
    """
    monkeypatch.setattr(server_state, "FRAME_CHANNEL_QUEUE_MAXSIZE", 2)
    channel, _generation = _watching_tab("tab-a")
    server_state.begin_session_stream("session-1", threading.Event())
    assert server_state.frame_channel_subscribe("tab-a", "session-1")[0] is True

    for n in range(1, 6):
        assert server_state.publish_session_stream_frame(
            "session-1", {"role": "assistant", "content": str(n)}
        ) == n

    assert channel["dropped"] > 0, "the queue was over full and said so"
    assert server_state.frame_channel_snapshot()["tab-a"]["dropped"] == channel["dropped"]
    with server_state._session_stream_lock:
        retained = [item["seq"] for item in server_state._session_streams["session-1"]["frames"]]
    assert retained == [1, 2, 3, 4, 5]

    # Asked again from a cursor, the whole generation comes back: the hole the tab
    # spotted is closeable, which is the difference between a slow reader and a
    # conversation that quietly lacks pieces.
    _registered, snapshot = server_state.frame_channel_subscribe(
        "tab-a", "session-1", after_seq=0
    )
    assert [item["seq"] for item in snapshot["frames"]] == [1, 2, 3, 4, 5]


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


def test_channel_lifecycle_is_traceable_from_the_log(caplog):
    """Every delivery change the tab makes is visible in the log, by itself.

    Field reports of an interrupted run used to hinge on reconstructing this
    history from log archaeology: did the tab come back, how many times, how long
    was its transport gone. The attach / detach / drop lines say all of it, and
    they say it without ever implying that the inference was affected -- nothing
    on a channel can cancel a generation.
    """
    with patch.object(server_state, "FRAME_CHANNEL_GRACE_S", 0.05), \
            caplog.at_level(logging.INFO, logger="runtime.server"):
        cancel_event = threading.Event()
        server_state.begin_session_stream("session-1", cancel_event)
        server_state.publish_session_stream_frame(
            "session-1", {"role": "assistant", "content": "still working"})
        _channel, generation = _watching_tab("tab-a")
        assert server_state.frame_channel_subscribe("tab-a", "session-1")[0] is True

        assert server_state.detach_frame_channel("tab-a", generation) is True
        deadline = time.time() + 3
        while "tab-a" in server_state.frame_channel_snapshot() and time.time() < deadline:
            time.sleep(0.02)

    text = "\n".join(record.getMessage() for record in caplog.records)
    # One attach only: a re-connect would have shown up as a second attach line,
    # which is how "the browser never came back" is distinguished from "it came
    # back". The number itself is a process-wide token, never a per-conn tally, so
    # what is checkable is that one token runs through this incarnation.
    attaches = [line for line in text.splitlines()
                if "frame_channel attach conn=tab-a" in line]
    assert len(attaches) == 1, attaches
    token = re.search(r"generation=(\d+)", attaches[0]).group(1)
    # The grace window, with its length and what was still registered on it.
    assert f"frame_channel detached conn=tab-a generation={token} grace=0.05s" in text
    assert f"frame_channel dropped conn=tab-a generation={token} cause=grace_expired" in text
    assert "released=['session-1']" in text
    # Nothing here is a cancellation: the log cannot claim the run ended, because
    # it did not.
    assert "CANCELLED" not in text
    assert not cancel_event.is_set()




def test_a_stale_reader_detach_does_not_drop_the_live_channel():
    """One conn can have two attached readers; the older one must not kill it.

    Two tabs can share a sessionStorage conn id (duplicating a tab), and a proxy can
    keep the upstream of a replaced request open. Whichever reader dies must not
    start the countdown that releases the subscriptions of a channel that is still
    streaming: the live reader would keep delivering frames while the backend had
    already destroyed the channel they came from.
    """
    with patch.object(server_state, "FRAME_CHANNEL_GRACE_S", 0.2):
        cancel_event = threading.Event()
        server_state.begin_session_stream("session-1", cancel_event)
        _channel, first = _watching_tab("tab-a")
        assert server_state.frame_channel_subscribe("tab-a", "session-1")[0] is True

        _channel, second = server_state.attach_frame_channel("tab-a")
        assert second > first

        assert server_state.detach_frame_channel("tab-a", first) is False
        threading.Event().wait(0.35)
        assert not cancel_event.is_set()
        assert server_state.frame_channel_snapshot()["tab-a"]["sessions"] == ["session-1"]

        # Only when the reader that owns the current generation leaves does the
        # grace window start.
        assert server_state.detach_frame_channel("tab-a", second) is True
        deadline = time.time() + 3
        while "tab-a" in server_state.frame_channel_snapshot() and time.time() < deadline:
            time.sleep(0.02)
        assert server_state.frame_channel_snapshot() == {}
        assert not cancel_event.is_set()




def test_the_countdown_starts_when_the_last_reader_leaves():
    """The grace window must still run if the owning reader leaves first.

    Otherwise a lingering stale reader would keep an unattended channel — and the
    8k frame queue behind it — alive indefinitely. Reaping it releases the
    delivery; it does not touch the generation that was being delivered.
    """
    with patch.object(server_state, "FRAME_CHANNEL_GRACE_S", 0.2):
        cancel_event = threading.Event()
        server_state.begin_session_stream("session-1", cancel_event)
        _channel, first = _watching_tab("tab-a")
        assert server_state.frame_channel_subscribe("tab-a", "session-1")[0] is True

        _channel, second = server_state.attach_frame_channel("tab-a")

        # The owning reader goes first while a stale one lingers: still attached.
        assert server_state.detach_frame_channel("tab-a", second) is False
        threading.Event().wait(0.35)
        assert not cancel_event.is_set()
        assert server_state.frame_channel_snapshot()["tab-a"]["sessions"] == ["session-1"]

        # The last reader leaving starts the clock, and the reap releases the
        # subscription without touching the inference.
        assert server_state.detach_frame_channel("tab-a", first) is False
        deadline = time.time() + 3
        while "tab-a" in server_state.frame_channel_snapshot() and time.time() < deadline:
            time.sleep(0.02)
        assert server_state.frame_channel_snapshot() == {}
        with server_state._session_stream_lock:
            assert server_state._session_streams["session-1"]["subscribers"] == []
        assert not cancel_event.is_set()
        assert server_state.publish_session_stream_frame(
            "session-1", {"role": "assistant", "delta": "still running"}) == 1


# ---------------------------------------------------------------------------
# Ownership: a generation belongs to its session, never to a connection
# ---------------------------------------------------------------------------

def test_closing_the_channel_never_cancels_the_inference():
    """The whole "detached window" question is gone: closing changes nothing.

    There used to be a window, a policy, and a cancellation at the end of it, and
    the window was what any momentary transport problem could reach. A page unload
    therefore had to announce how long it expected to be unreachable, a hidden tab
    had to ask for a hold, and a reconnect had to rescue the generation before the
    timer fired. All of that machinery is removed together with the question.
    """
    cancel_event = threading.Event()
    server_state.begin_session_stream("session-1", cancel_event)
    _channel, generation = server_state.attach_frame_channel("tab-a")
    assert server_state.frame_channel_subscribe("tab-a", "session-1")[0] is True
    assert server_state.close_frame_channel("tab-a", reason="page_unload") is True
    assert server_state.frame_channel_snapshot() == {}
    assert not cancel_event.is_set()

    # No policy knob can bring a cancellation back: there is no window to shrink.
    assert not hasattr(server_state, "_cancel_unobserved_stream_locked")
    assert not hasattr(server_state, "frame_channel_hold")


def test_losing_every_observer_never_cancels_the_inference():
    """The replacement for the entire abandonment policy, stated once.

    Attach, watch, unsubscribe, close the channel, detach the reader, never come
    back: the run is still running, still producing, and still replayable in order
    by whoever opens the session again. The only ways a generation ends are an
    explicit stop, the work finishing, or the process shutting down.
    """
    cancel_a, cancel_b = threading.Event(), threading.Event()
    server_state.begin_session_stream("session-a", cancel_a)
    server_state.begin_session_stream("session-b", cancel_b)
    _channel, generation = _watching_tab("tab-a")
    server_state.frame_channel_subscribe("tab-a", "session-a")
    server_state.frame_channel_subscribe("tab-a", "session-b")

    assert server_state.frame_channel_unsubscribe("tab-a", "session-b") is True
    assert server_state.close_frame_channel("tab-a", reason="bfcache") is True
    server_state.detach_frame_channel("tab-a", generation)
    threading.Event().wait(0.2)                       # past the channel grace
    assert server_state.frame_channel_snapshot() == {}

    assert server_state.publish_session_stream_frame(
        "session-a", {"role": "assistant", "delta": "still working"}) == 1
    assert not cancel_a.is_set() and not cancel_b.is_set()

    # The tab comes back minutes later and reads the whole generation from cursor 0.
    _channel, _gen = _watching_tab("tab-a")
    registered, snapshot = server_state.frame_channel_subscribe(
        "tab-a", "session-a", after_seq=0)
    assert registered is True
    assert [item["frame"]["delta"] for item in snapshot["frames"]] == ["still working"]










# ---------------------------------------------------------------------------
# A channel exists only while its reader is attached.
#
# Regression (local runs cancelled at 11:12-11:30 with nobody pressing Stop): a
# subscribe command for a ``conn`` the registry no longer knew created a
# generation-0 channel with no reader, answered ``registered: true``, and its
# 2-second anti-leak grace then unsubscribed the session and armed the
# abandonment countdown -- while the POST that *was* the only observer had
# already aborted itself on the strength of that answer. Twelve log lines carry
# the signature: ``dropped ... generation=0 cause=grace_expired handlers=0
# released=['<sid>'] detached_for=2.0s``. The companion signature on the reader
# side is a channel dropped while its reader was still attached (``cause=idle
# handlers=1``) followed by that reader's ``stream ended`` 26-29 seconds later:
# the tab kept receiving keepalives from a channel the server had destroyed, so
# its "silent for two keepalives" test never fired and it kept re-subscribing.
# ---------------------------------------------------------------------------


def test_a_subscribe_command_without_a_reader_registers_nothing(monkeypatch):
    """No reader means no channel, so the answer must be "not watched"."""
    cancel_event = threading.Event()
    server_state.begin_session_stream("session-1", cancel_event)

    registered, _snapshot = server_state.frame_channel_subscribe("tab-a", "session-1")

    assert registered is False, "a subscription nobody can deliver is not a registration"
    with server_state._frame_channels_lock:
        assert "tab-a" not in server_state._frame_channels
    with server_state._session_stream_lock:
        assert server_state._session_streams["session-1"]["subscribers"] == []
    assert cancel_event.is_set() is False


def test_a_detached_channel_still_refuses_subscriptions():
    """The reuse grace is not an observer: a detached channel cannot watch."""
    cancel_event = threading.Event()
    server_state.begin_session_stream("session-1", cancel_event)
    _channel, generation = server_state.attach_frame_channel("tab-a")
    server_state.detach_frame_channel("tab-a", generation)   # within FRAME_CHANNEL_GRACE_S

    assert server_state.frame_channel_subscribe("tab-a", "session-1")[0] is False
    with server_state._session_stream_lock:
        assert server_state._session_streams["session-1"]["subscribers"] == []


def test_the_frame_channel_reader_exits_when_the_channel_is_dropped(server):
    """Destruction must be visible to the tab, not only to the log.

    The reader's only exit conditions used to be a failed write and a newer
    generation taking over, so a channel dropped by a close command (or by the
    registry bound) kept its reader alive, pumping keepalives into a browser that
    believed the transport was healthy. Making the teardown observable is what
    lets the client's existing staleness logic reconnect -- no client-side
    zombie detector required.
    """
    cancel_event = threading.Event()
    server_state.begin_session_stream("session-1", cancel_event)

    conn = _open_channel(server, "tab-z")
    assert server_state.frame_channel_subscribe("tab-z", "session-1")[0] is True
    assert _read_event(conn)[0] == "chan_init"
    assert _read_event(conn)[0] == "sub_init"

    server_state.close_frame_channel("tab-z", reason="test_evict")
    try:
        rest = conn.read()          # the urlopen timeout bounds this at 5s
    except OSError as err:          # socket.timeout on the old build
        pytest.fail(f"reader outlived its channel: {err}")
    assert rest == b""                        # the stream ended, promptly


def test_the_subscribe_endpoint_refuses_a_conn_that_has_no_reader(server):
    """What the client's hand-off gate reads must mean "delivery is possible".

    ``registered: true`` used to be answered for any conn id the browser named,
    creating a channel object with no reader behind it. The page then released
    the connection that was the session's only observer, and the generation was
    left the page thinking a session was being delivered when nothing could be.
    """
    server_state.begin_session_stream("session-1", threading.Event())
    body = _post(server, "/v1/sessions/frames/subscribe",
                 {"conn": "tab-ghost", "session_id": "session-1", "after": -1})
    assert body["registered"] is False
    assert body["reason"] == "no_reader"
    with server_state._frame_channels_lock:
        assert "tab-ghost" not in server_state._frame_channels
