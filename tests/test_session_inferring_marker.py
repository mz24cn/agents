"""The durable "推理中" marker in index.json: who writes it and what it means.

An inference is owned by its session and executed by one agentsvc instance. When
that instance goes away the generation cannot follow it, so the instance has to
record that the run is over -- and if it did not get the chance, the marker it
left behind is the only evidence that the run was killed rather than stopped.

Two facts, tested here:

* ``begin_session_stream`` / ``finish_session_stream`` write and clear the marker,
  and a failing persistence hook can never damage the run itself;
* a marker still standing when an instance starts means an unclean shutdown, and
  it is turned into a terminal (failed) status exactly once.
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


def test_a_started_generation_marks_the_session_inferring(manager):
    server_state.set_inferring_sink(manager.set_session_inferring)

    server_state.begin_session_stream("s1", threading.Event())

    entry = _entry(manager, "s1")
    assert entry["inferring"] is True
    assert entry["inferring_since"], "when the run started is what makes a stale marker readable"
    assert manager.inferring_sessions() == ["s1"]


def test_finishing_the_generation_clears_the_marker(manager):
    server_state.set_inferring_sink(manager.set_session_inferring)
    server_state.begin_session_stream("s1", threading.Event())

    assert server_state.finish_session_stream("s1") is True

    entry = _entry(manager, "s1")
    assert "inferring" not in entry and "inferring_since" not in entry
    assert manager.inferring_sessions() == []


def test_a_replacement_generation_keeps_the_marker_honest(manager):
    """Two generations, one marker: it describes the session, not the request."""
    server_state.set_inferring_sink(manager.set_session_inferring)
    server_state.begin_session_stream("s1", threading.Event())
    since = _entry(manager, "s1")["inferring_since"]

    server_state.begin_session_stream("s1", threading.Event())
    assert _entry(manager, "s1")["inferring_since"] == since, "no duplicate write per round"

    server_state.finish_session_stream("s1")
    assert "inferring" not in _entry(manager, "s1")


def test_a_failing_persistence_hook_never_reaches_the_run(manager):
    """index.json is a record, not a dependency of the inference."""
    def broken_sink(session_id, inferring):
        raise RuntimeError("disk gone")

    server_state.set_inferring_sink(broken_sink)
    cancel_event = threading.Event()

    server_state.begin_session_stream("s1", cancel_event)
    seq = server_state.publish_session_stream_frame(
        "s1", {"role": "assistant", "delta": "still producing"})
    assert seq == 1
    assert server_state.finish_session_stream("s1") is True
    assert not cancel_event.is_set()
    assert manager.inferring_sessions() == []


def test_clearing_the_marker_of_an_unknown_session_writes_nothing(manager):
    """A finished run of a deleted session must not resurrect its index entry."""
    manager.set_session_inferring("ghost", False)
    assert manager._read_index() == {}


def test_a_marker_left_behind_is_failed_exactly_once(manager):
    """The residue of a killed instance, read once by the instance that replaces it."""
    manager.set_session_inferring("s2", True)
    manager.set_session_inferring("s1", True)

    interrupted = manager.fail_inferring_sessions()

    assert interrupted == ["s1", "s2"]
    assert manager.inferring_sessions() == []
    for session_id in ("s1", "s2"):
        assert "inferring" not in _entry(manager, session_id)
        assert "inferring_since" not in _entry(manager, session_id)
    assert manager.fail_inferring_sessions() == []


def test_interrupted_sessions_are_seeded_with_a_terminal_status():
    """The sidebar must show a failed run, not a session that never stops streaming."""
    server_state.seed_interrupted_status(["s1", "s2"])

    with server_state._session_state_lock:
        assert server_state._session_statuses["s1"] == "done_error_unread"
        assert server_state._session_statuses["s2"] == "done_error_unread"
        assert server_state._unread_sessions["s1"] == "done_error_unread"

    server_state.seed_interrupted_status([])                  # no-op, never clears

    with server_state._session_state_lock:
        assert server_state._session_statuses["s1"] == "done_error_unread"
