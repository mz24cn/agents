"""Client-side tool calls: registration, loop handoff, and session resume.

A client-side tool (``tool_type="client_side"``) is a function tool stripped of
its executable entity: the agent service keeps only the registration JSON, hands
the definition to the model, and never runs the tool itself.  When the model
calls one, the inference loop ends *normally* and the call is handed back; the
caller executes it locally and resumes the same session with the result.

These tests pin the whole contract:

1. ``ToolConfig`` accepts the new type and both shapes of a session-provided
   definition (ToolConfig-shaped and OpenAI function schema).
2. A call to a client-side tool stops the loop with no fabricated tool result.
3. Parallel tool calls that straddle both execution sides are partitioned: the
   server-side call still runs, the client-side one is handed back.
4. ``client_tools`` shipped with a request are visible to the model for that
   session only — never in the ToolRegistry.
5. The non-streaming envelope names the pending calls (``client_tool_calls``),
   and a follow-up request carrying the results merges them into the session by
   session id and continues the loop.
6. One call has exactly one result: whichever client ran the tool and answered
   first is the one the session continues with, and a second answer for the same
   call is refused instead of starting a competing turn -- including the case
   where two clients of a session both have the tool and both post an answer
   for the same handoff batch, and the case where a second device joins after
   the handoff and is the one that finally answers.  Nothing about a handoff is
   claimable up front: every client that has the tool is expected to show the
   question, and the first answer back decides.

The model is a real local SSE endpoint rather than a monkeypatched
``urlopen``, so the test's own HTTP calls to the agent service stay real.
"""

import contextlib
import http.server
import io
import json
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch

from runtime.models import (
    CLIENT_SIDE_TOOL_TYPE,
    InferenceRequest,
    ModelConfig,
    ToolConfig,
)
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer
from runtime.server_state import (
    answered_client_tool_call_ids,
    pending_client_tool_calls,
)


# ---------------------------------------------------------------------------
# Fake model transport
# ---------------------------------------------------------------------------

class _FakeModelHandler(http.server.BaseHTTPRequestHandler):
    """Replays scripted OpenAI SSE bodies and records every request payload."""

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            self.server.seen.append(json.loads(raw.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            self.server.seen.append({})
        index = self.server.index
        self.server.index += 1
        if not self.server.streams:
            body = b"data: [DONE]\n\n"
        else:
            body = self.server.streams[min(index, len(self.server.streams) - 1)].getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return


@contextlib.contextmanager
def _fake_model_server(streams: list[io.BytesIO]):
    """Serve *streams*, one per model call; yield ``(base_url, seen)``."""
    seen: list[dict] = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeModelHandler)
    httpd.seen = seen
    httpd.streams = streams
    httpd.index = 0
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", seen
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _make_model_registry(api_base: str = "http://127.0.0.1:9999") -> ModelRegistry:
    registry = ModelRegistry()
    registry.register(ModelConfig(
        model_id="test-model",
        api_base=api_base,
        model_name="test",
        api_protocol="openai",
    ))
    return registry


def _sse(lines: list[str]) -> io.BytesIO:
    return io.BytesIO("".join(lines).encode("utf-8"))


def _sse_tool_calls(calls: list[tuple[str, str, str]]) -> io.BytesIO:
    """One OpenAI SSE stream declaring several parallel tool calls."""
    lines = []
    for index, (name, call_id, arguments) in enumerate(calls):
        lines.append("data: " + json.dumps({
            "choices": [{
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": index,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }],
                }
            }]
        }) + "\n\n")
    lines.append("data: [DONE]\n\n")
    return _sse(lines)


def _sse_content(text: str) -> io.BytesIO:
    return _sse([
        "data: " + json.dumps({
            "choices": [{"delta": {"role": "assistant", "content": text}}]
        }) + "\n\n",
        "data: [DONE]\n\n",
    ])


def _server_tool() -> str:
    return "server-side result"


def _make_runtime(api_base: str, extra_tools: list[ToolConfig] | None = None) -> Runtime:
    tool_registry = ToolRegistry()
    tool_registry.register(
        ToolConfig(
            tool_id="function-server_echo",
            tool_type="function",
            name="server_echo",
            description="A server-side tool that runs here",
            parameters={"type": "object", "properties": {}},
        ),
        callable_fn=_server_tool,
    )
    tool_registry.register(ToolConfig(
        tool_id=f"{CLIENT_SIDE_TOOL_TYPE}-probe",
        tool_type=CLIENT_SIDE_TOOL_TYPE,
        name="probe",
        description="Executed by the caller",
        parameters={"type": "object", "properties": {"target": {"type": "string"}}},
    ))
    for config in extra_tools or []:
        tool_registry.register(config)
    return Runtime(
        model_registry=_make_model_registry(api_base),
        tool_registry=tool_registry,
    )


# ---------------------------------------------------------------------------
# 1. ToolConfig / session definition parsing
# ---------------------------------------------------------------------------

def test_client_side_tool_config_is_marked_client_side() -> None:
    config = ToolConfig(
        tool_id="client-x", tool_type=CLIENT_SIDE_TOOL_TYPE, name="x",
        description="d", parameters={"type": "object", "properties": {}},
    )
    assert config.is_client_side() is True
    function_tool = ToolConfig(
        tool_id="function-x", tool_type="function", name="x",
        description="d", parameters={},
    )
    assert function_tool.is_client_side() is False
    # Round-trips through the registry JSON unchanged.
    assert ToolConfig.from_dict(config.to_dict()).is_client_side()


def test_session_definition_accepts_both_shapes() -> None:
    plain = ToolConfig.from_session_definition({
        "name": "read_local_file",
        "description": "Read a file on the caller machine",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    })
    assert plain.tool_type == CLIENT_SIDE_TOOL_TYPE
    assert plain.tool_id == "read_local_file"
    assert plain.parameters["properties"]["path"]["type"] == "string"

    openai_shape = ToolConfig.from_session_definition({
        "type": "function",
        "function": {
            "name": "local_exec",
            "description": "Run a command locally",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
        },
    })
    assert openai_shape.name == "local_exec"
    assert openai_shape.tool_type == CLIENT_SIDE_TOOL_TYPE

    # A caller cannot smuggle an executable type in: the service never runs it.
    sneaky = ToolConfig.from_session_definition({
        "name": "sneaky", "tool_type": "function",
        "function_file_path": "/etc/evil.py", "function_name": "boom",
    })
    assert sneaky.tool_type == CLIENT_SIDE_TOOL_TYPE
    assert sneaky.function_file_path is None

    try:
        ToolConfig.from_session_definition({"description": "nameless"})
    except ValueError as exc:
        assert "name" in str(exc)
    else:
        raise AssertionError("a client tool without a name must be rejected")


# ---------------------------------------------------------------------------
# 2. A client-side call ends the loop normally (streaming)
# ---------------------------------------------------------------------------

def test_client_side_call_ends_loop_without_fabricated_result() -> None:
    streams = [_sse_tool_calls([("probe", "call_c1", '{"target": "app"}')])]
    with _fake_model_server(streams) as (api_base, seen):
        runtime = _make_runtime(api_base)
        request = InferenceRequest(
            model_id="test-model",
            tool_ids=[f"{CLIENT_SIDE_TOOL_TYPE}-probe"],
            text="probe the target",
            stream=True,
        )
        collected = list(runtime.infer_stream(request))

    # The loop asked the model exactly once: a client-side call is a normal
    # exit, not a round to keep looping on.
    assert len(seen) == 1
    assert [m.role for m in collected if m.role == "tool"] == []
    declared = [
        call for m in collected if m.role == "assistant" and m.tool_calls
        for call in m.tool_calls
    ]
    assert [c["name"] for c in declared] == ["probe"]
    # The definition reached the model like any other tool.
    sent_tools = [t["function"]["name"] for t in seen[0].get("tools", [])]
    assert "probe" in sent_tools


def test_mixed_parallel_round_runs_server_tool_and_hands_over_the_rest() -> None:
    """One model round declaring both sides: server runs, client is handed back."""
    streams = [
        _sse_tool_calls([
            ("server_echo", "call_s1", "{}"),
            ("probe", "call_c1", '{"target": "app"}'),
        ]),
        _sse_content("should not be reached"),
    ]
    with _fake_model_server(streams) as (api_base, seen):
        runtime = _make_runtime(api_base)
        request = InferenceRequest(
            model_id="test-model",
            tool_ids=["function-server_echo", f"{CLIENT_SIDE_TOOL_TYPE}-probe"],
            text="both at once",
            stream=True,
        )
        collected = list(runtime.infer_stream(request))

    assert len(seen) == 1, "the loop must not continue after a client-side call"
    tool_msgs = [m for m in collected if m.role == "tool"]
    assert [m.name for m in tool_msgs] == ["server_echo"], (
        "only the server-side half of the round runs here"
    )
    assert tool_msgs[0].content == "server-side result"
    # The client-side call stays declared but unanswered.
    answered = {m.tool_use_id for m in tool_msgs}
    declared = [
        call for m in collected if m.role == "assistant" and m.tool_calls
        for call in m.tool_calls
    ]
    assert {c["id"] for c in declared} == {"call_s1", "call_c1"}
    assert "call_c1" not in answered

    pending = pending_client_tool_calls(
        [{"role": "assistant", "tool_calls": declared},
         {"role": "tool", "tool_use_id": "call_s1"}],
        {"probe"},
    )
    assert pending == [{
        "id": "call_c1", "name": "probe", "arguments": '{"target": "app"}',
    }]


# ---------------------------------------------------------------------------
# 3. Session-provided client tools never enter the registry
# ---------------------------------------------------------------------------

def test_session_client_tools_are_invisible_to_the_registry() -> None:
    streams = [_sse_tool_calls([("ad_hoc_tool", "call_a1", "{}")])]
    with _fake_model_server(streams) as (api_base, seen):
        runtime = _make_runtime(api_base)
        request = InferenceRequest(
            model_id="test-model",
            tool_ids=[],
            client_tools=[ToolConfig.from_session_definition({
                "name": "ad_hoc_tool",
                "description": "Defined for this session only",
                "parameters": {"type": "object", "properties": {}},
            })],
            text="use the ad-hoc tool",
            stream=True,
        )
        collected = list(runtime.infer_stream(request))

    sent_tools = [t["function"]["name"] for t in seen[0].get("tools", [])]
    assert sent_tools == ["ad_hoc_tool"], "the model must see the session tool"
    assert runtime._tool_registry.get("ad_hoc_tool") is None, (
        "a session-provided tool must not leak into the ToolRegistry"
    )
    assert [m.role for m in collected if m.role == "tool"] == []


def test_call_tool_refuses_a_client_side_tool() -> None:
    runtime = _make_runtime("http://127.0.0.1:9999")
    result = runtime.call_tool(f"{CLIENT_SIDE_TOOL_TYPE}-probe", {})
    assert "client-side tool" in result


# ---------------------------------------------------------------------------
# 4. End to end over HTTP: handoff, envelope, resume by session id
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _running_server(tmp_path, api_base: str):
    chats_dir = tmp_path / "chats"
    with contextlib.ExitStack() as stack:
        for target, value in (
            ("runtime.server._MODELS_PATH", "models.json"),
            ("runtime.server._TOOLS_PATH", "tools.json"),
            ("runtime.server._PROMPT_TEMPLATES_PATH", "prompt_templates.json"),
            ("runtime.server._DATA_DIR", "."),
        ):
            stack.enter_context(patch(target, str(tmp_path / value)))
        srv = RuntimeHTTPServer(
            _make_runtime(api_base), chats_dir=str(chats_dir),
        )
        srv.start_background(host="127.0.0.1", port=0)
        try:
            yield srv, str(chats_dir)
        finally:
            srv.stop()


def _post(base: str, path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": raw}


def _conversation(chats_dir: str, session_id: str) -> dict:
    with open(f"{chats_dir}/{session_id}/conversation.json", encoding="utf-8") as fh:
        return json.load(fh)


def test_non_streaming_handoff_and_resume(tmp_path) -> None:
    """Both execution sides in one session, over the non-streaming endpoint.

    Round 1: the model declares a server-side and a client-side call in the same
    message.  The server runs its own, then stops and reports the pending call.
    Round 2: the caller posts the local result with the same session id; the
    service merges it into the session history and the loop continues.
    """
    streams = [
        _sse_tool_calls([
            ("server_echo", "call_s1", "{}"),
            ("frame_probe", "call_c1", '{"fid": "f1"}'),
        ]),
        _sse_content("diagnosis: null pointer in frame f1"),
    ]
    with _fake_model_server(streams) as (api_base, seen):
        with _running_server(tmp_path, api_base) as (srv, chats_dir):
            base = f"http://127.0.0.1:{srv.port}"

            status, envelope = _post(base, "/v1/infer", {
                "model_id": "test-model",
                "tool_ids": ["function-server_echo"],
                "workspace": str(tmp_path),
                "session_id": "new",
                "messages": [{"role": "user", "content": "inspect frame f1"}],
                "client_tools": [{
                    "name": "frame_probe",
                    "description": "Dump a frame from the caller's process",
                    "parameters": {
                        "type": "object",
                        "properties": {"fid": {"type": "string"}},
                    },
                }],
            })

            assert status == 200, envelope
            session_id = envelope["session_id"]

            # The envelope names exactly the call the caller has to run locally.
            assert envelope.get("client_tool_calls") == [{
                "id": "call_c1", "name": "frame_probe",
                "arguments": '{"fid": "f1"}',
            }], envelope

            turns = _conversation(chats_dir, session_id)["messages"]
            # The server-side half ran; the client-side call was left unanswered.
            assert [t["role"] for t in turns] == ["user", "assistant", "tool"]
            assert [t.get("name") for t in turns if t["role"] == "tool"] == ["server_echo"]
            assistant_calls = turns[1]["tool_calls"]
            assert [c["name"] for c in assistant_calls] == ["server_echo", "frame_probe"]

            # Session-scoped: the ad-hoc tool is nowhere in the server registry.
            assert srv._runtime._tool_registry.get("frame_probe") is None
            tools_status, tools_body = _get_json(base, "/v1/tools")
            assert tools_status == 200
            assert "frame_probe" not in json.dumps(tools_body)

            # The definitions were recorded on the session itself, so a later
            # request of this session keeps showing them to the model.
            assert _conversation(chats_dir, session_id)["meta"]["client_tools"][0]["name"] == "frame_probe"

            # ---- resume with the locally executed result --------------------
            status, resumed = _post(base, "/v1/infer", {
                "model_id": "test-model",
                "tool_ids": ["function-server_echo"],
                "workspace": str(tmp_path),
                "session_id": session_id,
                "messages": [{
                    "role": "tool",
                    "name": "frame_probe",
                    "tool_use_id": "call_c1",
                    "content": "locals: x=None",
                }],
            })

            assert status == 200, resumed
            assert "client_tool_calls" not in resumed
            assert any(
                "null pointer" in (m.get("content") or "")
                for m in resumed["messages"] if m["role"] == "assistant"
            ), resumed

            # The second model request carried the session history *and* the
            # local tool result, in protocol order.
            second_request = seen[-1]
            roles = [m["role"] for m in second_request["messages"]]
            assert roles[-1] == "tool", roles
            tool_result = second_request["messages"][-1]
            assert tool_result["tool_call_id"] == "call_c1"
            assert tool_result["content"] == "locals: x=None"
            # client_tools was omitted by the caller and restored from the session.
            sent_tools = [t["function"]["name"] for t in second_request.get("tools", [])]
            assert "frame_probe" in sent_tools

            turns = _conversation(chats_dir, session_id)["messages"]
            assert [t.get("name") for t in turns if t["role"] == "tool"] == [
                "server_echo", "frame_probe",
            ]


def test_an_empty_declaration_takes_the_tool_back_from_the_session(tmp_path) -> None:
    """``client_tools: []`` is the caller revoking the tools, not saying nothing.

    The web client used to declare AskUser on every request, so a model could
    open a question dialog in a conversation where the user had never enabled
    the tool.  Now the picker decides, and un-ticking has to survive the session:
    a request that declares ``[]`` must clear the list the session remembered,
    so even a later request that omits the field does not hand the tool back.
    """
    probe = {
        "name": "frame_probe",
        "description": "Ask the caller a question",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
    }
    streams = [
        _sse_tool_calls([("frame_probe", "call_c1", '{"q": "which?"}')]),
        _sse_content("no question then"),
        _sse_content("still no question"),
    ]
    with _fake_model_server(streams) as (api_base, seen):
        with _running_server(tmp_path, api_base) as (srv, chats_dir):
            base = f"http://127.0.0.1:{srv.port}"

            # 1. enabled: the call is handed back, and the session remembers it.
            status, first = _post(base, "/v1/infer", {
                "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                "session_id": "new", "messages": [{"role": "user", "content": "pick a plan"}],
                "client_tools": [probe],
            })
            assert status == 200, first
            session_id = first["session_id"]
            assert [c["name"] for c in first.get("client_tool_calls", [])] == ["frame_probe"]
            assert "frame_probe" in [t["function"]["name"] for t in seen[-1].get("tools", [])]

            # 2. revoked: the model is not even offered the tool.
            status, second = _post(base, "/v1/infer", {
                "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                "session_id": session_id,
                "messages": [{"role": "user", "content": "actually, decide yourself"}],
                "client_tools": [],
            })
            assert status == 200, second
            assert not second.get("client_tool_calls"), second
            assert "frame_probe" not in [t["function"]["name"] for t in seen[-1].get("tools", [])]
            assert _conversation(chats_dir, session_id)["meta"].get("client_tools") == []

            # 3. and it stays revoked when the field is left out entirely: the
            #    session no longer remembers a list to restore.
            status, third = _post(base, "/v1/infer", {
                "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                "session_id": session_id,
                "messages": [{"role": "user", "content": "one more round"}],
            })
            assert status == 200, third
            assert not third.get("client_tool_calls"), third
            assert "frame_probe" not in [t["function"]["name"] for t in seen[-1].get("tools", [])]


def _get_json(base: str, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(base + path, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def test_streaming_handoff_is_observable_by_tool_name(tmp_path) -> None:
    """A streaming client detects the handoff from the streamed tool call.

    The assistant frame declares the client-side call and no tool result frame
    for it ever follows, which is the signal the caller acts on.
    """
    streams = [_sse_tool_calls([("probe", "call_c9", "{}")])]
    with _fake_model_server(streams) as (api_base, _seen):
        with _running_server(tmp_path, api_base) as (srv, _chats_dir):
            base = f"http://127.0.0.1:{srv.port}"
            body = json.dumps({
                "model_id": "test-model",
                "tool_ids": [f"{CLIENT_SIDE_TOOL_TYPE}-probe"],
                "workspace": str(tmp_path),
                "session_id": "new",
                "messages": [{"role": "user", "content": "probe it"}],
            }).encode("utf-8")
            req = urllib.request.Request(
                base + "/v1/infer/stream", data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")

    frames = [
        json.loads(line[6:]) for line in raw.splitlines()
        if line.startswith("data: ") and line[6:] != "[DONE]"
    ]
    declared = [
        call for f in frames
        if f.get("role") == "assistant" and f.get("tool_calls")
        for call in f["tool_calls"]
    ]
    assert [c.get("name") for c in declared if c.get("name")] == ["probe"]
    assert not [f for f in frames if f.get("role") == "tool"], (
        "the service must not answer a client-side call itself"
    )
    # The stream also says so explicitly, just before [DONE].
    handoff_frames = [
        f for f in frames
        if f.get("role") == "status" and f.get("status") == "client_tool_calls"
    ]
    assert len(handoff_frames) == 1, frames
    assert handoff_frames[0]["client_tool_calls"] == [{
        "id": declared[0]["id"], "name": "probe", "arguments": "{}",
    }]


def test_server_only_rounds_still_loop_as_before() -> None:
    """Sanity: the client-side exit did not disturb the existing loop behaviour."""
    streams = [
        _sse_tool_calls([("server_echo", "call_s9", "{}")]),
        _sse_content("done"),
    ]
    with _fake_model_server(streams) as (api_base, seen):
        runtime = _make_runtime(api_base)
        request = InferenceRequest(
            model_id="test-model",
            tool_ids=["function-server_echo"],
            text="server only",
            stream=True,
        )
        collected = list(runtime.infer_stream(request))

    assert len(seen) == 2
    assert [m.name for m in collected if m.role == "tool"] == ["server_echo"]
    assert any(m.content == "done" for m in collected if m.role == "assistant")

# ---------------------------------------------------------------------------
# What max_tool_rounds actually budgets (matters for handoff-driven callers)
# ---------------------------------------------------------------------------

def test_pure_client_side_round_does_not_consume_max_tool_rounds() -> None:
    """``max_tool_rounds`` counts server-side rounds of *one* request.

    A round that only declares client-side calls leaves nothing for the server
    to execute, so the loop breaks *before* the round counter is checked, and a
    continuation request starts from 0 again.  A caller that drives a session
    through client tools (the online debugger does) is therefore bounded by its
    own handoff limit, not by the service's per-request cap.
    """
    streams = [_sse_tool_calls([("probe", "call_c1", '{"target": "app"}')])]
    with _fake_model_server(streams) as (api_base, _seen):
        runtime = _make_runtime(api_base)
        request = InferenceRequest(
            model_id="test-model",
            tool_ids=[f"{CLIENT_SIDE_TOOL_TYPE}-probe"],
            text="probe the target",
            stream=True,
            max_tool_rounds=1,          # one *server-side* round allowed
        )
        collected = list(runtime.infer_stream(request))

    text = "".join(m.content or "" for m in collected if m.role == "assistant")
    assert "maximum tool-call rounds" not in text, text
    declared = [
        call for m in collected if m.role == "assistant" and m.tool_calls
        for call in m.tool_calls
    ]
    assert [c["name"] for c in declared] == ["probe"]


def test_server_side_round_does_consume_max_tool_rounds() -> None:
    """Contrast: the same cap does bite as soon as a server-side round runs."""
    streams = [
        _sse_tool_calls([("server_echo", "call_s1", "{}")]),
        _sse_tool_calls([("server_echo", "call_s2", "{}")]),
    ]
    with _fake_model_server(streams) as (api_base, seen):
        runtime = _make_runtime(api_base)
        request = InferenceRequest(
            model_id="test-model",
            tool_ids=["function-server_echo"],
            text="echo twice",
            stream=True,
            max_tool_rounds=1,
        )
        collected = list(runtime.infer_stream(request))

    text = "".join(m.content or "" for m in collected if m.role == "assistant")
    assert "maximum tool-call rounds (1) reached" in text, text
    assert len(seen) == 2


# ---------------------------------------------------------------------------
# 5. What the session's *other* clients see when one client runs a call
# ---------------------------------------------------------------------------

def _session_stream_frames(base: str, session_id: str, after: int = 0,
                           timeout: float = 30.0) -> list[dict]:
    """Frames of the session's last inference generation, as a bystander reads them.

    ``GET /v1/sessions/{id}/stream`` is what any client that did not POST the
    inference consumes: the generation's retained frames replayed from *after*,
    then the live ones.  Reading it to ``[DONE]`` therefore shows exactly what
    such a bystander ends up applying.
    """
    frames: list[dict] = []
    req = urllib.request.Request(
        f"{base}/v1/sessions/{session_id}/stream?after={after}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for line in resp:
            line = line.decode("utf-8", "replace").strip()
            if line == "data: [DONE]":
                break
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            frames.append(json.loads(line[6:]))
    return frames


def test_a_client_side_result_reaches_the_other_clients(tmp_path) -> None:
    """A call one client ran must stop showing as running everywhere else.

    The web page is often only a bystander of a session: the online debugger
    script owns it, executes the handed-back ``client_side`` call and answers
    with a ``role=tool`` message on a follow-up request.  The service neither
    ran that tool nor emits a result frame of its own, so unless the submitted
    result is brokered every other client keeps rendering that call as still
    running (⏳) -- until the page is reloaded and conversation.json fills the
    gap in.
    """
    probe = {
        "name": "frame_probe",
        "description": "Dump a frame from the caller's process",
        "parameters": {"type": "object", "properties": {"fid": {"type": "string"}}},
    }
    streams = [
        _sse_tool_calls([("frame_probe", "call_c1", '{"fid": "f1"}')]),
        _sse_content("diagnosis: null pointer in frame f1"),
    ]
    with _fake_model_server(streams) as (api_base, _seen):
        with _running_server(tmp_path, api_base) as (srv, chats_dir):
            base = f"http://127.0.0.1:{srv.port}"

            def _post_stream(payload: dict) -> tuple[int, list[dict]]:
                """POST /v1/infer/stream and return the frames ITS caller sees."""
                req = urllib.request.Request(
                    base + "/v1/infer/stream",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                return 200, [
                    json.loads(line[6:]) for line in raw.splitlines()
                    if line.startswith("data: ") and line[6:] != "[DONE]"
                ]

            status, first_frames = _post_stream({
                "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                "session_id": "new",
                "messages": [{"role": "user", "content": "inspect frame f1"}],
                "client_tools": [probe],
            })
            assert status == 200
            session_id = next(
                f["session_id"] for f in first_frames if f.get("session_id")
            )
            assert any(
                f.get("status") == "client_tool_calls" for f in first_frames
            ), first_frames

            # The caller executed the call locally and answers on this session.
            _status, second_frames = _post_stream({
                "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                "session_id": session_id,
                "messages": [{
                    "role": "tool", "name": "frame_probe",
                    "tool_use_id": "call_c1", "content": "locals: x=None",
                }],
            })

            # A bystander of the same session sees the answer, and sees it
            # before the assistant turn that reasons about it -- the same order
            # conversation.json keeps, and the same one the web UI needs to
            # attach the result to the call that declared it.
            bystander = _session_stream_frames(base, session_id)
            results = [f for f in bystander if f.get("role") == "tool"]
            assert [(f.get("tool_use_id"), f.get("content")) for f in results] == [
                ("call_c1", "locals: x=None"),
            ], bystander
            protocol_order = [
                f.get("role") for f in bystander if f.get("role") in ("tool", "assistant")
            ]
            assert protocol_order[0] == "tool", protocol_order

            # The submitting client is not handed its own answer back: like a
            # submitted user turn, it renders what it posted, and the broker
            # carries it to everyone else.  Echoing it back would show one
            # client-side call twice in the client that ran it.
            assert not [f for f in second_frames if f.get("role") == "tool"], second_frames

            # And it is the session's conversation from now on, not only the
            # stream: a client that was not watching at all gets it on reload.
            turns = _conversation(chats_dir, session_id)["messages"]
            assert [
                (t.get("tool_use_id"), t.get("content"))
                for t in turns if t["role"] == "tool"
            ] == [("call_c1", "locals: x=None")]


def test_a_buffered_caller_still_brokers_the_result_it_posted(tmp_path) -> None:
    """The client that ran the tool does not have to be a streaming one.

    ``accessories/online_debugger.py`` drives its diagnosis through the buffered
    ``POST /v1/infer`` by default: that caller has no frames of its own at all,
    so a bystander's browser would get nothing from it unless the submitted
    result is published into the session's retained stream -- a decision that
    belongs to the shared pipeline, not to the SSE sink.
    """
    probe = {
        "name": "frame_probe",
        "description": "Dump a frame from the caller's process",
        "parameters": {"type": "object", "properties": {"fid": {"type": "string"}}},
    }
    streams = [
        _sse_tool_calls([("frame_probe", "call_c1", '{"fid": "f1"}')]),
        _sse_content("diagnosis: null pointer in frame f1"),
    ]
    with _fake_model_server(streams) as (api_base, _seen):
        with _running_server(tmp_path, api_base) as (srv, _chats_dir):
            base = f"http://127.0.0.1:{srv.port}"
            status, first = _post(base, "/v1/infer", {
                "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                "session_id": "new",
                "messages": [{"role": "user", "content": "inspect frame f1"}],
                "client_tools": [probe],
            })
            assert status == 200, first
            session_id = first["session_id"]

            status, second = _post(base, "/v1/infer", {
                "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                "session_id": session_id,
                "messages": [{
                    "role": "tool", "name": "frame_probe",
                    "tool_use_id": "call_c1", "content": "locals: x=None",
                }],
            })
            assert status == 200, second

            results = [
                f for f in _session_stream_frames(base, session_id)
                if f.get("role") == "tool"
            ]
            assert [(f.get("tool_use_id"), f.get("content")) for f in results] == [
                ("call_c1", "locals: x=None"),
            ], results


# ---------------------------------------------------------------------------
# 6. Several clients, one result: the first answer back wins
# ---------------------------------------------------------------------------

_PROBE = {
    "name": "frame_probe",
    "description": "Dump a frame from the caller's process",
    "parameters": {"type": "object", "properties": {"fid": {"type": "string"}}},
}


def _answer(payload_call: dict) -> dict:
    return {"role": "tool", "name": "frame_probe", **payload_call}


def test_a_second_answer_for_the_same_call_is_refused(tmp_path) -> None:
    """The conversation continues with the first answer; a later one is refused.

    Two browsers can both have ``AskUser`` and both be shown the same question,
    so two answers for one call are a normal accident rather than a caller bug.
    Accepting the second would run a competing turn on the same handoff and put
    two results on one call, so the service refuses it and says which call is
    already answered -- without touching the session.
    """
    streams = [
        _sse_tool_calls([("frame_probe", "call_c1", '{"fid": "f1"}')]),
        _sse_content("diagnosis: null pointer in frame f1"),
    ]
    with _fake_model_server(streams) as (api_base, seen):
        with _running_server(tmp_path, api_base) as (srv, chats_dir):
            base = f"http://127.0.0.1:{srv.port}"

            status, first = _post(base, "/v1/infer", {
                "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                "session_id": "new", "messages": [{"role": "user", "content": "inspect f1"}],
                "client_tools": [_PROBE],
            })
            assert status == 200, first
            session_id = first["session_id"]

            status, second = _post(base, "/v1/infer", {
                "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                "session_id": session_id,
                "messages": [_answer({"tool_use_id": "call_c1", "content": "first answer"})],
            })
            assert status == 200, second
            model_calls = len(seen)

            status, refused = _post(base, "/v1/infer", {
                "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                "session_id": session_id,
                "messages": [_answer({"tool_use_id": "call_c1", "content": "another client's answer"})],
            })

            assert status == 409, refused
            assert refused.get("code") == "client_tool_already_answered", refused
            assert refused.get("answered_call_ids") == ["call_c1"], refused
            assert "call_c1" in refused.get("error", ""), refused

            # Refusing means refusing: no model call, no extra turn, and the
            # first answer is still the only result of that call.
            assert len(seen) == model_calls, seen
            turns = _conversation(chats_dir, session_id)["messages"]
            assert [
                (t.get("tool_use_id"), t.get("content"))
                for t in turns if t["role"] == "tool"
            ] == [("call_c1", "first answer")], turns


def test_a_reused_call_id_is_a_new_call_not_a_duplicate() -> None:
    """An id the model declared *again* is waiting for its first result.

    Refusing a second result protects one call; it must not become a permanent
    ban on an id.  Not every model is disciplined about tool-call id uniqueness
    across rounds, and ``accessories/online_debugger.py`` drives sessions that
    keep handing the same tool back -- wedging such a session with
    "already answered" would be far worse than the duplicate it guards against.
    """
    declared = {"role": "assistant", "tool_calls": [{"id": "call_1", "name": "AskUser"}]}
    answered = {"role": "tool", "tool_use_id": "call_1", "name": "AskUser"}

    # Answered, and nothing has re-declared it: a second answer is a duplicate.
    assert answered_client_tool_call_ids([declared, answered], {"AskUser"}) == {"call_1"}
    # Declared again after that result: the call is open again.
    assert answered_client_tool_call_ids(
        [declared, answered, declared], {"AskUser"},
    ) == set()
    # A call of a tool this session does not treat as client-side is nobody's
    # client-side business: the service ran it itself.
    assert answered_client_tool_call_ids([declared, answered], {"other_tool"}) == set()
    # Without client-side tools there is nothing to arbitrate.
    assert answered_client_tool_call_ids([declared, answered], set()) == set()


def test_a_batch_is_answered_by_one_client_or_not_at_all(tmp_path) -> None:
    """Parallel calls of one round are handed back and answered as one batch.

    A request that answers two calls but conflicts on one of them gets the whole
    batch back as refused: quietly keeping the half that did not conflict would
    leave the conversation with a half-answered round.  What stays pending stays
    answerable, so the client that is still holding ``call_c2`` can post it.
    """
    streams = [
        _sse_tool_calls([
            ("frame_probe", "call_c1", '{"fid": "f1"}'),
            ("frame_probe", "call_c2", '{"fid": "f2"}'),
        ]),
        _sse_content("both frames diagnosed"),
    ]
    with _fake_model_server(streams) as (api_base, seen):
        with _running_server(tmp_path, api_base) as (srv, chats_dir):
            base = f"http://127.0.0.1:{srv.port}"

            status, first = _post(base, "/v1/infer", {
                "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                "session_id": "new", "messages": [{"role": "user", "content": "inspect f1 f2"}],
                "client_tools": [_PROBE],
            })
            assert status == 200, first
            session_id = first["session_id"]
            assert [c["id"] for c in first.get("client_tool_calls", [])] == ["call_c1", "call_c2"]

            # One client answers the whole batch; the other client, which also
            # has the tool and was showing the same two questions, is too late.
            status, answered = _post(base, "/v1/infer", {
                "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                "session_id": session_id,
                "messages": [
                    _answer({"tool_use_id": "call_c1", "content": "first"}),
                    _answer({"tool_use_id": "call_c2", "content": "second"}),
                ],
            })
            assert status == 200, answered
            model_calls = len(seen)

            status, refused = _post(base, "/v1/infer", {
                "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                "session_id": session_id,
                "messages": [
                    _answer({"tool_use_id": "call_c1", "content": "mine"}),
                    _answer({"tool_use_id": "call_c2", "content": "mine too"}),
                ],
            })
            assert status == 409, refused
            assert refused.get("answered_call_ids") == ["call_c1", "call_c2"], refused
            assert len(seen) == model_calls, seen
            assert [
                t.get("content") for t in _conversation(chats_dir, session_id)["messages"]
                if t["role"] == "tool"
            ] == ["first", "second"]


def test_the_in_flight_claim_leaves_a_failed_attempt_answerable_again() -> None:
    """The in-memory half of the arbitration, without a server around.

    Two answers can be in flight at the same moment, before either reached
    conversation.json, so the history cannot arbitrate yet.  The claim table
    settles that window all-or-nothing, gives the calls back when the request
    carrying them is over, and expires a claim whose holder disappeared.
    """
    from runtime.server_state import (
        claim_client_tool_answers,
        release_client_tool_answers,
    )

    session_id = "s-claim-arbitration"
    try:
        assert claim_client_tool_answers(session_id, ["call_1", "call_2"]) == []
        # Conflicting batch: refused, and nothing of it is claimed either.
        assert claim_client_tool_answers(session_id, ["call_2", "call_3"]) == ["call_2"]
        assert claim_client_tool_answers(session_id, ["call_3"]) == []

        release_client_tool_answers(session_id, ["call_2"])
        assert claim_client_tool_answers(session_id, ["call_2"]) == []

        # A handler that died without releasing must not wedge the session:
        # once the claim expires the call can be answered again.
        with patch.dict("os.environ", {"CLIENT_ANSWER_CLAIM_TTL_S": "0"}):
            assert claim_client_tool_answers(session_id, ["call_9"]) == []
            assert claim_client_tool_answers(session_id, ["call_9"]) == []
    finally:
        release_client_tool_answers(session_id)


def test_a_device_that_joins_after_the_handoff_can_be_the_one_that_answers(tmp_path) -> None:
    """The question belongs to whoever is in front of the session now.

    A handoff is not addressed to the client that happened to be streaming: a
    session is commonly open on the office machine and on the phone at once.
    Here the desktop streamed the round, saw the ``client_tool_calls`` frame and
    its user walked away without answering; the phone opened the session later,
    picked the question up from what the session has *persisted*, and its answer
    is the one the conversation continues with.  The desktop becomes a bystander
    of its own question: it is shown the phone's result, and clicking its own
    stale dialog afterwards is refused rather than starting a competing turn.

    Nothing about the handoff is claimable up front -- every client with the tool
    is expected to show the question, and the first answer back decides.
    """
    streams = [
        _sse_tool_calls([("frame_probe", "call_c1", '{"fid": "f1"}')]),
        _sse_content("diagnosis: null pointer in frame f1"),
    ]
    with _fake_model_server(streams) as (api_base, seen):
        with _running_server(tmp_path, api_base) as (srv, chats_dir):
            base = f"http://127.0.0.1:{srv.port}"

            req = urllib.request.Request(
                base + "/v1/infer/stream",
                data=json.dumps({
                    "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                    "session_id": "new",
                    "messages": [{"role": "user", "content": "inspect frame f1"}],
                    "client_tools": [_PROBE],
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                desktop_frames = [
                    json.loads(line[6:]) for line in
                    resp.read().decode("utf-8", "replace").splitlines()
                    if line.startswith("data: ") and line[6:] != "[DONE]"
                ]
            session_id = next(
                f["session_id"] for f in desktop_frames if f.get("session_id")
            )
            assert any(
                f.get("status") == "client_tool_calls" for f in desktop_frames
            ), desktop_frames

            # The phone joins with no knowledge of that frame: it reads the
            # session, and the call is still open in what was persisted.  Being
            # late to the handoff must not make the question unanswerable.
            open_calls = pending_client_tool_calls(
                _conversation(chats_dir, session_id)["messages"], {"frame_probe"},
            )
            assert [c["id"] for c in open_calls] == ["call_c1"], open_calls

            status, resumed = _post(base, "/v1/infer", {
                "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                "session_id": session_id,
                "messages": [_answer({
                    "tool_use_id": "call_c1", "content": "answered on the phone",
                })],
            })
            assert status == 200, resumed

            # The desktop keeps watching the same session, so its view gets the
            # phone's result: that is what closes its still-open dialog and
            # replaces its own pending call.
            bystander = _session_stream_frames(base, session_id)
            assert [
                (f.get("tool_use_id"), f.get("content"))
                for f in bystander if f.get("role") == "tool"
            ] == [("call_c1", "answered on the phone")], bystander

            # A stale click on the desktop is refused without side effects; the
            # phone's answer stays the conversation's only result of that call.
            status, refused = _post(base, "/v1/infer", {
                "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                "session_id": session_id,
                "messages": [_answer({
                    "tool_use_id": "call_c1", "content": "answered at the desk",
                })],
            })
            assert status == 409, refused
            assert refused.get("answered_call_ids") == ["call_c1"], refused
            assert [
                t.get("content") for t in _conversation(chats_dir, session_id)["messages"]
                if t["role"] == "tool"
            ] == ["answered on the phone"]
            assert len(seen) == 2, seen


class _HeldStream(io.BytesIO):
    """An SSE body the fake model withholds until *gate* opens.

    Holding one inference's generation open is what makes two answers of the
    same handoff genuinely overlap in time.  By then the first answer may or may
    not be on disk -- the history half of the arbitration and the in-memory claim
    half cover different moments of that race -- so what a test can pin is the
    outcome the protocol promises: one refusal, one result, one model call.
    """

    def __init__(self, data: bytes, gate: threading.Event) -> None:
        super().__init__(data)
        self.gate = gate

    def getvalue(self) -> bytes:
        assert self.gate.wait(30), "the held model answer was never released"
        return super().getvalue()


def test_two_clients_answering_at_the_same_moment_one_wins(tmp_path) -> None:
    """Two answers in flight at once: the first to arrive continues the session.

    A handoff whose tool is available on several clients can be answered twice
    within the same second.  Whatever half of the arbitration sees it first --
    the answer already persisted by the request that got there first, or the
    claim it holds while its generation runs -- the other client is refused
    instead of starting a competing turn, and the session ends up with exactly
    one result for that call.  The generation is held open here (the fake model
    withholds its answer) so the two submissions really do overlap.
    """
    gate = threading.Event()
    streams = [
        _sse_tool_calls([("frame_probe", "call_c1", '{"fid": "f1"}')]),
        _HeldStream(_sse_content("diagnosis: null pointer in frame f1").getvalue(), gate),
    ]
    with _fake_model_server(streams) as (api_base, seen):
        with _running_server(tmp_path, api_base) as (srv, chats_dir):
            base = f"http://127.0.0.1:{srv.port}"

            status, first = _post(base, "/v1/infer", {
                "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                "session_id": "new", "messages": [{"role": "user", "content": "inspect f1"}],
                "client_tools": [_PROBE],
            })
            assert status == 200, first
            session_id = first["session_id"]

            outcome: dict = {}

            def _first_client_answers() -> None:
                outcome["status"], outcome["body"] = _post(base, "/v1/infer", {
                    "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                    "session_id": session_id,
                    "messages": [_answer({
                        "tool_use_id": "call_c1", "content": "the first client in",
                    })],
                })

            winner = threading.Thread(target=_first_client_answers)
            winner.start()
            try:
                # The winner is inside its generation (the model answer is held),
                # so it is still the only client that can answer this call.
                deadline = time.time() + 15
                while len(seen) < 2 and time.time() < deadline:
                    time.sleep(0.02)
                assert len(seen) == 2, seen

                status, refused = _post(base, "/v1/infer", {
                    "model_id": "test-model", "tool_ids": [], "workspace": str(tmp_path),
                    "session_id": session_id,
                    "messages": [_answer({
                        "tool_use_id": "call_c1", "content": "the second client",
                    })],
                })
                assert status == 409, refused
                assert refused.get("code") == "client_tool_already_answered", refused
                assert refused.get("answered_call_ids") == ["call_c1"], refused
            finally:
                gate.set()
                winner.join(timeout=30)

            assert outcome.get("status") == 200, outcome
            assert len(seen) == 2, seen          # the refused one called no model
            assert [
                t.get("content") for t in _conversation(chats_dir, session_id)["messages"]
                if t["role"] == "tool"
            ] == ["the first client in"]


def test_a_refused_in_flight_answer_claims_nothing_and_frees_nothing() -> None:
    """A refusal claims nothing, and must not give back what someone else holds.

    ``_claim_client_tool_answers`` returns ``(conflicts, claimed)`` and a
    request's cleanup releases exactly ``claimed``.  Hand back the ids the claim
    table *refused* as though this request owned them and the losing client ends
    its request by releasing the claim of a generation that is still running --
    which both hides the refusal (the caller sends no 409) and reopens the race
    for a third answer.
    """
    from runtime.handler_infer import HandlerInferMixin
    from runtime.server_state import (
        claim_client_tool_answers,
        release_client_tool_answers,
    )

    session_id = "s-refused-claims"

    class _StubHandler:
        def _client_side_tool_names(self, request) -> set:
            return {"frame_probe"}

    class _NoHistory:
        def load_conversation(self, session):
            return []      # nothing persisted yet: only the claim table can arbitrate

    body = {"messages": [_answer({"tool_use_id": "call_c1", "content": "second client"})]}
    try:
        # Another request of this session is already carrying the answer.
        assert claim_client_tool_answers(session_id, ["call_c1"]) == []

        conflicts, claimed = HandlerInferMixin._claim_client_tool_answers(
            _StubHandler(), body, None, _NoHistory(), session_id,
        )
        assert conflicts == ["call_c1"], conflicts     # the caller answers 409
        assert claimed == [], claimed                  # and holds nothing to release

        release_client_tool_answers(session_id, claimed)   # the loser's cleanup
        assert claim_client_tool_answers(session_id, ["call_c1"]) == ["call_c1"], \
            "the loser released a claim it never took"
    finally:
        release_client_tool_answers(session_id)
