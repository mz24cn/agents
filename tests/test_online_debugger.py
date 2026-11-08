"""online_debugger: the diagnosis loop runs on client side tool calls.

The debugger no longer opens a listening port or asks the agent to shell out to
``curl``.  It ships its on-site inspection endpoints as session-scoped
``client_side`` tools; the service ends the loop normally when the model calls
one, and the host process executes the call locally (reading the live traceback
frame locals) and resumes the same session with the result.

These tests drive that whole loop with a scripted model: the model asks for the
event snapshot and evaluates an expression in the target process, the debugger
answers locally, and the session completes with a diagnosis report.
"""

import contextlib
import http.server
import json
import pathlib
import sys
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "accessories"))

import online_debugger  # noqa: E402

from tests.test_client_side_tools import (  # noqa: E402
    _conversation,
    _fake_model_server,
    _running_server,
    _sse,
    _sse_content,
    _sse_tool_calls,
)


@pytest.fixture()
def dbg(monkeypatch):
    """A debugger with all process-wide state reset, never dispatching on its own."""
    online_debugger._STATE.update(
        {"started": False, "worker": False, "dispatch_ok": 0,
         "dispatch_fail": 0, "handoffs": 0})
    online_debugger._EVENTS.clear()
    online_debugger._FINGERPRINTS.clear()
    online_debugger._FRAME_CACHE.clear()
    online_debugger._CFG = dict(online_debugger._DEFAULTS)
    monkeypatch.setenv("DEBUGGER_AGENT_ID", "")     # 不用 agent，直接指定 model_id
    monkeypatch.setenv("DEBUGGER_MODEL_ID", "test-model")
    monkeypatch.delenv("DEBUGGER_STREAM", raising=False)
    monkeypatch.delenv("DEBUGGER_EVAL", raising=False)
    return online_debugger


def _raise_and_capture(step="下单"):
    """Produce a real traceback whose frame locals the debugger must read."""
    def place_order(user_id, items):
        price = 0
        order_id = "SO-42"
        return price / 0 if items else order_id  # ZeroDivisionError

    try:
        place_order("u-1", ["x"])
    except ZeroDivisionError as exc:
        return online_debugger.report(step, exc)


def _run_diagnosis(tmp_path, dbg, make_streams, capsys,
                   stream_mode=False, handoff_rounds=30):
    """Service + scripted model + one diagnosis round trip; returns the artifacts.

    The scripted rounds need the real ``eid``, which only exists once the
    exception has been captured, so the model script starts empty and is filled
    in after ``report()`` — the fake model serves whatever is there per call.
    """
    streams = []
    with _fake_model_server(streams) as (api_base, seen):
        with _running_server(tmp_path, api_base) as (srv, chats_dir):
            dbg.start(project="demo", agent_enabled=False,
                      agents_url=f"http://127.0.0.1:{srv.port}",
                      stream=stream_mode, handoff_rounds=handoff_rounds,
                      cooldown_file=str(tmp_path / "cooldown.json"))
            event = _raise_and_capture()
            streams.extend(make_streams(event))
            box = {}
            dbg._run_diagnosis(event, box)
            turns = _conversation(chats_dir, box["session_id"])["messages"]
            capsys.readouterr()
            return box, seen, turns, srv


def test_snapshot_and_client_tool_handlers_read_live_locals(dbg, tmp_path) -> None:
    dbg.start(project="demo", agent_enabled=False,
              cooldown_file=str(tmp_path / "cooldown.json"))
    event = _raise_and_capture()

    assert event and event["type"] == "ZeroDivisionError"
    snapshot = json.loads(dbg._execute_client_tool("debugger_event", {"eid": event["eid"]}))
    assert snapshot["step"] == "下单"
    frame = snapshot["frames"][-1]
    assert frame["func"] == "place_order"

    # debugger_frame(fid): locals as captured at the moment of the raise.
    detail = json.loads(dbg._execute_client_tool("debugger_frame", {"fid": frame["fid"]}))
    assert detail["locals"]["order_id"] == "str: 'SO-42'"

    # debugger_eval: evaluated in this process, in that frame's scope.
    evaluated = json.loads(dbg._execute_client_tool(
        "debugger_eval", {"expr": "user_id", "fid": frame["fid"]}))
    assert evaluated["ok"] is True and evaluated["result"] == "str: 'u-1'"

    # Status/threads answer too, and unknown ids answer normally (no crash).
    status = json.loads(dbg._execute_client_tool("debugger_status", {}))
    assert status["project"] == "demo" and status["events_stored"] == 1
    assert "threads" in json.loads(dbg._execute_client_tool("debugger_threads", {}))
    assert "error" in json.loads(dbg._execute_client_tool("debugger_event", {"eid": "nope"}))
    assert "error" in json.loads(dbg._execute_client_tool("no_such_tool", {}))


def test_eval_can_be_switched_off(dbg, tmp_path) -> None:
    dbg.start(project="demo", agent_enabled=False, eval_enabled=False,
              cooldown_file=str(tmp_path / "cooldown.json"))
    payload = json.loads(dbg._execute_client_tool("debugger_eval", {"expr": "1+1"}))
    assert payload["ok"] is False and "DEBUGGER_EVAL" in payload["error"]
    # ...and the tool is not even offered to the model.
    assert "debugger_eval" not in [t["name"] for t in dbg._client_tools_payload()]


def test_diagnosis_loop_runs_client_tools_locally(dbg, tmp_path, capsys) -> None:
    """One round declares two client calls; the next round reports the diagnosis."""
    def streams_for(event):
        return [
            _sse_tool_calls([
                ("debugger_event", "call_ev", json.dumps({"eid": event["eid"]})),
                ("debugger_eval", "call_eval", json.dumps({"expr": "len(_EVENTS)"})),
            ]),
            _sse_content("## 问题\n除零\n## 根因\nprice=0\n## 修改\n未修改\n"),
        ]

    box, seen, turns, srv = _run_diagnosis(tmp_path, dbg, streams_for, capsys)

    assert box["ok"] is True, box["text"]
    assert box["text"].startswith("## 问题")
    assert len(seen) == 2, "the loop must continue after the local results"

    tool_turns = [t for t in turns if t["role"] == "tool"]
    assert [t["name"] for t in tool_turns] == ["debugger_event", "debugger_eval"]
    assert "ZeroDivisionError" in tool_turns[0]["content"]
    # The evaluation ran in *this* process: it saw the debugger's own state.
    assert json.loads(tool_turns[1]["content"])["result"] == "int: 1"
    # Session-scoped: none of the debugger tools reached the service registry.
    for tool in dbg._CLIENT_TOOLS:
        assert srv._runtime._tool_registry.get(tool["name"]) is None
    assert online_debugger._STATE["handoffs"] == 1


def test_diagnosis_loop_over_streaming_endpoint(dbg, tmp_path, capsys) -> None:
    def streams_for(_event):
        return [
            _sse_tool_calls([
                ("debugger_status", "call_st", "{}"),
                ("debugger_threads", "call_th", "{}"),
            ]),
            _sse_content("## 问题\n卡死\n## 根因\n线程等待\n## 修改\n未修改\n"),
        ]

    box, seen, turns, _srv = _run_diagnosis(
        tmp_path, dbg, streams_for, capsys, stream_mode=True)

    assert box["ok"] is True, box["text"]
    assert len(seen) == 2
    tool_turns = [t for t in turns if t["role"] == "tool"]
    assert [t["name"] for t in tool_turns] == ["debugger_status", "debugger_threads"]
    assert json.loads(tool_turns[0]["content"])["project"] == "demo"
    assert "threads" in json.loads(tool_turns[1]["content"])


def test_handoff_rounds_are_bounded(dbg, tmp_path, capsys) -> None:
    """A model that keeps calling client tools must not ping-pong forever."""
    looping = _sse_tool_calls([("debugger_status", "call_loop", "{}")])

    def streams_for(_event):
        return [looping] * 6

    box, seen, _turns, _srv = _run_diagnosis(
        tmp_path, dbg, streams_for, capsys, handoff_rounds=2)

    assert box["ok"] is False
    assert "交回轮次超过 2 次" in box["text"]
    assert len(seen) == 3, "the initial request plus the 2 allowed handoffs"



# ---------------------------------------------------------------------------
# The handoff list must be reported-union-unanswered, not reported-only
# ---------------------------------------------------------------------------

class _RawSSEHandler(http.server.BaseHTTPRequestHandler):
    """Replays one fixed SSE body as *the agent service* would have sent it."""

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.hits.append(dict(self.headers))
        body = self.server.body
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return


@contextlib.contextmanager
def _raw_sse_server(body: bytes):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RawSSEHandler)
    srv.body = body
    srv.hits = []
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _dbg_sse(frames) -> bytes:
    lines = [f"data: {json.dumps(f)}\n\n" for f in frames]
    return ("".join(lines) + "data: [DONE]\n\n").encode("utf-8")


def _post(dbg, frames):
    with _raw_sse_server(_dbg_sse(frames)) as srv2:
        dbg._CFG["agents_url"] = f"http://127.0.0.1:{srv2.server_address[1]}"
        return dbg._post_stream("/v1/infer/stream", {"messages": []})


def test_stream_handoff_unions_status_frame_with_unanswered_calls(dbg) -> None:
    """A parallel batch whose status frame names only part must not drop the rest.

    ``reported or [fallback]`` used to discard the fallback the moment the
    service named *anything*, so the calls the frame did not name were never
    executed: the session shows a client-tool call stuck pending mid-conversation
    while inference keeps running.  The handoff list is now reported ∪
    announced-but-unanswered, de-duplicated by id.
    """
    dbg.start(project="demo", agent_enabled=False,
              cooldown_file="/tmp/unused-cooldown.json")
    frames = [
        {"session_id": "s1", "role": "assistant", "tool_calls": [
            {"id": "a", "name": "debugger_eval", "arguments": '{"expr": "1"}'},
            {"id": "b", "name": "debugger_eval", "arguments": '{"expr": "2"}'},
        ]},
        # the service names only one of the two declared calls
        {"role": "status", "status": "client_tool_calls", "client_tool_calls": [
            {"id": "a", "name": "debugger_eval", "arguments": '{"expr": "1"}'},
        ]},
        {"role": "assistant", "content": "ok"},
    ]
    status, envelope = _post(dbg, frames)
    assert status == 200
    assert {c["id"] for c in envelope["client_tool_calls"]} == {"a", "b"}


def test_stream_handoff_dedups_by_id_and_keeps_arguments(dbg) -> None:
    dbg.start(project="demo", agent_enabled=False,
              cooldown_file="/tmp/unused-cooldown.json")
    frames = [
        {"session_id": "s1", "role": "assistant", "tool_calls": [
            {"id": "a", "name": "debugger_eval", "arguments": '{"expr": "2+2"}'},
        ]},
        # same id again, arguments-less: the richer copy must win
        {"role": "status", "status": "client_tool_calls", "client_tool_calls": [
            {"id": "a", "name": "debugger_eval", "arguments": ""},
        ]},
        {"role": "assistant", "content": "ok"},
    ]
    _status, envelope = _post(dbg, frames)
    calls = envelope["client_tool_calls"]
    assert len(calls) == 1 and calls[0]["arguments"] == '{"expr": "2+2"}'


def test_stream_handoff_skips_calls_already_answered(dbg) -> None:
    dbg.start(project="demo", agent_enabled=False,
              cooldown_file="/tmp/unused-cooldown.json")
    frames = [
        {"session_id": "s1", "role": "assistant", "tool_calls": [
            {"id": "a", "name": "debugger_status", "arguments": "{}"},
            {"id": "b", "name": "debugger_threads", "arguments": "{}"},
        ]},
        {"role": "tool", "tool_use_id": "b", "content": "{}"},
        {"role": "assistant", "content": "ok"},
    ]
    _status, envelope = _post(dbg, frames)
    assert [c["id"] for c in envelope["client_tool_calls"]] == ["a"]


# ---------------------------------------------------------------------------
# Contract details that the packaged device build taught us
# ---------------------------------------------------------------------------

def test_fingerprint_carries_line_numbers(dbg) -> None:
    """Same call chain, different raise site -> different fingerprint.

    With only ``func@file`` the five breakpoints of one chain collapsed into a
    single fingerprint, so the later (genuinely new) variant only bumped a
    counter: no jsonl line, no dispatch.
    """
    same_a = [{"func": "run", "file": "/x/a.py", "line": 10}]
    same_b = [{"func": "run", "file": "/x/a.py", "line": 42}]
    assert dbg._make_fingerprint("ValueError", same_a) == \
        dbg._make_fingerprint("ValueError", list(same_a))
    assert dbg._make_fingerprint("ValueError", same_a) != \
        dbg._make_fingerprint("ValueError", same_b)


def test_api_key_rides_only_in_the_request_header(dbg) -> None:
    dbg.start(project="demo", agent_enabled=False, api_key="sekret",
              cooldown_file="/tmp/unused-cooldown.json")
    headers = dbg._headers()
    assert headers["Authorization"] == "Bearer sekret"
    status = json.loads(dbg._execute_client_tool("debugger_status", {}))
    assert status["auth"] == "Bearer"
    assert "sekret" not in json.dumps(status)   # this blob is fed to the model
    dbg._CFG["api_key"] = ""
    assert "Authorization" not in dbg._headers()


def test_explicit_workspace_overrides_the_derived_one(dbg) -> None:
    dbg.start(project="demo", agent_enabled=False, workspace="/srv/app",
              cooldown_file="/tmp/unused-cooldown.json")
    event = _raise_and_capture()
    assert event["workspace"] == "/srv/app"


def test_handoff_budget_is_the_clients_own_limit(dbg) -> None:
    """The service caps *server-side* rounds per request; the caller owns handoffs.

    A round that only declares client tools ends the loop before the round
    counter, and every continuation request restarts that counter -- so a small
    handoff_rounds silently truncates long field-debugging sessions (observed on
    device at exactly 30).
    """
    assert online_debugger._DEFAULTS["handoff_rounds"] >= 80


# ---------------------------------------------------------------------------
# The verification step is now part of the contract (L4)
# ---------------------------------------------------------------------------

def test_diagnosis_prompt_requires_red_then_green_evidence(dbg) -> None:
    dbg.start(project="demo", agent_enabled=False,
              cooldown_file="/tmp/unused-cooldown.json")
    event = _raise_and_capture()
    prompt = dbg._build_prompt(event)
    assert "红→绿" in prompt and "改动前" in prompt
    assert "未验证：" in prompt                      # explicit "not verified" marker
    assert "{workspace}" not in prompt              # slots must resolve
    assert "reload" in prompt                       # no fake "it took effect"

    dbg._CFG["verify_fix"] = False
    off = dbg._build_prompt(event)
    assert "未执行验证" in off and "红→绿" not in off


# ---------------------------------------------------------------------------
# One portable file: no host-specific special cases, no non-stdlib imports
# ---------------------------------------------------------------------------

def test_the_client_imports_nothing_outside_the_standard_library() -> None:
    """The Android logcat mirror (tag ``HH_DEBUGGER``) used to live in ``_print``.

    A host whose stdout never reaches a log is a property of *that host*, not of
    this client.  Carrying that branch here means a portable single file grows a
    non-stdlib import plus a per-host special case; the host's integration layer
    should own its own log instead.
    """
    import ast

    src = pathlib.Path(online_debugger.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert "android" not in imported
    assert sorted(imported - set(sys.stdlib_module_names)) == []


def test_debugger_own_voice_has_exactly_one_outlet(capsys) -> None:
    online_debugger._print("交回中止: 撞 handoff 上限")
    assert capsys.readouterr().out.strip() == "[debugger] 交回中止: 撞 handoff 上限"
