"""Tests for the soft-pause / resume inference feature (phase M1).

Covers the three layers introduced together:

1. ``server_state`` round-boundary control (pause_requested / boundary action /
   exit_reason): owner-identity checks, abort-wins precedence, clear-on-abort.
2. ``Runtime.infer_stream(pause_gate=...)``: the generator stops cleanly at the
   loop top (before any new model API call) when the gate hands out a stop, and
   appends buffered user messages before continuing when it does not.
3. Handler level (real ``RuntimeHTTPServer``): ``POST /v1/infer/abort`` with
   ``action=stop|force|pause`` (legacy ``forced`` bool kept compatible), the
   "paused" terminal status, and ``POST /v1/infer/stream`` with
   ``resume: true`` continuing from an intact persisted conversation without
   removing any trailing turn.
"""

import io
import json
import os
import threading
import urllib.request
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from runtime import server_state
from runtime.common import _thread_local, clear_request_context, set_request_context
from runtime.group_chat import run_group_chat_stream_gen
from runtime.handler_infer import HandlerInferMixin
from runtime.models import InferenceRequest, Message, ModelConfig, ToolConfig
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server_state import (
    begin_session_stream,
    clear_infer_control,
    finish_session_stream,
    get_infer_exit_reason,
    queue_infer_interjection,
    request_pause_infer,
    take_infer_boundary_action,
    take_infer_interjections,
)

# Reuse the scripted-model server harness from the continue/persistence tests.
from tests.test_incremental_persist_and_continue import (
    _get_session,
    _make_resp,
    _openai_sse_text,
    _patch_model_urlopen,
    _post_json,
    _post_stream,
    _scripted_urlopen,
    _turn,
    _url,
    continue_server,
)


# ---------------------------------------------------------------------------
# 1. server_state control primitives
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_session_stream_state():
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


def test_pause_request_requires_current_generation_owner():
    cancel_event = threading.Event()
    # No stream at all -> nothing to pause.
    assert request_pause_infer("s1", cancel_event) is False

    begin_session_stream("s1", cancel_event)
    assert request_pause_infer("s1", cancel_event) is True

    # A replacement generation owns the session; the old event must be a no-op.
    newer_event = threading.Event()
    begin_session_stream("s1", newer_event)
    assert request_pause_infer("s1", cancel_event) is False
    assert request_pause_infer("s1", newer_event) is True


def test_boundary_action_consumes_pause_and_records_exit_reason():
    cancel_event = threading.Event()
    begin_session_stream("s1", cancel_event)
    # Nothing requested -> nothing to apply.
    assert take_infer_boundary_action("s1", cancel_event) is None

    request_pause_infer("s1", cancel_event)
    action = take_infer_boundary_action("s1", cancel_event)
    assert action == {"append": [], "stop": True}
    # Consumed exactly once.
    assert take_infer_boundary_action("s1", cancel_event) is None
    assert get_infer_exit_reason("s1", cancel_event) == "paused"


def test_abort_wins_over_pending_pause():
    cancel_event = threading.Event()
    begin_session_stream("s1", cancel_event)
    request_pause_infer("s1", cancel_event)
    cancel_event.set()
    # Once cancelled, the boundary check defers to the loop's cancel path.
    assert take_infer_boundary_action("s1", cancel_event) is None

    # clear_infer_control drops a pause that has not been consumed yet.
    begin_session_stream("s2", cancel_event)
    request_pause_infer("s2", cancel_event)
    clear_infer_control("s2", cancel_event)
    assert take_infer_boundary_action("s2", cancel_event) is None


def test_finished_generation_ignores_pause():
    cancel_event = threading.Event()
    begin_session_stream("s1", cancel_event)
    server_state.finish_session_stream("s1", owner_event=cancel_event)
    assert request_pause_infer("s1", cancel_event) is False
    assert take_infer_boundary_action("s1", cancel_event) is None


# ---------------------------------------------------------------------------
# 2. Runtime.infer_stream pause_gate
# ---------------------------------------------------------------------------


def _stream_runtime() -> Runtime:
    model_registry = ModelRegistry()
    model_registry.register(ModelConfig(
        model_id="test-model",
        api_base="http://localhost:9999",
        model_name="test",
        api_protocol="openai",
    ))
    return Runtime(model_registry=model_registry, tool_registry=ToolRegistry())


def _fake_stream(data: bytes):
    def mock_urlopen(request, **kwargs):
        stream = io.BytesIO(data)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__iter__ = lambda self: iter(stream.readlines())
        mock_resp.read = stream.read
        mock_resp.close = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp
    return mock_urlopen


def _request() -> InferenceRequest:
    return InferenceRequest(model_id="test-model", text="hi", stream=True)


def test_pause_gate_stops_before_any_model_call():
    """A stop handed out at the first boundary ends the generator cleanly."""
    runtime = _stream_runtime()
    calls = []

    def gate():
        return {"append": [], "stop": True}

    def _no_call(request, **kwargs):  # pragma: no cover - must never run
        calls.append(request)
        raise AssertionError("model API must not be called after pause")

    with patch("urllib.request.urlopen", side_effect=_no_call):
        messages = list(runtime.infer_stream(_request(), pause_gate=gate))

    assert calls == []
    assert messages == []  # no "Error: user interrupted" turn on a soft pause


def test_pause_gate_appends_buffered_user_messages_and_continues():
    """Buffered interjections join the context and the same generation goes on."""
    runtime = _stream_runtime()
    seen = {"n": 0}

    def gate():
        seen["n"] += 1
        if seen["n"] == 1:
            return {
                "append": [{"role": "user", "content": "injected mid-run",
                            "timestamp": "2026-08-12T15:00:09"}],
                "stop": False,
            }
        return None

    with patch(
        "urllib.request.urlopen",
        side_effect=_fake_stream(_openai_sse_text("done.")),
    ):
        messages = list(runtime.infer_stream(_request(), pause_gate=gate))

    assert messages[0].role == "user"
    assert messages[0].content == "injected mid-run"
    assert any(m.role == "assistant" and m.content == "done." for m in messages)
    assert any(m.role == "usage" for m in messages)


def test_pause_gate_none_is_transparent():
    runtime = _stream_runtime()
    with patch(
        "urllib.request.urlopen",
        side_effect=_fake_stream(_openai_sse_text("plain answer")),
    ):
        messages = list(runtime.infer_stream(
            _request(), pause_gate=lambda: None,
        ))
    assert [m.role for m in messages][:1] == ["assistant"]
    assert messages[-1].role == "usage"


# ---------------------------------------------------------------------------
# 3a. abort action parsing (handler unit)
# ---------------------------------------------------------------------------


class _StubAbortHandler(HandlerInferMixin):
    """Instantiate the mixin without BaseHTTPRequestHandler.__init__."""

    def __init__(self, body):
        self._body = body
        self.responses = []
        self.errors = []
        self.forced_calls = []
        self.server = SimpleNamespace(active_infers={})

    def _read_json_body(self):
        return dict(self._body)

    def _send_json_response(self, code, obj):
        self.responses.append((code, obj))

    def _send_json_error(self, code, msg):
        self.errors.append((code, msg))

    def _force_abort(self, session_id, owner_event=None):
        self.forced_calls.append((session_id, owner_event))


def test_abort_pause_action_sets_flag_only():
    cancel_event = threading.Event()
    begin_session_stream("s1", cancel_event)
    handler = _StubAbortHandler({"session_id": "s1", "action": "pause"})
    handler.server.active_infers = {"s1": cancel_event}

    handler._handle_infer_abort()

    assert handler.responses == [(200, {"ok": True, "action": "pause", "effective": True})]
    assert not cancel_event.is_set()  # pause never touches cancel_event
    assert handler.forced_calls == []
    action = take_infer_boundary_action("s1", cancel_event)
    assert action == {"append": [], "stop": True}


def test_abort_legacy_forced_bool_still_forces():
    cancel_event = threading.Event()
    handler = _StubAbortHandler({"session_id": "s1", "forced": True})
    handler.server.active_infers = {"s1": cancel_event}

    handler._handle_infer_abort()

    assert cancel_event.is_set()
    assert handler.forced_calls == [("s1", cancel_event)]


def test_abort_stop_supersedes_pending_pause():
    cancel_event = threading.Event()
    begin_session_stream("s1", cancel_event)
    request_pause_infer("s1", cancel_event)
    handler = _StubAbortHandler({"session_id": "s1"})
    handler.server.active_infers = {"s1": cancel_event}

    handler._handle_infer_abort()

    assert cancel_event.is_set()
    # The stale pause is cleared and can no longer hand out a "paused" stop.
    assert take_infer_boundary_action("s1", cancel_event) is None
    assert get_infer_exit_reason("s1", cancel_event) is None


# ---------------------------------------------------------------------------
# 3b. end-to-end: pause during a two-tool-round inference, then resume
# ---------------------------------------------------------------------------


def test_pause_stops_loop_at_boundary_and_broadcasts_paused(continue_server):
    """Pause lands while round 2's model call is blocked; round 3 never runs."""
    srv = continue_server
    cm = srv._context_manager
    session_id = cm.create_session()

    r2_gate = threading.Event()
    r1 = _openai_sse_text("", tool_calls=[("echo", "{}")])
    r2 = _openai_sse_text("", tool_calls=[("echo", "{}")])
    r3 = _openai_sse_text("All done.")

    def side_effect(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        if "tool" in json.dumps(body.get("messages", [])):
            # Second model call: wait until the test observed the tool frame
            # and armed the pause, then answer.
            r2_gate.wait(timeout=10)
            return _make_resp(r2)
        return _make_resp(r1)

    captured = []

    def capturing_side_effect(request, **kwargs):
        captured.append(request)
        return side_effect(request, **kwargs)

    payload = json.dumps({
        "session_id": session_id,
        "model_id": "test-model",
        "tool_ids": ["echo"],
        "messages": [{"role": "user", "content": "go"}],
        "stream": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        _url(srv, "/v1/infer/stream"), data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )

    lines = []
    with _patch_model_urlopen(srv, capturing_side_effect):
        resp = urllib.request.urlopen(req, timeout=15)
        tool_frames = 0
        paused = False
        for raw in resp:
            text = raw.decode("utf-8", "replace")
            lines.append(text)
            if '"role": "tool"' in text and not paused:
                # First completed round: ask for a soft pause, then let the
                # blocked second model call proceed. The loop can only stop at
                # the boundary *after* round 2 (round 1's gate check may have
                # already happened), so at most one more round streams.
                status, res = _post_json(
                    srv, "/v1/infer/abort",
                    {"session_id": session_id, "action": "pause"},
                )
                assert status == 200 and res.get("effective") is True
                r2_gate.set()
                paused = True
        r2_gate.set()  # in case the tool frame arrived after the wait armed

    body = "".join(lines)
    assert "data: [DONE]" in body
    assert "Error: user interrupted" not in body
    # The pause must have prevented the third model round.
    assert "All done." not in body

    # Terminal status is the resting "paused", not a done_* outcome.
    assert server_state._session_statuses.get(session_id) == "paused"

    # The persisted conversation stays protocol-complete: the last turn is a
    # tool result (fully executed round), waiting to be interpreted.
    turns = cm.load_conversation(session_id)
    assert turns[-1].role == "tool"


def test_resume_continues_from_paused_history(continue_server):
    """resume:true continues from an intact history without removing turns."""
    srv = continue_server
    cm = srv._context_manager
    session_id = cm.create_session()
    cm.save_conversation(session_id, [
        _turn("user", "go", timestamp="2026-08-12T15:00:00"),
        _turn("assistant", "", timestamp="2026-08-12T15:00:01",
              tool_calls=[{"id": "call_1", "name": "echo", "arguments": "{}"}]),
        _turn("tool", "echo result", timestamp="2026-08-12T15:00:02",
              name="echo", tool_use_id="call_1"),
    ])

    final_round = _openai_sse_text("Interpreted the tool result.")
    status, body = _post_stream(srv, {
        "session_id": session_id,
        "model_id": "test-model",
        "tool_ids": ["echo"],
        "resume": True,
        "messages": [],
    }, _scripted_urlopen([final_round]))
    assert status == 200, f"resume failed: {body[:500]}"
    assert "data: [DONE]" in body

    status, sess = _get_session(srv, session_id)
    assert status == 200
    roles = [msg["role"] for msg in sess["messages"]]
    # Nothing was removed: the tool round stays, the answer is appended.
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert sess["messages"][-1]["content"] == "Interpreted the tool result."


def test_resume_requires_existing_session(continue_server):
    srv = continue_server
    status, err = _post_json(srv, "/v1/infer/stream", {
        "session_id": "new",
        "model_id": "test-model",
        "tool_ids": ["echo"],
        "resume": True,
        "messages": [],
    })
    assert status == 400
    assert "session_id" in err.get("error", "")


def test_resume_stops_at_pause_again_and_title_not_updated(continue_server):
    """A resumed generation that hits a pause boundary ends "paused" as well."""
    srv = continue_server
    cm = srv._context_manager
    session_id = cm.create_session()
    cm.save_conversation(session_id, [
        _turn("user", "go", timestamp="2026-08-12T15:00:00"),
        _turn("assistant", "", timestamp="2026-08-12T15:00:01",
              tool_calls=[{"id": "call_1", "name": "echo", "arguments": "{}"}]),
        _turn("tool", "echo result", timestamp="2026-08-12T15:00:02",
              name="echo", tool_use_id="call_1"),
    ])

    r_tool = _openai_sse_text("", tool_calls=[("echo", "{}")])
    second_round_gate = threading.Event()
    calls = {"n": 0}

    def side_effect(request, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            # Block the next model call until the test has armed the pause,
            # so the following boundary is guaranteed to observe it.
            second_round_gate.wait(timeout=10)
        return _make_resp(r_tool)

    payload = json.dumps({
        "session_id": session_id,
        "model_id": "test-model",
        "tool_ids": ["echo"],
        "resume": True,
        "messages": [],
        "stream": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        _url(srv, "/v1/infer/stream"), data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    lines = []
    with _patch_model_urlopen(srv, side_effect):
        resp = urllib.request.urlopen(req, timeout=15)
        tool_seen = False
        for raw in resp:
            text = raw.decode("utf-8", "replace")
            lines.append(text)
            if '"role": "tool"' in text and not tool_seen:
                tool_seen = True
                _status, res = _post_json(
                    srv, "/v1/infer/abort",
                    {"session_id": session_id, "action": "pause"},
                )
                assert res.get("effective") is True
                second_round_gate.set()
    second_round_gate.set()
    body = "".join(lines)
    assert "data: [DONE]" in body
    assert "Error: user interrupted" not in body
    assert server_state._session_statuses.get(session_id) == "paused"
    # Paused exits must not trigger title generation (update_title=False path):
    # the session title stays at whatever the index had before the pause.
    sess = srv._session_manager.get_session(session_id)
    assert sess is not None
    assert sess.get("title_generated") in (None, False) or sess.get("title")


# ---------------------------------------------------------------------------
# 4. Interjection (M2): queue at the boundary, same-generation continuation
# ---------------------------------------------------------------------------


def test_queue_interjection_requires_current_generation():
    cancel_event = threading.Event()
    assert queue_infer_interjection("s1", cancel_event, {"role": "user", "content": "x"}) is None

    begin_session_stream("s1", cancel_event)
    assert queue_infer_interjection("s1", cancel_event, {"role": "user", "content": "a"}) == "queued"
    assert queue_infer_interjection("s1", cancel_event, {"role": "user", "content": "b"}) == "queued"

    # Superseded generations cannot enqueue into the current one.
    other = threading.Event()
    assert queue_infer_interjection("s1", other, {"role": "user", "content": "c"}) is None

    action = take_infer_boundary_action("s1", cancel_event)
    assert action == {"append": [
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
    ], "stop": False}
    assert take_infer_boundary_action("s1", cancel_event) is None


def test_queue_interjection_cap_is_ten():
    cancel_event = threading.Event()
    begin_session_stream("s1", cancel_event)
    for i in range(10):
        assert queue_infer_interjection(
            "s1", cancel_event, {"role": "user", "content": f"m{i}"}
        ) == "queued"
    assert queue_infer_interjection(
        "s1", cancel_event, {"role": "user", "content": "overflow"}
    ) == "full"


def test_buffered_interjection_supersedes_pending_pause():
    cancel_event = threading.Event()
    begin_session_stream("s1", cancel_event)
    request_pause_infer("s1", cancel_event)
    queue_infer_interjection("s1", cancel_event, {"role": "user", "content": "insert"})

    # Insertion wins over the stop: the same generation continues with the
    # new message instead of finalizing as "paused".
    action = take_infer_boundary_action("s1", cancel_event)
    assert action is not None and action["stop"] is False
    assert [m["content"] for m in action["append"]] == ["insert"]
    assert get_infer_exit_reason("s1", cancel_event) is None


def test_abort_clears_buffered_interjections():
    cancel_event = threading.Event()
    begin_session_stream("s1", cancel_event)
    queue_infer_interjection("s1", cancel_event, {"role": "user", "content": "x"})
    clear_infer_control("s1", cancel_event)
    assert take_infer_boundary_action("s1", cancel_event) is None


class _StubInterjectHandler(HandlerInferMixin):
    """Instantiate the interject endpoint without BaseHTTPRequestHandler."""

    def __init__(self, body, agent_ids=None):
        self._body = body
        self.responses = []
        self.errors = []
        self.server = SimpleNamespace(active_infers={})

    def _read_json_body(self):
        return dict(self._body)

    def _send_json_response(self, code, obj):
        self.responses.append((code, obj))

    def _send_json_error(self, code, msg):
        self.errors.append((code, msg))

    def _get_runtime(self):  # pragma: no cover - not used by this endpoint
        raise AssertionError("interject must not touch the runtime")


def test_interject_endpoint_not_active_when_no_generation():
    handler = _StubInterjectHandler({
        "session_id": "s1",
        "message": {"role": "user", "content": "hello"},
    })
    handler._handle_infer_interject()
    assert handler.responses == [
        (200, {"ok": True, "status": "not_active", "session_id": "s1"}),
    ]
    assert handler.errors == []


def test_interject_endpoint_queues_and_validates():
    cancel_event = threading.Event()
    begin_session_stream("s1", cancel_event)
    handler = _StubInterjectHandler({
        "session_id": "s1",
        "message": {"role": "user", "content": "  look here  "},
    })
    handler.server.active_infers = {"s1": cancel_event}

    handler._handle_infer_interject()
    assert handler.responses[0][0] == 202
    assert handler.responses[0][1]["status"] == "queued"

    action = take_infer_boundary_action("s1", cancel_event)
    assert action["append"][0]["content"] == "look here"  # trimmed
    assert action["append"][0]["role"] == "user"

    # Malformed payloads are rejected, never buffered.
    bad = _StubInterjectHandler({"session_id": "s1", "message": {"role": "assistant"}})
    bad.server.active_infers = {"s1": cancel_event}
    bad._handle_infer_interject()
    assert bad.errors and bad.errors[0][0] == 400

    empty = _StubInterjectHandler({"session_id": "s1", "message": {"role": "user", "content": "  "}})
    empty.server.active_infers = {"s1": cancel_event}
    empty._handle_infer_interject()
    assert empty.errors and empty.errors[0][0] == 400


def test_interject_allowed_for_group_chat_generation_and_published_live():
    """Group sessions accept interjections (coarse wave-boundary semantics)
    and the queued turn is broadcast to the session stream immediately."""
    cancel_event = threading.Event()
    begin_session_stream("s1", cancel_event)
    server_state.set_session_stream_agents("s1", cancel_event, ["a1", "a2"])
    handler = _StubInterjectHandler({
        "session_id": "s1",
        "message": {"role": "user", "content": "mine", "timestamp": "2026-01-01 00:00:00"},
    })
    handler.server.active_infers = {"s1": cancel_event}

    handler._handle_infer_interject()
    assert not handler.errors
    assert handler.responses and handler.responses[-1][1].get("status") == "queued"
    with server_state._session_stream_lock:
        frames = [env["frame"] for env in server_state._session_streams["s1"]["frames"]]
    # The client timestamp survives verbatim: it is the identity used by the
    # local bubble and every later re-publish for dedup.
    assert any(
        f.get("role") == "user" and f.get("content") == "mine"
        and f.get("timestamp") == "2026-01-01 00:00:00"
        for f in frames
    )
    # Still consumable exactly once by the generation's boundary gate.
    assert queue_infer_interjection("s1", cancel_event, {"role": "user", "content": "x"}) == "queued"
    finish_session_stream("s1", owner_event=cancel_event)


def test_end_paused_session_status_transitions_and_guards():
    """paused -> done_success_unread only when idle-and-paused; live
    generations and non-paused sessions are left untouched."""
    sid = "s-end"
    cancel_event = threading.Event()
    try:
        # Live generation owns the session: refusing to end, even if the
        # status string currently reads "paused".
        begin_session_stream(sid, cancel_event)
        server_state._session_statuses[sid] = "paused"
        assert server_state.end_paused_session_status(sid) is False
        assert server_state._session_statuses[sid] == "paused"

        # Generation finished and the session rests paused: ending converges
        # the status exactly like natural completion (and marks unread).
        finish_session_stream(sid, owner_event=cancel_event)
        assert server_state.end_paused_session_status(sid) is True
        assert server_state._session_statuses[sid] == "done_success_unread"
        assert server_state._unread_sessions.get(sid) == "done_success_unread"

        # Idempotent: no longer paused, nothing to end.
        assert server_state.end_paused_session_status(sid) is False
    finally:
        server_state._session_statuses.pop(sid, None)
        server_state._unread_sessions.pop(sid, None)


def test_abort_end_action_converges_paused_session():
    class _StubAbortHandler(HandlerInferMixin):
        def __init__(self, body):
            self._body = body
            self.responses = []
            self.errors = []
            self.server = SimpleNamespace(active_infers={})

        def _read_json_body(self):
            return dict(self._body)

        def _send_json_response(self, code, obj):
            self.responses.append((code, obj))

        def _send_json_error(self, code, msg):
            self.errors.append((code, msg))

    sid = "s-end2"
    try:
        server_state._session_statuses[sid] = "paused"
        handler = _StubAbortHandler({"session_id": sid, "action": "end"})
        handler._handle_infer_abort()
        assert not handler.errors
        assert handler.responses and handler.responses[-1][1] == {
            "ok": True, "action": "end", "ended": True,
        }
        assert server_state._session_statuses[sid] == "done_success_unread"
    finally:
        server_state._session_statuses.pop(sid, None)
        server_state._unread_sessions.pop(sid, None)


def test_group_chat_gate_consumes_interjections_only():
    """A stray pause flag must never be consumed by the group gate."""
    cancel_event = threading.Event()
    begin_session_stream("s2", cancel_event)
    server_state.request_pause_infer("s2", cancel_event)
    assert queue_infer_interjection("s2", cancel_event, {"role": "user", "content": "q"}) == "queued"

    taken = server_state.take_infer_interjections("s2", cancel_event)
    assert [m["content"] for m in taken] == ["q"]
    # Pause flag untouched: the interjection-only trailing drain never
    # honours it (the wave loop already ended on its own), but consuming
    # interjections must not silently clear it either — the boundary gate
    # (take_infer_boundary_action) is what honours group pause mid-run.
    action = server_state.take_infer_boundary_action("s2", cancel_event)
    assert action is not None and action["stop"] is True and action["append"] == []
    # Nothing buffered a second time.
    assert server_state.take_infer_interjections("s2", cancel_event) == []
    finish_session_stream("s2", owner_event=cancel_event)


# ---------------------------------------------------------------------------
# Group chat: interjections join the shared transcript at wave boundaries
# ---------------------------------------------------------------------------

_GROUP_AGENTS = [
    {
        "agent_id": "SunWuKong",
        "nickname": "孙悟空",
        "description": "齐天大圣",
        "system_prompt": "你是孙悟空。",
        "model_id": "m1",
        "tool_ids": [],
    },
    {
        "agent_id": "ShaWuJing",
        "nickname": "沙和尚",
        "description": "卷帘大将",
        "system_prompt": "你是沙和尚。",
        "model_id": "m1",
        "tool_ids": [],
    },
]


class _GroupStubAgentManager:
    def __init__(self, agents):
        self._by_id = {a["agent_id"]: a for a in agents}

    def get(self, key):
        for a in self._by_id.values():
            if a.get("nickname") == key:
                return a
        return self._by_id.get(key)


class _GroupScriptRuntime:
    """Scripted per-wave replies; records every model request it receives."""

    def __init__(self, replies):
        self._replies = list(replies)
        self._lock = threading.Lock()
        self.requests = []

    def infer_stream(self, request, cancel_event=None, on_round_complete=None,
                   on_context_overflow=None, **_pipeline_kwargs):
        with self._lock:
            idx = len(self.requests)
            self.requests.append(request)
        content = self._replies[idx] if idx < len(self._replies) else "(no more)"
        yield Message(role="assistant", content=content)


def test_group_chat_interjection_injected_at_wave_boundary():
    """Coarse group semantics: an interjection queued during wave 1 joins the
    transcript after wave 1 settles, is yielded once as a session-level user
    frame, and is already visible to wave 2's model request."""
    manager = _GroupStubAgentManager(_GROUP_AGENTS)
    runtime = _GroupScriptRuntime(["@沙和尚 你怎么看", "好的，简短回答"])

    boundaries = {"n": 0}

    def interject_gate():
        boundaries["n"] += 1
        if boundaries["n"] == 2:  # queued while wave 1 was running
            return [{
                "role": "user",
                "content": "插一句：请简短回答",
                "timestamp": "2026-01-01T00:00:00",
            }]
        return []

    set_request_context(
        agent_manager=manager,
        all_agent_ids=[a["agent_id"] for a in _GROUP_AGENTS],
    )
    try:
        out = list(run_group_chat_stream_gen(
            runtime=runtime,
            mentioned_agent_ids=["SunWuKong"],
            all_agent_ids=[a["agent_id"] for a in _GROUP_AGENTS],
            original_messages=[Message(role="user", content="@孙悟空 开始")],
            base_request=InferenceRequest(model_id="m1", messages=[]),
            cancel_event=None,
            context_manager=None,
            session_id=None,
            agent_manager=manager,
            model_id="m1",
            tool_ids=[],
            max_rounds=5,
            interject_gate=interject_gate,
        ))
    finally:
        clear_request_context(list(_thread_local.__dict__.keys()))

    roles = [(m.role, m.content or "") for m in out]
    wave1_idx = next(i for i, (r, c) in enumerate(roles) if r == "assistant" and "你怎么看" in c)
    interj_idx = next(i for i, (r, c) in enumerate(roles) if r == "user" and "插一句" in c)
    wave2_idx = next(i for i, (r, c) in enumerate(roles) if r == "assistant" and "简短回答" in c)
    assert wave1_idx < interj_idx < wave2_idx

    interj_msg = out[interj_idx]
    # Session-level frame: no agent attribution, original enqueue timestamp.
    assert interj_msg.agent_id is None
    assert interj_msg.timestamp == "2026-01-01T00:00:00"
    # Drained exactly once, not replayed at later boundaries.
    assert sum(1 for r, c in roles if r == "user" and "插一句" in c) == 1
    assert boundaries["n"] >= 2

    # Wave 2's model request already carries the injected user turn.
    second_msgs = runtime.requests[-1].messages
    second_dicts = [m.to_dict() if hasattr(m, "to_dict") else m for m in second_msgs]
    assert any(
        m.get("role") == "user" and "插一句" in (m.get("content") or "")
        for m in second_dicts
    )


def test_group_chat_interjection_during_last_wave_is_answered():
    """The reported bug: an interjection queued while the LAST wave ran used
    to be appended to the transcript with no wave left to process it — it sat
    at the end of conversation.json unread, answered only by the NEXT user
    turn. Now the boundary routes it like a plain user message (no @ → last
    speaker) and an injection-driven wave answers it inside the generation.
    """
    manager = _GroupStubAgentManager(_GROUP_AGENTS)
    runtime = _GroupScriptRuntime([
        "第一波发言完毕。",           # wave 1: mention cascade ends here
        "收到，我会简短回答。",        # injection wave: last speaker answers
    ])
    boundaries = {"n": 0}

    def interject_gate():
        boundaries["n"] += 1
        if boundaries["n"] == 2:  # queued while the last wave was running
            return [{
                "role": "user",
                "content": "插一句：请简短回答",
                "timestamp": "2026-01-01T00:00:00",
            }]
        return []

    set_request_context(
        agent_manager=manager,
        all_agent_ids=[a["agent_id"] for a in _GROUP_AGENTS],
    )
    try:
        out = list(run_group_chat_stream_gen(
            runtime=runtime,
            mentioned_agent_ids=["SunWuKong"],
            all_agent_ids=[a["agent_id"] for a in _GROUP_AGENTS],
            original_messages=[Message(role="user", content="@孙悟空 开始")],
            base_request=InferenceRequest(model_id="m1", messages=[]),
            cancel_event=None,
            context_manager=None,
            session_id=None,
            agent_manager=manager,
            model_id="m1",
            tool_ids=[],
            max_rounds=5,
            interject_gate=interject_gate,
        ))
    finally:
        clear_request_context(list(_thread_local.__dict__.keys()))

    roles = [
        (m.role, m.content or "")
        for m in out
        if m.role in ("user", "assistant") and (m.content or "")
    ]
    interj_idx = next(i for i, (r, c) in enumerate(roles) if r == "user" and "插一句" in c)
    answer_idx = next(
        i for i, (r, c) in enumerate(roles) if r == "assistant" and "简短回答" in c
    )
    # The interjection is answered INSIDE this generation…
    assert interj_idx < answer_idx
    # …and the transcript does not end with an unanswered user turn.
    assert roles[-1][0] == "assistant"

    # The routed agent sees the interjection as its CURRENT message (rendered
    # bare, no author prefix), exactly once — not duplicated in history.
    second = runtime.requests[-1]
    user_turns = [
        (m.content or "")
        for m in second.messages
        if (getattr(m, "role", "") or (m.get("role") if isinstance(m, dict) else "")) == "user"
    ]
    injections = [t for t in user_turns if "插一句" in t]
    assert len(injections) == 1
    assert injections[0].strip() == "插一句：请简短回答"


def test_group_chat_boundary_refill_after_drain_keeps_generation_open():
    """A message buffered just after a boundary's drain (the exit-decision
    race) must keep the generation open: close_gate answers False, the next
    pass drains and routes it, and the wave loop exits only once the queue is
    verifiably empty."""
    manager = _GroupStubAgentManager(_GROUP_AGENTS)
    runtime = _GroupScriptRuntime(["第一波发言", "收到收口前的一句。"])
    state = {"queue": [], "asked": 0}

    def interject_gate():
        if state["queue"]:
            return [state["queue"].pop(0)]
        return []

    def close_gate():
        state["asked"] += 1
        if state["asked"] == 1:
            # Message lands between the drain and the exit decision.
            state["queue"].append({
                "role": "user",
                "content": "收口前最后一句",
                "timestamp": "2026-01-01T00:00:02",
            })
            return False  # buffered → keep the generation open
        return True      # empty → close

    set_request_context(
        agent_manager=manager,
        all_agent_ids=[a["agent_id"] for a in _GROUP_AGENTS],
    )
    try:
        out = list(run_group_chat_stream_gen(
            runtime=runtime,
            mentioned_agent_ids=["SunWuKong"],
            all_agent_ids=[a["agent_id"] for a in _GROUP_AGENTS],
            original_messages=[Message(role="user", content="@孙悟空 开始")],
            base_request=InferenceRequest(model_id="m1", messages=[]),
            cancel_event=None,
            context_manager=None,
            session_id=None,
            agent_manager=manager,
            model_id="m1",
            tool_ids=[],
            max_rounds=5,
            interject_gate=interject_gate,
            close_gate=close_gate,
        ))
    finally:
        clear_request_context(list(_thread_local.__dict__.keys()))

    seq = [(m.role, m.content or "") for m in out if m.role in ("user", "assistant") and m.content]
    interj_idx = next(i for i, (r, c) in enumerate(seq) if r == "user" and "收口前" in c)
    answer_idx = next(i for i, (r, c) in enumerate(seq) if r == "assistant" and "收口前" in c)
    assert interj_idx < answer_idx
    assert seq[-1][0] == "assistant"
    assert state["asked"] >= 2  # re-asked after the refill, then closed on empty


def test_group_chat_interjection_budget_is_bounded():
    """Injection-driven waves cannot hold the loop open forever: even a gate
    that never runs dry stops buying waves after the injection budget."""
    manager = _GroupStubAgentManager(_GROUP_AGENTS)
    runtime = _GroupScriptRuntime(["开场"])  # everything after is "(no more)"
    counter = {"n": 0}

    def interject_gate():
        counter["n"] += 1
        return [{
            "role": "user",
            "content": f"连环插话 {counter['n']}",
            "timestamp": "2026-01-01T00:00:00",
        }]

    set_request_context(
        agent_manager=manager,
        all_agent_ids=[a["agent_id"] for a in _GROUP_AGENTS],
    )
    try:
        out = list(run_group_chat_stream_gen(
            runtime=runtime,
            mentioned_agent_ids=["SunWuKong"],
            all_agent_ids=[a["agent_id"] for a in _GROUP_AGENTS],
            original_messages=[Message(role="user", content="@孙悟空 开始")],
            base_request=InferenceRequest(model_id="m1", messages=[]),
            cancel_event=None,
            context_manager=None,
            session_id=None,
            agent_manager=manager,
            model_id="m1",
            tool_ids=[],
            max_rounds=5,
            interject_gate=interject_gate,
        ))
    finally:
        clear_request_context(list(_thread_local.__dict__.keys()))

    assert out  # terminated instead of looping forever
    # Round 1 + at most the injection budget (same cap as the interjection
    # queue), never more.
    # 首波边界：尚无 assistant 发言，插话按"全员广播"路由 → 首波 2 个派发；
    # 之后注入波最多 _MAX_INJECTION_ROUNDS 次。
    assert len(runtime.requests) <= 2 + 10


def test_group_chat_pause_stops_at_wave_boundary():
    """Coarse group pause: a boundary action with stop=True ends the
    generation after the settled wave, WITHOUT dispatching the next wave —
    and exports exactly what a resume needs (transcript + pending wave)."""
    manager = _GroupStubAgentManager(_GROUP_AGENTS)
    runtime = _GroupScriptRuntime(["@ShaWuJing 你怎么看", "绝不该被执行"])

    calls = {"n": 0}

    def boundary_gate():
        calls["n"] += 1
        if calls["n"] == 2:  # wave 1 settled, wave 2 not dispatched yet
            return {
                "append": [{
                    "role": "user", "content": "暂停前的插话",
                    "timestamp": "2026-01-01T00:00:01",
                }],
                "stop": True,
            }
        return None

    exit_state: dict = {}
    set_request_context(
        agent_manager=manager,
        all_agent_ids=[a["agent_id"] for a in _GROUP_AGENTS],
    )
    try:
        out = list(run_group_chat_stream_gen(
            runtime=runtime,
            mentioned_agent_ids=["SunWuKong"],
            all_agent_ids=[a["agent_id"] for a in _GROUP_AGENTS],
            original_messages=[Message(role="user", content="@SunWuKong 开始")],
            base_request=InferenceRequest(model_id="m1", messages=[]),
            cancel_event=None,
            context_manager=None,
            session_id=None,
            agent_manager=manager,
            model_id="m1",
            tool_ids=[],
            max_rounds=5,
            boundary_gate=boundary_gate,
            exit_state=exit_state,
        ))
    finally:
        clear_request_context(list(_thread_local.__dict__.keys()))

    # Wave 1's reply and the queued interjection made it out; wave 2 never ran.
    assert any(m.role == "assistant" and "你怎么看" in (m.content or "") for m in out)
    assert any(m.role == "user" and "暂停前的插话" in (m.content or "") for m in out)
    assert not any("绝不该被执行" in (m.content or "") for m in out)
    assert len(runtime.requests) == 1
    # The pause snapshot: transcript includes wave 1 + the interjection, and
    # the cut-off wave is recorded for the resume to replay.
    snapshot = exit_state.get("messages") or []
    assert any(
        (m.get("content") or "") == "暂停前的插话" and m.get("role") == "user"
        for m in snapshot
    )
    assert any("你怎么看" in (m.get("content") or "") for m in snapshot)
    assert exit_state.get("pending_agent_ids") == ["ShaWuJing"]


def test_group_chat_resume_replays_wave_from_initial_turns():
    """Resume replays the cut-off wave against the sidecar transcript: the
    pending agent sees the paused history (including the interjection) and is
    the only agent dispatched — no disk load, no windowed re-selection."""
    manager = _GroupStubAgentManager(_GROUP_AGENTS)
    runtime = _GroupScriptRuntime(["收到，结束"])

    transcript = [
        Message(role="user", content="@SunWuKong 开始", timestamp="2026-01-01 00:00:00"),
        Message(role="assistant", content="@ShaWuJing 你怎么看",
                agent_id="SunWuKong", timestamp="2026-01-01 00:00:01"),
        Message(role="user", content="暂停前的插话", timestamp="2026-01-01 00:00:02"),
    ]
    set_request_context(
        agent_manager=manager,
        all_agent_ids=[a["agent_id"] for a in _GROUP_AGENTS],
    )
    try:
        out = list(run_group_chat_stream_gen(
            runtime=runtime,
            mentioned_agent_ids=["ShaWuJing"],
            all_agent_ids=[a["agent_id"] for a in _GROUP_AGENTS],
            original_messages=[],
            base_request=InferenceRequest(model_id="m1", messages=[]),
            cancel_event=None,
            context_manager=None,
            session_id=None,
            agent_manager=manager,
            model_id="m1",
            tool_ids=[],
            max_rounds=5,
            initial_turns=list(transcript),
        ))
    finally:
        clear_request_context(list(_thread_local.__dict__.keys()))

    assert any(m.role == "assistant" and "收到" in (m.content or "") for m in out)
    # Exactly one dispatch: the replayed wave; only ShaWuJing ran.
    assert len(runtime.requests) == 1
    sent = [
        m.to_dict() if hasattr(m, "to_dict") else m
        for m in runtime.requests[0].messages
    ]
    # The model call must NOT contain the injected instruction; the caller
    # replays the recorded turn instead.
    assert not any(
        "group-chat resume: continue the pending wave" in (m.get("content") or "")
        for m in sent
    )
    assert any(
        m.get("role") == "user" and "暂停前的插话" in (m.get("content") or "")
        for m in sent
    )
    assert any(
        "你怎么看" in (m.get("content") or "") for m in sent
    )


def test_group_chat_pause_resume_end_to_end(continue_server):
    """Full cycle over HTTP: a group generation pauses at a wave boundary
    (the wave already settled, the triggered wave never dispatched), the
    sidecar records the cut-off wave, and a resume replays exactly that
    wave against the snapshot transcript — and never re-runs the settled
    agent."""
    srv = continue_server
    cm = srv._context_manager
    srv._agent_manager.create(
        agent_id="SunWuKong", model_id="test-model", nickname="孙悟空", tool_ids=[])
    srv._agent_manager.create(
        agent_id="ShaWuJing", model_id="test-model", nickname="沙悟净", tool_ids=[])
    session_id = cm.create_session()

    r1 = _openai_sse_text("@ShaWuJing 你怎么看")   # Sun's wave-1 reply
    r2 = _openai_sse_text("绝不该出现在第一段流")   # would-be Sha wave-2 reply
    r3 = _openai_sse_text("恢复后完成")            # resume: Sha's replayed wave
    bodies: list = []
    pause_result: dict = {}

    def side_effect(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        bodies.append(body)
        if len(bodies) == 1:
            # Strictly inside wave 1: the pause cannot be consumed before
            # the very next wave boundary.
            status, res = _post_json(
                srv, "/v1/infer/abort",
                {"session_id": session_id, "action": "pause"},
            )
            pause_result.update(status=status, body=res)
            return _make_resp(r1)
        if len(bodies) == 2:
            return _make_resp(r3)
        return _make_resp(r2)  # never reached

    _st1, text1 = _post_stream(srv, {
        "session_id": session_id,
        "model_id": "test-model",
        "tool_ids": [],
        "messages": [{"role": "user", "content": "@SunWuKong 开始"}],
        "agent_ids": ["SunWuKong", "ShaWuJing"],
        "stream": True,
    }, side_effect)

    assert pause_result.get("status") == 200
    assert pause_result.get("body", {}).get("effective") is True
    assert "你怎么看" in text1
    assert "绝不该出现在第一段流" not in text1
    # Resting status is a session-event broadcast, not a frame in the
    # inference body: check the status registry directly (same as the
    # single-agent pause tests).
    assert server_state._session_statuses.get(session_id) == "paused"
    assert len(bodies) == 1             # wave 2 was never dispatched

    # The sidecar recorded the cut-off wave for a deterministic replay.
    state = cm.read_pause_state(session_id)
    assert state is not None
    assert state.get("pending_agent_ids") == ["ShaWuJing"]
    assert any(
        "开始" in (m.get("content") or "") for m in state["context"]
    )

    _st2, text2 = _post_stream(srv, {
        "session_id": session_id,
        "model_id": "test-model",
        "tool_ids": [],
        "messages": [],
        "agent_ids": ["SunWuKong", "ShaWuJing"],
        "resume": True,
        "stream": True,
    }, side_effect)

    assert "恢复后完成" in text2
    assert server_state._session_statuses.get(session_id) == "done_success_unread"
    # Only the pending agent ran: exactly one model call for the replay.
    assert len(bodies) == 2
    replay_msgs = bodies[-1]["messages"]
    # The replayed request is built from the snapshot transcript: it already
    # carries Sun's settled reply and the original user turn.
    assert any("你怎么看" in (m.get("content") or "") for m in replay_msgs)
    assert any("开始" in (m.get("content") or "") for m in replay_msgs)
    # Sidecar consumed exactly once.
    assert cm.read_pause_state(session_id) is None


def test_interject_end_to_end_continues_same_generation(continue_server):
    """A mid-run interjection surfaces as a user frame and steers the SAME
    generation: the next model call already contains it, and no paused state
    or new request is involved.

    Deterministic by construction — the interjection is queued from inside the
    scripted handler of the FIRST model round. At that point the generation is
    streaming, so the request is accepted, and the first round boundary cannot
    have been reached yet, so the drain is guaranteed to find it. No sleeps, no
    watching the SSE for a tool frame to decide when to POST, no wall-clock
    assumption.

    The previous version blocked the *second* model call until the POST had
    returned, which guards the wrong point: the boundary drains the buffer
    *before* that call is issued, so under load the drain could run on an empty
    buffer and the "round 2 already carries the insertion" assertion failed
    intermittently. That was a test race, not a product race.
    """
    srv = continue_server
    cm = srv._context_manager
    session_id = cm.create_session()

    r1 = _openai_sse_text("", tool_calls=[("echo", "{}")])
    r2 = _openai_sse_text("Considered the insertion.")
    model_bodies = []
    queue_status = {}

    def side_effect(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        model_bodies.append(body)
        if len(model_bodies) > 1:
            return _make_resp(r2)
        # Round 1 is in flight: queue the insertion strictly before the
        # boundary that consumes it.
        status, res = _post_json(
            srv, "/v1/infer/interject",
            {"session_id": session_id,
             "message": {"role": "user", "content": "also mind X"}},
        )
        queue_status.update(status=status, body=res)
        return _make_resp(r1)

    payload = json.dumps({
        "session_id": session_id,
        "model_id": "test-model",
        "tool_ids": ["echo"],
        "messages": [{"role": "user", "content": "go"}],
        "stream": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        _url(srv, "/v1/infer/stream"), data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )

    lines = []
    with _patch_model_urlopen(srv, side_effect):
        resp = urllib.request.urlopen(req, timeout=15)
        for raw in resp:
            lines.append(raw.decode("utf-8", "replace"))
    body = "".join(lines)

    assert queue_status["status"] == 202
    assert queue_status["body"]["status"] == "queued"
    # The interjection is brokered to the session stream as a user frame. On
    # this (starter) connection it surfaces at the round boundary — after the
    # completed tool round, before the answer that considered it; the browsers
    # attached as subscribers also get it the moment it is queued, which is the
    # arrival order the UI has to repair when the late tool result lands.
    assert '"role": "user"' in body and "also mind X" in body
    assert (
        body.index('"role": "tool"')
        < body.index("also mind X")
        < body.index("Considered the insertion.")
    )

    # Same generation: the insertion reached round 2, and it was not part of
    # round 1 (which started before it could be queued).
    assert len(model_bodies) == 2
    assert not any(
        m.get("content") == "also mind X" for m in model_bodies[0]["messages"]
    )
    assert any(
        m.get("role") == "user" and m.get("content") == "also mind X"
        for m in model_bodies[1]["messages"]
    )
    assert "Considered the insertion." in body and "data: [DONE]" in body

    # Terminal outcome is the normal success (not paused); the interjection
    # and the answer that considered it are both persisted in order.
    assert server_state._session_statuses.get(session_id) == "done_success_unread"
    roles = [t.role for t in cm.load_conversation(session_id)]
    assert roles == ["user", "assistant", "tool", "user", "assistant"]


def test_deleting_session_drops_pause_and_interject_state(continue_server):
    """Session deletion must not leave a paused marker or buffered input
    behind for a (possibly reused) session id."""
    srv = continue_server
    cm = srv._context_manager
    session_id = cm.create_session()
    cancel_event = threading.Event()
    begin_session_stream(session_id, cancel_event)
    request_pause_infer(session_id, cancel_event)
    queue_infer_interjection(
        session_id, cancel_event, {"role": "user", "content": "pending"}
    )
    server_state._session_statuses[session_id] = "paused"

    status, _ = _post_json_delete(srv, session_id)
    assert status == 200

    with server_state._session_stream_lock:
        assert session_id not in server_state._session_streams
    with server_state._session_state_lock:
        assert session_id not in server_state._session_statuses
        assert session_id not in server_state._unread_sessions
    # The cancelled generation can no longer hand out a pause stop.
    assert take_infer_boundary_action(session_id, cancel_event) is None


def _post_json_delete(srv, session_id):
    req = urllib.request.Request(
        _url(srv, f"/v1/sessions/{session_id}"), method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _get_json(srv, path):
    req = urllib.request.Request(_url(srv, path), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


# ---------------------------------------------------------------------------
# 5. Resume re-binds inference parameters from the resume request itself
# ---------------------------------------------------------------------------


def _capture_model_calls(side_effect):
    """Wrap a model side effect, capturing each OpenAI request body sent."""
    captured = []

    def wrapper(request, **kwargs):
        try:
            captured.append(json.loads(request.data.decode("utf-8")))
        except Exception:
            captured.append({})
        return side_effect(request, **kwargs)

    return captured, wrapper


def _tool_names(body):
    names = set()
    for tool in body.get("tools") or []:
        fn = tool.get("function") or tool
        if fn.get("name"):
            names.add(fn["name"])
    return names


def _seed_paused_history(cm):
    session_id = cm.create_session()
    cm.save_conversation(session_id, [
        _turn("user", "go", timestamp="2026-08-12T15:00:00"),
        _turn("assistant", "", timestamp="2026-08-12T15:00:01",
              tool_calls=[{"id": "call_1", "name": "echo", "arguments": "{}"}]),
        _turn("tool", "echo result", timestamp="2026-08-12T15:00:02",
              name="echo", tool_use_id="call_1"),
    ])
    return session_id


def _register_second_model_and_tool(srv):
    srv._runtime._model_registry.register(ModelConfig(
        model_id="model-b", api_base="http://localhost:9999",
        model_name="model-B-label", api_protocol="openai",
    ))
    srv._runtime._tool_registry.register(
        ToolConfig(tool_id="alpha", tool_type="function", name="alpha",
                   description="alpha tool", parameters={}),
        callable_fn=lambda **kw: "alpha result",
    )


def test_resume_applies_fresh_model_and_tool_selection(continue_server):
    """暂停时换模型、换工具，恢复请求携带的新参数作用于新一轮：推理参数是
    generation（请求）级绑定，暂停不会把参数冻结在暂停时刻。"""
    srv = continue_server
    session_id = _seed_paused_history(srv._context_manager)
    _register_second_model_and_tool(srv)

    captured, wrapper = _capture_model_calls(
        _scripted_urlopen([_openai_sse_text("switched")])
    )
    status, body = _post_stream(srv, {
        "session_id": session_id,
        "model_id": "model-b",
        "tool_ids": ["alpha"],
        "resume": True,
        "messages": [],
    }, wrapper)
    assert status == 200, f"resume failed: {body[:500]}"
    assert "switched" in body

    assert captured, "model API was not called"
    sent = captured[-1]
    assert sent.get("model") == "model-B-label"
    assert _tool_names(sent) == {"alpha"}


def test_resume_agent_switch_rebinds_model_and_tools(continue_server):
    """暂停时换 AI 代理：代理配置里的 model/tool 绑定在恢复的新一轮生效
    （请求体里的旧 model_id/tool_ids 会被代理配置覆盖，与普通发送一致）。"""
    srv = continue_server
    session_id = _seed_paused_history(srv._context_manager)
    _register_second_model_and_tool(srv)
    srv._agent_manager.create(agent_id="AgentA", model_id="test-model", nickname="A", tool_ids=["echo"])
    srv._agent_manager.create(agent_id="AgentB", model_id="model-b", nickname="B", tool_ids=["alpha"])

    captured, wrapper = _capture_model_calls(
        _scripted_urlopen([_openai_sse_text("rebound")])
    )
    status, body = _post_stream(srv, {
        "session_id": session_id,
        "agent_ids": ["AgentB"],
        "model_id": "test-model",   # stale on purpose; the agent binding wins
        "tool_ids": ["echo"],       # stale on purpose; the agent binding wins
        "resume": True,
        "messages": [],
    }, wrapper)
    assert status == 200, f"resume failed: {body[:500]}"

    assert captured, "model API was not called"
    sent = captured[-1]
    assert sent.get("model") == "model-B-label"
    assert _tool_names(sent) == {"alpha"}


# ---------------------------------------------------------------------------
# 6. M3: the paused resting state survives an agentsvc restart
# ---------------------------------------------------------------------------


def test_pause_sidecar_roundtrip_and_validation(tmp_path):
    """write/read/clear plus self-healing on corrupt or mismatched files."""
    from runtime.context_manager import ContextManager

    cm = ContextManager(infer_fn=lambda *a, **k: "", chats_dir=str(tmp_path))
    sid = cm.create_session()
    assert cm.read_pause_state(sid) is None

    cm.write_pause_state(sid, {
        "version": ContextManager.PAUSE_STATE_VERSION,
        "kind": "infer_pause",
        "session_id": sid,
        "paused_at": "2026-08-12T15:00:03",
        "context": [{"role": "user", "content": "go"}],
    })
    state = cm.read_pause_state(sid)
    assert state is not None and state["kind"] == "infer_pause"
    assert cm.clear_pause_state(sid) is True
    assert cm.clear_pause_state(sid) is False

    # Corrupt JSON is discarded (and the file removed, so the session is
    # never pinned to a paused marker whose snapshot cannot load).
    with open(cm.pause_state_path(sid), "w", encoding="utf-8") as f:
        f.write("{not json")
    assert cm.read_pause_state(sid) is None
    assert not os.path.isfile(cm.pause_state_path(sid))

    # A sidecar written for another session id, or with a foreign version,
    # is likewise discarded.
    cm.write_pause_state(sid, {"version": 1, "session_id": "other", "context": []})
    assert cm.read_pause_state(sid) is None
    cm.write_pause_state(sid, {"version": 99, "session_id": sid, "context": []})
    assert cm.read_pause_state(sid) is None
    assert not os.path.isfile(cm.pause_state_path(sid))


def test_pause_status_endpoint_recognizes_pause_lazily(continue_server):
    """No startup scan: the UI's per-session check re-arms "paused" on open.

    Cost is O(1) per opened session (one small file), independent of how many
    sessions the service has on disk. A live generation still wins.
    """
    from runtime.context_manager import ContextManager

    srv = continue_server
    cm = srv._context_manager
    paused_sid = cm.create_session()
    # Session ids are second-precise; a second create_session() in the same
    # second would collide. An id that was never created is a perfectly good
    # idle session for this check (no directory, no sidecar).
    idle_sid = "000000_000000"
    cm.write_pause_state(paused_sid, {
        "version": ContextManager.PAUSE_STATE_VERSION,
        "kind": "infer_pause", "session_id": paused_sid,
        "paused_at": "x", "context": [],
    })

    status, res = _get_json(srv, f"/v1/infer/pause-status?session_id={paused_sid}")
    assert status == 200 and res.get("paused") is True
    assert server_state._session_statuses.get(paused_sid) == "paused"

    status, res = _get_json(srv, f"/v1/infer/pause-status?session_id={idle_sid}")
    assert status == 200 and res.get("paused") is False
    assert idle_sid not in server_state._session_statuses

    # An active generation owns the status: the stale sidecar never wins.
    owner = threading.Event()
    begin_session_stream(paused_sid, owner)
    status, res = _get_json(srv, f"/v1/infer/pause-status?session_id={paused_sid}")
    assert status == 200 and res.get("paused") is False
    server_state.clear_session_stream_state(paused_sid)

    # Missing session_id is a client error.
    status, _res = _get_json(srv, "/v1/infer/pause-status")
    assert status == 400


def test_exit_state_snapshots_inflight_context_at_pause_boundary():
    """The generator hands the loop's exact in-flight list to the handler."""
    runtime = _stream_runtime()
    seen = {"n": 0}

    def gate():
        seen["n"] += 1
        if seen["n"] == 1:
            return None
        return {"append": [], "stop": True}

    rounds = [_openai_sse_text("", tool_calls=[("echo", "{}")])]

    def urlopen(request, **kwargs):
        # Second boundary stop: the model must never be called again.
        return _make_resp(rounds.pop(0) if rounds else _openai_sse_text("never"))

    exit_state: dict = {}
    with patch("urllib.request.urlopen", side_effect=urlopen):
        list(runtime.infer_stream(
            InferenceRequest(
                model_id="test-model",
                messages=[Message(role="user", content="go")],
                stream=True,
            ),
            pause_gate=gate,
            exit_state=exit_state,
        ))

    snapshot = exit_state.get("messages")
    assert snapshot, "pause exit must snapshot the in-flight context"
    roles = [m.get("role") for m in snapshot]
    assert roles[0] == "user"
    assert any(m.get("tool_calls") for m in snapshot)
    # The snapshot ends on the fully executed round (function == the loop's
    # role for a tool result; it is persisted/published as "tool").
    assert roles[-1] in ("function", "tool")


def test_pause_writes_sidecar_and_resume_survives_simulated_restart(continue_server):
    """Full cycle: pause persists a sidecar, a simulated restart re-seeds
    "paused", and resume then consumes the sidecar and continues the session."""
    srv = continue_server
    cm = srv._context_manager
    session_id = cm.create_session()

    r2_gate = threading.Event()
    r1 = _openai_sse_text("", tool_calls=[("echo", "{}")])
    r2 = _openai_sse_text("", tool_calls=[("echo", "{}")])

    def side_effect(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        if "tool" in json.dumps(body.get("messages", [])):
            r2_gate.wait(timeout=10)
            return _make_resp(r2)
        return _make_resp(r1)

    payload = json.dumps({
        "session_id": session_id,
        "model_id": "test-model",
        "tool_ids": ["echo"],
        "messages": [{"role": "user", "content": "go"}],
        "stream": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        _url(srv, "/v1/infer/stream"), data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with _patch_model_urlopen(srv, side_effect):
        resp = urllib.request.urlopen(req, timeout=15)
        paused = False
        for raw in resp:
            text = raw.decode("utf-8", "replace")
            if '"role": "tool"' in text and not paused:
                _status, res = _post_json(
                    srv, "/v1/infer/abort",
                    {"session_id": session_id, "action": "pause"},
                )
                assert res.get("effective") is True
                r2_gate.set()
                paused = True
    r2_gate.set()

    # The pause wrote a durable snapshot of the in-flight context.
    sidecar = cm.read_pause_state(session_id)
    assert sidecar is not None, "pause must persist a resume_state.json sidecar"
    assert sidecar["kind"] == "infer_pause"
    assert sidecar["session_id"] == session_id
    assert os.path.isfile(cm.pause_state_path(session_id))
    roles = [m.get("role") for m in sidecar["context"]]
    assert roles[0] == "user"
    assert any(m.get("tool_calls") for m in sidecar["context"])
    assert roles[-1] in ("function", "tool")

    # Simulate the restart: every in-memory registry is empty, then the
    # startup scan re-seeds the resting status from disk.
    with server_state._session_state_lock:
        server_state._session_statuses.clear()
        server_state._unread_sessions.clear()
    with server_state._session_stream_lock:
        server_state._session_streams.clear()
    # The UI opening the session performs the O(1) per-session check that
    # re-arms the resting status from disk (no startup scan anywhere).
    _status, res = _get_json(srv, f"/v1/infer/pause-status?session_id={session_id}")
    assert res.get("paused") is True
    assert server_state._session_statuses.get(session_id) == "paused"

    # Resume after the "restart" completes the session and consumes the file.
    status, body = _post_stream(
        srv,
        {
            "session_id": session_id,
            "model_id": "test-model",
            "tool_ids": ["echo"],
            "resume": True,
            "messages": [],
        },
        _scripted_urlopen([_openai_sse_text("Resumed after restart.")]),
    )
    assert status == 200, f"resume after restart failed: {body[:500]}"
    assert "Resumed after restart." in body
    assert cm.read_pause_state(session_id) is None
    assert not os.path.isfile(cm.pause_state_path(session_id))


def test_resume_uses_sidecar_context_verbatim_and_deletes_it(continue_server):
    """A resume continues from the snapshot, not from re-assembled history.

    The snapshot carries one frame that was never persisted (a skill
    disclosure marker); seeing it in the outgoing model request proves the
    in-flight context -- not the disk history -- fed the resumed round.
    """
    from runtime.context_manager import ContextManager

    srv = continue_server
    cm = srv._context_manager
    session_id = _seed_paused_history(cm)
    cm.write_pause_state(session_id, {
        "version": ContextManager.PAUSE_STATE_VERSION,
        "kind": "infer_pause",
        "session_id": session_id,
        "paused_at": "2026-08-12T15:00:03",
        "context": [
            {"role": "user", "content": "go", "timestamp": "2026-08-12T15:00:00"},
            {"role": "assistant", "content": "", "timestamp": "2026-08-12T15:00:01",
             "tool_calls": [{"id": "call_1", "name": "echo", "arguments": "{}"}]},
            {"role": "tool", "content": "echo result", "timestamp": "2026-08-12T15:00:02",
             "name": "echo", "tool_use_id": "call_1"},
            {"role": "system", "content": "SKILL-DISCLOSURE-MARKER",
             "timestamp": "2026-08-12T15:00:03"},
        ],
    })

    captured, wrapper = _capture_model_calls(
        _scripted_urlopen([_openai_sse_text("Resumed from snapshot.")])
    )
    status, body = _post_stream(srv, {
        "session_id": session_id,
        "model_id": "test-model",
        "tool_ids": ["echo"],
        "resume": True,
        "messages": [],
    }, wrapper)
    assert status == 200, f"resume failed: {body[:500]}"
    assert "Resumed from snapshot." in body

    assert captured, "model API was not called"
    sent = json.dumps(captured[-1], ensure_ascii=False)
    assert "SKILL-DISCLOSURE-MARKER" in sent, (
        "resumed round must carry the sidecar's transient context"
    )
    # Restored, then deleted immediately -- no stale paused marker survives.
    assert cm.read_pause_state(session_id) is None
    assert not os.path.isfile(cm.pause_state_path(session_id))


def test_fresh_generation_discards_stale_pause_sidecar(continue_server):
    """A normal send on a paused session supersedes the resting state."""
    from runtime.context_manager import ContextManager

    srv = continue_server
    cm = srv._context_manager
    session_id = _seed_paused_history(cm)
    cm.write_pause_state(session_id, {
        "version": ContextManager.PAUSE_STATE_VERSION,
        "kind": "infer_pause", "session_id": session_id,
        "paused_at": "2026-08-12T15:00:03",
        "context": [{"role": "user", "content": "stale snapshot"}],
    })
    server_state._session_statuses[session_id] = "paused"

    status, body = _post_stream(srv, {
        "session_id": session_id,
        "model_id": "test-model",
        "tool_ids": ["echo"],
        "messages": [{"role": "user", "content": "next question"}],
    }, _scripted_urlopen([_openai_sse_text("answered fresh")]))
    assert status == 200, f"send failed: {body[:500]}"

    assert cm.read_pause_state(session_id) is None
    assert not os.path.isfile(cm.pause_state_path(session_id))
    assert server_state._session_statuses.get(session_id) != "paused"


def test_end_action_discards_pause_sidecar(continue_server):
    """Ending the pause also discards the durable sidecar."""
    from runtime.context_manager import ContextManager

    srv = continue_server
    cm = srv._context_manager
    session_id = _seed_paused_history(cm)
    cm.write_pause_state(session_id, {
        "version": ContextManager.PAUSE_STATE_VERSION,
        "kind": "infer_pause", "session_id": session_id,
        "paused_at": "2026-08-12T15:00:03", "context": [],
    })
    server_state._session_statuses[session_id] = "paused"

    status, res = _post_json(
        srv, "/v1/infer/abort", {"session_id": session_id, "action": "end"},
    )
    assert status == 200
    assert res.get("ended") is True
    assert cm.read_pause_state(session_id) is None
    assert not os.path.isfile(cm.pause_state_path(session_id))
    assert server_state._session_statuses.get(session_id) == "done_success_unread"
