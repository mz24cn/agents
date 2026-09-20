"""Regression tests: terminal done_* status events carry the canonical title.

The frontend restores the sidebar session title from these events after
inference completes.  Previously the sidebar kept displaying the temporary
user-message title until the page was reloaded, because:

* follow-up turns never broadcast a ``title_update`` (auto generation only
  runs once per session), and
* title generation may be skipped or fail on a given round.

At the moment a terminal ``done_*`` status is broadcast, final persistence
(including any auto title generation) has already completed, so the index
title attached to the event is the final canonical title.
"""

import json

import pytest

from runtime import server_state
from runtime.session_manager import SessionManager


@pytest.fixture(autouse=True)
def clean_state():
    previous_provider = server_state._session_title_provider
    with server_state._session_state_lock:
        server_state._session_event_subscribers.clear()
    yield
    server_state.set_session_title_provider(previous_provider)
    with server_state._session_state_lock:
        server_state._session_event_subscribers.clear()


def _broadcast_and_collect(session_id, status):
    frames = []

    def send(frame):
        frames.append(frame)
        return True

    with server_state._session_state_lock:
        server_state._session_event_subscribers.append(send)
    try:
        server_state._broadcast_session_status(session_id, status)
    finally:
        with server_state._session_state_lock:
            server_state._session_event_subscribers.remove(send)
    return [json.loads(frame[6:]) for frame in frames]


def test_done_events_carry_canonical_title(tmp_path):
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s1", "first user message")
    server_state.set_session_title_provider(sm.get_title_info)

    # Simulate auto generation having completed (final title in the index).
    index = sm._read_index()
    index["s1"]["title"] = "AI生成的标题"
    index["s1"]["title_generated"] = "AI生成的标题"
    sm._write_index(index)

    events = _broadcast_and_collect("s1", "done_success_unread")
    assert len(events) == 1
    assert events[0]["status"] == "done_success_unread"
    assert events[0]["title"] == "AI生成的标题"
    assert events[0]["title_given"] is False

    # The error terminal carries the same canonical title.
    events = _broadcast_and_collect("s1", "done_error_unread")
    assert events[0]["status"] == "done_error_unread"
    assert events[0]["title"] == "AI生成的标题"


def test_done_event_carries_manual_title_flag(tmp_path):
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s1", "first user message")
    sm.set_title_manually("s1", "我的标题")
    server_state.set_session_title_provider(sm.get_title_info)

    events = _broadcast_and_collect("s1", "done_success_unread")
    assert events[0]["title"] == "我的标题"
    assert events[0]["title_given"] is True


def test_non_terminal_events_do_not_carry_title(tmp_path):
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s1", "first user message")
    server_state.set_session_title_provider(sm.get_title_info)

    for status in ("streaming", "idle"):
        events = _broadcast_and_collect("s1", status)
        assert events[0]["status"] == status
        assert "title" not in events[0]


def test_done_event_for_unknown_session_has_no_title():
    sm_provider = lambda sid: None
    server_state.set_session_title_provider(sm_provider)

    events = _broadcast_and_collect("missing", "done_error_unread")
    assert events[0]["status"] == "done_error_unread"
    assert "title" not in events[0]


def test_done_event_without_provider_registered_has_no_title():
    # Without a registered provider (e.g. before server initialization) the
    # broadcast degrades gracefully to a plain status event.
    server_state.set_session_title_provider(None)

    events = _broadcast_and_collect("s1", "done_success_unread")
    assert events[0]["status"] == "done_success_unread"
    assert "title" not in events[0]


def test_snapshot_session_titles_returns_only_indexed_sessions(tmp_path):
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s1", "first user message")
    sm.on_session_created("s2", "another message")
    server_state.set_session_title_provider(sm.get_title_info)

    titles = server_state.snapshot_session_titles(["s1", "s2", "missing"])
    assert set(titles) == {"s1", "s2"}
    assert titles["s1"] == {"title": "first user message", "title_given": False}
    assert titles["s2"] == {"title": "another message", "title_given": False}


def test_get_title_info(tmp_path):
    sm = SessionManager(str(tmp_path))
    assert sm.get_title_info("missing") is None

    sm.on_session_created("s1", "first user message")
    assert sm.get_title_info("s1") == {
        "title": "first user message",
        "title_given": False,
    }

    sm.set_title_manually("s1", "manual title")
    assert sm.get_title_info("s1") == {
        "title": "manual title",
        "title_given": True,
    }
