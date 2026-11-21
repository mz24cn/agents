"""A session hosts one generation at a time, and a second request cannot move it.

An inference is owned by its session and executed by one agentsvc instance, but
the bookkeeping per session is single-slot: one broker in ``_session_streams``,
one ``active_infers`` entry through which ``/v1/infer/abort`` reaches a run
(stop, force, pause, interjection alike). So a second request for the same
session -- another browser tab of the same conversation, a retry, a Continue
issued while a run from elsewhere is still producing, a non-streaming
``POST /v1/infer`` aimed at a session a browser is streaming -- has nowhere to
put itself without displacing the run that holds the slot.

Displacing it is what used to happen, and it was the worst of both outcomes: the
old run went invisible (its frames refused, its status replaced) without
stopping, so it kept issuing model requests and executing tools -- mutating the
workspace -- for its whole remaining round budget, with no button anywhere able
to reach it, while the session looked idle. A generation is now ended only by
what the user can actually ask for: stop, pause, letting the run finish, or
shutting the process down. A request that arrives while one is running is
refused, and a frame that carries no generation identity cannot invent a
generation at all -- a broker nobody can stop would hold the session "推理中"
forever and be read as a crash by the next instance.
"""

import threading

import pytest

from runtime import server_state
from runtime.session_manager import SessionManager


@pytest.fixture(autouse=True)
def isolated_state():
    server_state.set_inferring_sink(None)
    with server_state._session_stream_lock:
        server_state._session_streams.clear()
    with server_state._session_state_lock:
        server_state._session_statuses.clear()
        server_state._unread_sessions.clear()
    yield
    server_state.set_inferring_sink(None)
    with server_state._session_stream_lock:
        server_state._session_streams.clear()
    with server_state._session_state_lock:
        server_state._session_statuses.clear()
        server_state._unread_sessions.clear()


@pytest.fixture
def manager(tmp_path):
    return SessionManager(chats_dir=str(tmp_path))


def _entry(manager, session_id):
    return manager._read_index().get(session_id)


def test_a_second_request_is_refused_instead_of_taking_the_session_over():
    """Refusing is the whole answer while a generation is live."""
    first, second = threading.Event(), threading.Event()
    assert server_state.begin_session_stream("s1", first) is None

    assert server_state.begin_session_stream("s1", second) is first
    assert not first.is_set(), "the run that was reached keeps running"
    with server_state._session_stream_lock:
        stream = server_state._session_streams["s1"]
    assert stream["cancel_event"] is first, "the session still belongs to the incumbent"
    assert server_state.session_stream_is_live("s1")


def test_a_refused_request_does_not_touch_the_durable_marker():
    """The 推理中 marker belongs to the generation that opened the session.

    A refused request must not re-record it either: index.json says "推理中"
    because of the run that is actually running, and a second writer for a run
    that never started is exactly the noise a later instance would read as a
    crash.
    """
    writes = []
    server_state.set_inferring_sink(lambda sid, value: writes.append((sid, value)))
    first, second = threading.Event(), threading.Event()

    server_state.begin_session_stream("s1", first)
    assert server_state.begin_session_stream("s1", second) is first

    assert writes == [("s1", True)], "only the incumbent claimed the session"


def test_an_already_stopped_run_is_not_reported_as_superseded():
    """Stopping first means the next request inherits a run that is already done."""
    first, second = threading.Event(), threading.Event()
    server_state.begin_session_stream("s1", first)
    first.set()

    assert server_state.begin_session_stream("s1", second) is None
    assert server_state.session_stream_is_live("s1")


def test_a_finished_generation_is_not_a_run_to_cancel():
    """A run that ended on its own (or handed tools to the client) is history."""
    first, second = threading.Event(), threading.Event()
    server_state.begin_session_stream("s1", first)
    assert server_state.finish_session_stream("s1", owner_event=first) is True

    assert server_state.begin_session_stream("s1", second) is None
    assert not first.is_set(), "the successor must not claim it stopped a finished run"


def test_a_run_that_was_stopped_cannot_publish_into_its_successor():
    """Frame identity follows the broker, so an old tail cannot poison a new one.

    The successor only exists here because the previous run had been asked to
    stop: a live generation refuses the replacement outright.
    """
    first, second = threading.Event(), threading.Event()
    server_state.begin_session_stream("s1", first)
    first.set()
    assert server_state.begin_session_stream("s1", second) is None

    assert server_state.publish_session_stream_frame(
        "s1", {"role": "assistant", "content": "tail of the old run"},
        owner_event=first,
    ) is None
    assert server_state.publish_session_stream_frame(
        "s1", {"role": "assistant", "content": "new run"}, owner_event=second,
    ) == 1


def test_a_run_that_was_stopped_reaching_its_end_leaves_the_new_marker_alone(manager):
    """The durable marker describes the live generation, never the outgoing one."""
    server_state.set_inferring_sink(manager.set_session_inferring)
    first, second = threading.Event(), threading.Event()
    server_state.begin_session_stream("s1", first)
    # Stop was requested, then a new turn started while that run unwound.
    first.set()
    server_state.begin_session_stream("s1", second)

    # The outgoing run unwinds through its normal cleanup afterwards.
    assert server_state.finish_session_stream("s1", owner_event=first) is False
    assert _entry(manager, "s1")["inferring"] is True, (
        "the replacement is still running: clearing here would hide a live run"
    )

    assert server_state.finish_session_stream("s1", owner_event=second) is True
    assert "inferring" not in _entry(manager, "s1")


def test_only_a_run_that_can_be_stopped_holds_the_request_gate_shut():
    """The gate exists to protect a live run; a stranded broker must not strand a session."""
    assert not server_state.session_stream_is_live("s1")

    cancel_event = threading.Event()
    server_state.begin_session_stream("s1", cancel_event)
    assert server_state.session_stream_is_live("s1")

    server_state.finish_session_stream("s1", owner_event=cancel_event)
    assert not server_state.session_stream_is_live("s1")

    server_state.begin_session_stream("s1", cancel_event)
    cancel_event.set()
    assert not server_state.session_stream_is_live("s1"), (
        "a stopped run is on its way out; refusing the next request over it "
        "would leave the session with no way forward"
    )


def test_a_frame_from_outside_any_run_invents_no_generation(manager):
    """No identity, no generation: no broker, no frames, no durable marker."""
    server_state.set_inferring_sink(manager.set_session_inferring)

    assert server_state.publish_session_stream_frame(
        "s9", {"role": "status", "status": "context_compression", "phase": "started"}
    ) is None

    with server_state._session_stream_lock:
        assert "s9" not in server_state._session_streams
    assert _entry(manager, "s9") is None, (
        "a marker for a run that does not exist is read as a crash by the next instance"
    )
    assert not server_state.session_stream_is_live("s9")


def test_a_frame_that_names_its_run_opens_the_broker_it_belongs_to(manager):
    """A worker thread of a live generation may open the broker it is bound to."""
    server_state.set_inferring_sink(manager.set_session_inferring)
    cancel_event = threading.Event()

    assert server_state.publish_session_stream_frame(
        "s9", {"role": "assistant", "content": "from a nested tool"},
        owner_event=cancel_event,
    ) == 1

    with server_state._session_stream_lock:
        stream = server_state._session_streams["s9"]
    assert stream["cancel_event"] is cancel_event
    assert server_state.session_stream_is_live("s9")
    assert manager.inferring_sessions() == ["s9"]

    assert server_state.publish_session_stream_frame(
        "s9", {"role": "assistant", "content": "second"}, owner_event=cancel_event,
    ) == 2
