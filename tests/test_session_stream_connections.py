"""Ownership tests for the retained session inference stream.

A generation belongs to its session and is identified by its ``owner_event``. No
connection owns it, and no connection can end it. These tests pin what that
means in practice:

* nothing an observer does -- subscribing, unsubscribing, going away for good --
  reaches the inference that it was watching;
* a generation that has already ended still cannot publish into its successor's
  stream, finish it, or overwrite its status;
* a second request cannot take a live generation's session over -- one slot per
  session means a displaced run could not be reached by any stop button
  afterwards (see also ``tests/test_session_generation_ownership.py``).
"""

import threading

import pytest

from runtime import server_state


@pytest.fixture(autouse=True)
def clean_session_stream_state():
    with server_state._session_stream_lock:
        server_state._session_streams.clear()
    with server_state._session_state_lock:
        server_state._session_statuses.clear()
        server_state._unread_sessions.clear()
        server_state._session_event_subscribers.clear()
    yield
    with server_state._session_stream_lock:
        server_state._session_streams.clear()
    with server_state._session_state_lock:
        server_state._session_statuses.clear()
        server_state._unread_sessions.clear()
        server_state._session_event_subscribers.clear()


def test_observers_coming_and_going_never_touch_the_generation():
    """Subscribers are registered and released; that is all they ever do.

    The broker used to derive "nobody is watching, so this is an orphan" from the
    same bookkeeping, which is how a hand-off, a reload, a hidden tab or a stale
    ``finally`` block ended up cancelling an inference nobody had stopped.
    """
    cancel_event = threading.Event()
    server_state.begin_session_stream("session-1", cancel_event)

    first = lambda envelope: True
    registered, snapshot = server_state.register_session_stream_with_snapshot(
        "session-1", first,
    )
    assert registered is True and snapshot["active"] is True

    second = lambda envelope: True
    server_state.register_session_stream_with_snapshot("session-1", second)
    server_state.unsubscribe_session_stream("session-1", first)
    server_state.unsubscribe_session_stream("session-1", second)
    # A duplicate/stale unsubscribe is a no-op rather than a re-evaluation.
    server_state.unsubscribe_session_stream("session-1", first)

    with server_state._session_stream_lock:
        assert server_state._session_streams["session-1"]["subscribers"] == []
        assert server_state._session_streams["session-1"]["done"] is False
    assert not cancel_event.is_set()
    assert server_state.publish_session_stream_frame(
        "session-1", {"role": "assistant", "delta": "still running"},
    ) == 1


def test_the_abandonment_machinery_is_gone_not_merely_disabled():
    """No window, no grace, no flight switch, no "starter disconnected" hook.

    Each of these was a way for a transport event to end a run, and each one had
    to be patched at least once. Deleting the names is what keeps the next
    interruption from being fixed by re-introducing one of them.
    """
    for gone in (
        "_cancel_unobserved_stream_locked",
        "_arm_unobserved_cancel_locked",
        "_resolve_unobserved_cancel",
        "_hold_session_locked",
        "_frame_channel_is_live",
        "frame_channel_hold",
        "disconnect_session_stream_starter",
        "set_session_flight_mode",
        "load_flight_sessions",
        "DETACHED_WINDOW_S",
        "_flight_sessions",
    ):
        assert not hasattr(server_state, gone), f"{gone} is back"

    with server_state._session_stream_lock:
        server_state.begin_session_stream("session-1", threading.Event())
        stream = server_state._session_streams["session-1"]
    for field in ("unobserved_timer", "watched_by", "hold_until", "starter_connected"):
        assert field not in stream, f"{field} is back"


def test_a_second_request_cannot_take_a_live_generation_over():
    """The session stays with the run that is producing output.

    There is one ``active_infers`` slot per session, so a generation that loses
    its broker also loses the only handle ``/v1/infer/abort`` -- stop, force,
    pause, interjection -- has on it. Turning its frames away hides the run
    without ending it: it keeps issuing model requests and executing tools for
    its whole remaining round budget, invisible and unstoppable. So the newcomer
    is refused, and ending a generation stays something the user can reach.
    """
    first_event, second_event = threading.Event(), threading.Event()
    server_state.begin_session_stream("session-1", first_event)

    incumbent = server_state.begin_session_stream("session-1", second_event)

    assert incumbent is first_event, "the refused request is told who owns the session"
    assert not first_event.is_set(), "the incumbent is left running, not cancelled"
    assert not second_event.is_set()
    with server_state._session_stream_lock:
        stream = server_state._session_streams["session-1"]
    assert stream["cancel_event"] is first_event
    assert stream["done"] is False
    assert server_state.publish_session_stream_frame(
        "session-1", {"role": "assistant", "delta": "still running"},
        owner_event=first_event,
    ) == 1
    assert server_state.publish_session_stream_frame(
        "session-1", {"role": "assistant", "delta": "mine now?"},
        owner_event=second_event,
    ) is None, "a refused request owns nothing to write through"


def test_events_hub_subscribers_are_not_part_of_the_session_stream():
    """The global status hub has its own subscriber list, deliberately.

    Sidebar/status subscribers cannot register a per-session observer, so they
    never appear in -- or disappear from -- a generation's delivery accounting.
    """
    cancel_event = threading.Event()
    server_state.begin_session_stream("session-1", cancel_event)
    server_state._session_event_subscribers.append(lambda data: True)
    try:
        with server_state._session_stream_lock:
            assert server_state._session_streams["session-1"]["subscribers"] == []
        assert server_state.publish_session_stream_frame(
            "session-1", {"role": "assistant", "delta": "x"}) == 1
        assert not cancel_event.is_set()
    finally:
        server_state._session_event_subscribers.clear()


def test_stale_generation_cannot_finish_or_publish_into_replacement_stream():
    old_event = threading.Event()
    new_event = threading.Event()
    old_subscriber_events = []
    new_subscriber_events = []

    server_state.begin_session_stream("session-1", old_event)
    server_state.register_session_stream_with_snapshot(
        "session-1", old_subscriber_events.append,
    )
    # A live generation is never replaced; the old run was stopped first, and it
    # is that outgoing run whose frames and cleanup must now stay out of the way.
    old_event.set()
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

    # Stopped first, so the successor is allowed to start; its cleanup below is
    # then the outgoing run arriving late.
    old_event.set()
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
