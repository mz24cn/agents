"""Connection-lifetime policy tests for retained session inference streams."""

import threading

import pytest

from runtime import server_state


@pytest.fixture(autouse=True)
def clean_session_stream_state():
    with server_state._session_stream_lock:
        server_state._session_streams.clear()
        server_state._flight_sessions.clear()
    yield
    with server_state._session_stream_lock:
        server_state._session_streams.clear()
        server_state._flight_sessions.clear()


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
