"""Non-streaming ``POST /v1/infer`` must keep the session visible while it runs.

A diagnostic client that posts one prompt to the non-streaming endpoint and
then lets the agent run for hundreds of tool rounds used to find its own
session invisible until the answer came back: that endpoint wrote
``conversation.json`` only once, after the inference was over, so during the
whole run the web frontend saw

    Session not found: 261101_200350

even though the session directory (and its index entry) already existed —
``GET /v1/sessions/{id}`` serves ``conversation.json`` from disk and used to
*prune* the index entry on that 404, so the session also vanished from
``GET /v1/sessions``.  The streaming endpoint never had this problem because it
pre-persists the input turn and flushes every completed round.

These tests pin the aligned behaviour:

1. The non-streaming endpoint pre-persists the input turn and flushes each
   completed round, so the session is readable (and stays listed) mid-inference.
2. ``GET /v1/sessions/{id}`` never removes an index entry while the session
   directory still exists.
3. The alignment is structural, not a copy: ``POST /v1/infer`` is a synchronous
   shell that runs :meth:`_handle_infer_stream` with a ``BufferedInferSink``, so
   the default SSE transport must keep emitting exactly the bytes it did before
   the seam, and a non-streaming run is registered in ``active_infers`` — which
   is what makes it observable, takeover-able and abortable like a streamed one.
"""

import contextlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch

from runtime.models import Message, ModelConfig, ToolConfig
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _running_server(tmp_path):
    """Yield ``(server, chats_dir)`` on an ephemeral port with redirected state.

    ``server._runtime.infer_stream`` is patched by the caller; the stub model
    only has to exist to pass request validation.
    """
    chats_dir = tmp_path / "chats"
    with contextlib.ExitStack() as stack:
        for target, value in (
            ("runtime.server._MODELS_PATH", "models.json"),
            ("runtime.server._TOOLS_PATH", "tools.json"),
            ("runtime.server._PROMPT_TEMPLATES_PATH", "prompt_templates.json"),
            ("runtime.server._DATA_DIR", "."),
        ):
            stack.enter_context(patch(target, str(tmp_path / value)))

        model_reg = ModelRegistry()
        model_reg.register(ModelConfig(
            model_id="stub-model",
            api_base="http://localhost:1",
            model_name="stub",
            api_protocol="openai",
        ))
        tool_reg = ToolRegistry()
        tool_reg.register(
            ToolConfig(tool_id="echo", tool_type="function", name="echo",
                       description="echo tool", parameters={}),
            callable_fn=lambda **kw: "echo-out",
        )
        srv = RuntimeHTTPServer(Runtime(model_reg, tool_reg),
                                chats_dir=str(chats_dir))
        srv.start_background(host="127.0.0.1", port=0)
        try:
            yield srv, str(chats_dir)
        finally:
            srv.stop()


def _get(base: str, path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(base + path, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _post(base: str, path: str, payload: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _session_ids(base: str) -> list[str]:
    """Session ids currently listed by ``GET /v1/sessions`` (paginated object)."""
    status, body = _get(base, "/v1/sessions")
    if status != 200:
        return []
    try:
        data = json.loads(body)
    except Exception:
        return []
    entries = data.get("sessions", data) if isinstance(data, dict) else data
    return [e.get("session_id") or e.get("id") for e in entries if isinstance(e, dict)]


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    """Poll ``predicate`` until it is true (index writes are asynchronous)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# 1. The session is readable — and still listed — while inference runs
# ---------------------------------------------------------------------------

def test_non_stream_infer_persists_progress(tmp_path):
    release = threading.Event()
    in_round = threading.Event()

    # POST /v1/infer runs the streaming pipeline, so the generator is called
    # with the pipeline's own kwargs (cancel_event, pause_gate, close_gate,
    # on_round_complete, on_context_overflow) — accept and ignore them.
    def _infer_stream(request, **_pipeline_kwargs):
        # One completed round (assistant tool_call + tool result) is emitted
        # first, then the generator blocks: everything after this point happens
        # while a client would be polling the session endpoints.
        for msg in (request.messages or []):
            yield msg
        yield Message(role="assistant", content="", tool_calls=[
            {"id": "call_1", "name": "echo", "arguments": {}}])
        yield Message(role="tool", name="echo", tool_id="echo",
                      tool_use_id="call_1", content="echo-out")
        in_round.set()
        release.wait(timeout=20)
        yield Message(role="assistant", content="final answer")
        yield Message(role="usage", content=json.dumps(
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}))

    with _running_server(tmp_path) as (srv, chats_dir):
        srv._runtime.infer_stream = _infer_stream
        base = f"http://127.0.0.1:{srv.port}"

        poster_result: list[tuple[int, str]] = []
        poster = threading.Thread(
            target=lambda: poster_result.append(_post(base, "/v1/infer", {
                "model_id": "stub-model",
                "tool_ids": ["echo"],
                "workspace": str(tmp_path),
                "session_id": "new",
                "messages": [{"role": "user", "content": "长任务：请多轮排查"}],
            })),
            daemon=True,
        )
        poster.start()
        try:
            if not in_round.wait(timeout=10):
                raise AssertionError(
                    f"inference never reached the blocking point: {poster_result[:1]}")
            assert _wait_until(lambda: bool(_session_ids(base))), "index entry not created"
            session_id = _session_ids(base)[0]

            # The session directory exists *and* the session is readable while
            # inference is still running.
            assert os.path.isdir(os.path.join(chats_dir, session_id))
            status, mid_body = _get(base, f"/v1/sessions/{session_id}")
            assert status == 200, f"mid-inference GET returned {status}: {mid_body[:200]}"
            assert "长任务：请多轮排查" in mid_body, mid_body[:200]
            # The completed round was flushed as well — this is what lets a web
            # page show progress instead of an empty/missing session.
            assert "echo-out" in mid_body, mid_body[:400]
            assert session_id in _session_ids(base), "session dropped from the index"

            release.set()
            poster.join(timeout=20)
            assert not poster.is_alive(), "inference did not finish"

            status, final_body = _get(base, f"/v1/sessions/{session_id}")
            assert status == 200
            assert "final answer" in final_body
        finally:
            release.set()


# ---------------------------------------------------------------------------
# 2. GET must not prune the index while the session directory survives
# ---------------------------------------------------------------------------

def test_get_session_keeps_index_entry_when_dir_survives(tmp_path):
    with _running_server(tmp_path) as (srv, chats_dir):
        base = f"http://127.0.0.1:{srv.port}"
        session_id = "261101_200350"
        srv._session_manager.on_session_created(session_id, "推理中的会话")
        # 建档：会话目录已创建，conversation.json 要等第一个落盘点才出现。
        os.makedirs(os.path.join(chats_dir, session_id), exist_ok=True)
        assert _wait_until(lambda: session_id in _session_ids(base)), "index entry not created"

        status, body = _get(base, f"/v1/sessions/{session_id}")
        assert status == 404 and "Session not found" in body

        # 目录还在 —— 索引条目必须保留，否则推理期间侧边栏会凭空少一条会话。
        time.sleep(0.3)  # let any (wrong) asynchronous index rewrite land
        assert session_id in _session_ids(base), \
            "index entry was pruned while the session directory still exists"

        # 目录真没了（手工删目录 / 清理残留）才回收索引条目。
        os.rmdir(os.path.join(chats_dir, session_id))
        assert _get(base, f"/v1/sessions/{session_id}")[0] == 404
        time.sleep(0.3)
        assert session_id not in _session_ids(base), \
            "index entry should be pruned once the session directory is gone"


# ---------------------------------------------------------------------------
# 3. The sink seam must not change a single byte of the streaming transport
# ---------------------------------------------------------------------------

class _FakeWfile:
    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, payload):
        self.chunks.append(payload)

    def flush(self):
        pass


class _FakeHandler:
    """Only what ``SseStreamSink`` touches of a BaseHTTPRequestHandler."""

    def __init__(self):
        self.wfile = _FakeWfile()
        self.close_connection = False
        self.calls: list[tuple] = []

    def send_response(self, status):
        self.calls.append(("response", status))

    def send_header(self, key, value):
        self.calls.append(("header", key, value))

    def end_headers(self):
        self.calls.append(("end_headers",))


def test_sse_sink_reproduces_the_legacy_transport():
    """流式端点的对外字节逐条钉住：这是接缝唯一可能改变行为的地方。"""
    from runtime.handler_infer import SseStreamSink

    handler = _FakeHandler()
    sink = SseStreamSink(handler)
    sink.begin("261101_200350")

    assert handler.calls == [
        ("response", 200),
        ("header", "Content-Type", "text/event-stream; charset=utf-8"),
        ("header", "Cache-Control", "no-cache"),
        ("header", "Connection", "close"),
        ("header", "X-Session-Id", "261101_200350"),
        ("end_headers",),
    ], handler.calls
    # SSE 不能 keep-alive：跑完就断开，供 handoff/续流判定连接已终结。
    assert handler.close_connection is True

    sink.write(b"event: init\ndata: {}\n\n")
    sink.heartbeat()
    sink.error('boom"x')
    assert handler.wfile.chunks == [
        b"event: init\ndata: {}\n\n",
        b": keepalive\n\n",
        b'data: {"error": "boom\\"x"}\n\n',
    ], handler.wfile.chunks

    # 无会话的请求不发 X-Session-Id（与接缝之前的条件分支一致）。
    anonymous = _FakeHandler()
    SseStreamSink(anonymous).begin(None)
    assert [c for c in anonymous.calls if c[0] == "header" and c[1] == "X-Session-Id"] == []


# ---------------------------------------------------------------------------
# 4. A non-streaming run is a first-class inference: registered and abortable
# ---------------------------------------------------------------------------

def test_non_stream_run_is_registered_and_abortable(tmp_path):
    """``/v1/infer`` 如今注册进 ``active_infers``，因此 ``/v1/infer/abort`` 能停它。

    这是"两条管线合成一条"直接买到的能力：以前非流式推理既不进
    ``active_infers`` 也不发状态流，前端既看不见也停不了它。
    """
    in_round = threading.Event()

    def _infer_stream(request, cancel_event=None, **_pipeline_kwargs):
        for msg in (request.messages or []):
            yield msg
        yield Message(role="assistant", content="", tool_calls=[
            {"id": "call_1", "name": "echo", "arguments": {}}])
        yield Message(role="tool", name="echo", tool_id="echo",
                      tool_use_id="call_1", content="echo-out")
        in_round.set()
        # 真实工具循环在轮边界看 cancel_event；这里同样只在被中止后收尾。
        # 文案与 runtime.py 中止时产出的一致（"Error: user interrupted."，
        # 无换行前缀 —— 带 \n\n 的那条只在 BrokenPipe 分支里追加）。
        while cancel_event is not None and not cancel_event.is_set():
            time.sleep(0.02)
        if cancel_event is not None and cancel_event.is_set():
            yield Message(role="assistant", content="Error: user interrupted.")

    with _running_server(tmp_path) as (srv, chats_dir):
        srv._runtime.infer_stream = _infer_stream
        base = f"http://127.0.0.1:{srv.port}"

        poster_result: list[tuple[int, str]] = []
        poster = threading.Thread(
            target=lambda: poster_result.append(_post(base, "/v1/infer", {
                "model_id": "stub-model",
                "tool_ids": ["echo"],
                "workspace": str(tmp_path),
                "session_id": "new",
                "messages": [{"role": "user", "content": "停下来"}],
            })),
            daemon=True,
        )
        poster.start()
        try:
            if not in_round.wait(timeout=10):
                raise AssertionError(
                    f"inference never reached the blocking point: {poster_result[:1]}")
            assert _wait_until(lambda: bool(_session_ids(base))), "index entry not created"
            session_id = _session_ids(base)[0]

            # 注册进 active_infers —— 中止、热更新保护与接管都以此为前提。
            assert _wait_until(lambda: session_id in srv._active_infers), \
                "non-streaming run is not tracked in active_infers"
            assert isinstance(srv._active_infers[session_id], threading.Event)

            status, body = _post(base, "/v1/infer/abort",
                                 {"session_id": session_id, "action": "stop"})
            assert status == 200, f"abort returned {status}: {body[:200]}"

            poster.join(timeout=20)
            assert not poster.is_alive(), "aborted inference did not return"
            status, response_body = poster_result[0]
            assert status == 500, f"expected 500 after abort, got {status}: {response_body[:200]}"
            data = json.loads(response_body)
            assert data.get("success") is False, response_body[:200]
            assert "Error: user interrupted." in json.dumps(data, ensure_ascii=False)

            # 收尾后必须注销，否则 /v1/setup 会一直报 inference_active 而挡住更新。
            assert _wait_until(lambda: session_id not in srv._active_infers, timeout=10), \
                "active_infers entry was not released"

            with open(os.path.join(chats_dir, session_id, "conversation.json"),
                      encoding="utf-8") as fp:
                assert "Error: user interrupted." in fp.read()
        finally:
            if in_round.is_set():
                poster.join(timeout=20)
