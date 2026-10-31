"""Connection-lifetime policy tests for retained session inference streams."""

import threading
import time

import pytest

from runtime import server_state


@pytest.fixture(autouse=True)
def clean_session_stream_state(monkeypatch):
    # These tests assert the connection policy itself. The deferred window that
    # protects a hand-off/reconnect from cancelling the generation is covered by
    # the *_grace_* tests below, which set their own window.
    monkeypatch.setenv("SESSION_UNOBSERVED_GRACE_S", "0")
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


def test_last_starter_disconnect_cancels_non_flight_inference():
    cancel_event = threading.Event()
    server_state.begin_session_stream("session-1", cancel_event)

    assert server_state.disconnect_session_stream_starter("session-1") is True
    assert cancel_event.is_set()


def test_other_open_browser_keeps_non_flight_inference_alive():
    cancel_event = threading.Event()
    subscriber = lambda envelope: True
    server_state.begin_session_stream("session-1", cancel_event)
    registered, snapshot = server_state.register_session_stream_with_snapshot(
        "session-1", subscriber
    )

    assert registered is True
    assert snapshot["active"] is True
    assert server_state.disconnect_session_stream_starter("session-1") is False
    assert not cancel_event.is_set()

    server_state.unsubscribe_session_stream("session-1", subscriber)
    assert cancel_event.is_set()


def test_stale_unsubscribe_does_not_cancel_after_a_replacement_subscriber_left():
    cancel_event = threading.Event()
    stale_subscriber = lambda envelope: True
    server_state.begin_session_stream("session-1", cancel_event)
    server_state.register_session_stream_with_snapshot("session-1", stale_subscriber)
    server_state.unsubscribe_session_stream("session-1", stale_subscriber)

    # The starter is still alive, so the first unsubscribe did not cancel.
    assert not cancel_event.is_set()
    server_state.disconnect_session_stream_starter("session-1")
    assert cancel_event.is_set()

    # A duplicate/stale finally block must be a no-op rather than re-evaluating
    # connection policy for a subscriber that is no longer registered.
    server_state.unsubscribe_session_stream("session-1", stale_subscriber)


def test_flight_mode_allows_zero_session_subscribers_until_disabled():
    cancel_event = threading.Event()
    server_state.begin_session_stream("session-1", cancel_event)
    server_state.set_session_flight_mode("session-1", True)

    assert server_state.disconnect_session_stream_starter("session-1") is False
    assert not cancel_event.is_set()

    enabled = server_state.set_session_flight_mode("session-1", False)
    assert enabled is False
    assert cancel_event.is_set()


def test_global_agent_service_connection_does_not_count_as_session_subscription():
    """Only starter/session-stream connections are represented in the broker."""
    cancel_event = threading.Event()
    server_state.begin_session_stream("session-1", cancel_event)

    # Global /v1/sessions/events subscribers live in a separate collection and
    # therefore cannot keep this session-specific inference alive.
    server_state._session_event_subscribers.append(lambda data: True)
    try:
        assert server_state.disconnect_session_stream_starter("session-1") is True
        assert cancel_event.is_set()
    finally:
        server_state._session_event_subscribers.clear()


# --- deferred cancellation window (the POST -> frame-channel hand-off) ---------

def test_unobserved_cancellation_waits_so_a_handoff_can_take_over(monkeypatch):
    """A momentarily unobserved generation must not be cancelled on the spot.

    Releasing the initiating POST stream and registering the shared frame-channel
    subscriber are two separate requests, so the broker can be briefly observed
    by nobody. Cancelling at that instant is what users saw as
    "Error: user interrupted." for inferences they never aborted.
    """
    monkeypatch.setenv("SESSION_UNOBSERVED_GRACE_S", "0.3")
    cancel_event = threading.Event()
    server_state.begin_session_stream("session-1", cancel_event)

    assert server_state.disconnect_session_stream_starter("session-1") is False
    assert not cancel_event.is_set()

    # The hand-off subscription lands inside the window -> nothing is cancelled.
    subscriber = lambda envelope: True
    registered, _snapshot = server_state.register_session_stream_with_snapshot(
        "session-1", subscriber
    )
    assert registered is True
    time.sleep(0.45)
    assert not cancel_event.is_set()

    # And when the observer really does go away, the window still cancels it.
    server_state.unsubscribe_session_stream("session-1", subscriber)
    assert not cancel_event.is_set()
    time.sleep(0.45)
    assert cancel_event.is_set()


def test_flight_mode_toggled_on_inside_the_window_disarms_the_cancellation(monkeypatch):
    monkeypatch.setenv("SESSION_UNOBSERVED_GRACE_S", "0.2")
    cancel_event = threading.Event()
    server_state.begin_session_stream("session-1", cancel_event)

    server_state.disconnect_session_stream_starter("session-1")
    assert not cancel_event.is_set()

    assert server_state.set_session_flight_mode("session-1", True) is True
    time.sleep(0.35)
    assert not cancel_event.is_set()


def test_pending_cancellation_of_a_retired_generation_still_runs(monkeypatch):
    """A new generation must not swallow the old generation's pending cancel."""
    monkeypatch.setenv("SESSION_UNOBSERVED_GRACE_S", "0.2")
    old_event = threading.Event()
    server_state.begin_session_stream("session-1", old_event)
    server_state.disconnect_session_stream_starter("session-1")
    assert not old_event.is_set()

    new_event = threading.Event()
    server_state.begin_session_stream("session-1", new_event)
    time.sleep(0.35)
    assert old_event.is_set()
    assert not new_event.is_set()


def test_stale_generation_cannot_finish_or_publish_into_replacement_stream():
    old_event = threading.Event()
    new_event = threading.Event()
    old_subscriber_events = []
    new_subscriber_events = []

    server_state.begin_session_stream("session-1", old_event)
    server_state.register_session_stream_with_snapshot(
        "session-1", old_subscriber_events.append,
    )
    server_state.begin_session_stream("session-1", new_event)
    assert old_subscriber_events == [None]
    server_state.register_session_stream_with_snapshot(
        "session-1", new_subscriber_events.append,
    )

    assert server_state.publish_session_stream_frame(
        "session-1", {"role": "assistant", "content": "old"}, old_event,
    ) is None
    assert server_state.finish_session_stream("session-1", old_event) is False
    assert new_subscriber_events == []

    seq = server_state.publish_session_stream_frame(
        "session-1", {"role": "assistant", "content": "new"}, new_event,
    )
    assert seq == 1
    assert new_subscriber_events[0]["frame"]["content"] == "new"


def test_stale_generation_cannot_overwrite_replacement_streaming_status():
    old_event = threading.Event()
    new_event = threading.Event()
    frames = []
    server_state._session_event_subscribers.append(frames.append)

    server_state.begin_session_stream("session-1", old_event)
    assert server_state.transition_session_stream_status(
        "session-1", old_event, "streaming",
    ) is True

    server_state.begin_session_stream("session-1", new_event)
    assert server_state.transition_session_stream_status(
        "session-1", new_event, "streaming",
    ) is True
    assert server_state.transition_session_stream_status(
        "session-1", old_event, "done_error_unread",
    ) is False

    assert server_state._session_statuses["session-1"] == "streaming"
    assert "session-1" not in server_state._unread_sessions
    assert '"status": "done_error_unread"' not in "".join(frames)


def test_stale_starter_disconnect_does_not_cancel_replacement_inference():
    old_event = threading.Event()
    new_event = threading.Event()

    server_state.begin_session_stream("session-1", old_event)
    server_state.begin_session_stream("session-1", new_event)

    assert server_state.disconnect_session_stream_starter(
        "session-1", owner_event=old_event,
    ) is False
    assert not new_event.is_set()
