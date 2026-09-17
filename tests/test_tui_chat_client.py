"""Tests for tui/chat_client.py (contract §4.1).

Uses a local fake HTTP server on 127.0.0.1 (no external network) to assert
auth header / query assembly, cookie handling, and canned SSE byte streams:
init / message / usage / [DONE] / heartbeat / id lines / error frames /
connection interruption.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tui.chat_client import ApiError, InferHandle, ServiceClient  # noqa: E402


# ===========================================================================
# Fake service
# ===========================================================================

class FakeService:
    """Single-port fake Agent Service with a route table.

    ``routes`` maps a path to either
      - a 3-tuple ``(status, headers, body_bytes)`` or
      - a callable ``fn(handler, body_bytes)`` that writes its own response
        (used for SSE streaming) and returns None.
    Every request is recorded in ``requests`` as a dict with method/path/
    headers/body.
    """

    def __init__(self) -> None:
        self.requests = []
        self.routes = {}
        self.httpd = None
        self.thread = None

    def start(self) -> None:
        service = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def _read_body(self) -> bytes:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    n = 0
                return self.rfile.read(n) if n > 0 else b""

            def _handle(self) -> None:
                body = self._read_body()
                service.requests.append({
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                })
                path = urllib.parse.urlparse(self.path).path
                route = service.routes.get(path)
                if route is None:
                    payload = json.dumps(
                        {"error": f"no route for {path}"}
                    ).encode("utf-8")
                    self._reply(404, {"Content-Type": "application/json"},
                                payload)
                    return
                if callable(route):
                    if route(self, body) is not None:
                        return
                    return
                status, headers, payload = route
                self._reply(status, headers, payload)

            def _reply(self, status: int, headers: dict, payload: bytes) -> None:
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_DELETE = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        self.thread.start()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def last_request(self) -> dict:
        return self.requests[-1]


@pytest.fixture()
def service():
    fake = FakeService()
    fake.start()
    yield fake
    fake.stop()


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


# ===========================================================================
# Auth assembly (query token / Bearer header / cookie jar)
# ===========================================================================

class TestAuthAssembly:
    def test_get_appends_token_query(self, service):
        service.routes["/v1/auth/config"] = (
            200, {"Content-Type": "application/json"},
            _json_bytes({"auth_enabled": False}),
        )
        client = ServiceClient(service.base_url, token="as_abc123")
        status = client.auth_status()
        assert status == {"auth_enabled": False}
        req = service.last_request()
        assert req["method"] == "GET"
        assert "token=as_abc123" in req["path"]
        assert "authorization" not in req["headers"]

    def test_write_uses_bearer_header(self, service):
        service.routes["/v1/auth/login"] = (
            200, {"Content-Type": "application/json"}, _json_bytes({"ok": True}),
        )
        client = ServiceClient(service.base_url, token="st_xyz")
        client.post("/v1/auth/login", {"password": "pw"})
        req = service.last_request()
        assert req["headers"].get("authorization") == "Bearer st_xyz"
        assert "token=" not in req["path"]

    def test_no_token_sends_no_credential(self, service):
        service.routes["/v1/env"] = (
            200, {"Content-Type": "application/json"},
            _json_bytes({"env": {}}),
        )
        client = ServiceClient(service.base_url)
        client.get("/v1/env")
        req = service.last_request()
        assert "token=" not in req["path"]
        assert "authorization" not in req["headers"]

    def test_put_delete_bearer(self, service):
        service.routes["/v1/x"] = (
            200, {"Content-Type": "application/json"}, _json_bytes({"ok": True}),
        )
        client = ServiceClient(service.base_url, token="as_k")
        client.put("/v1/x", {"a": 1})
        assert service.last_request()["headers"].get("authorization") \
            == "Bearer as_k"
        client.delete("/v1/x")
        req = service.last_request()
        assert req["method"] == "DELETE"
        assert req["headers"].get("authorization") == "Bearer as_k"

    def test_login_stores_session_cookie_and_sends_it(self, service):
        def login_route(handler, body):
            data = json.loads(body.decode("utf-8"))
            if data.get("password") == "secret123":
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.send_header(
                    "Set-Cookie",
                    "agent_service_session=st_cookie_value; Path=/; HttpOnly",
                )
                payload = _json_bytes({"ok": True})
                handler.send_header("Content-Length", str(len(payload)))
                handler.end_headers()
                handler.wfile.write(payload)
            else:
                payload = _json_bytes({"error": "invalid_password"})
                handler.send_response(401)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(payload)))
                handler.end_headers()
                handler.wfile.write(payload)

        service.routes["/v1/auth/login"] = login_route
        service.routes["/v1/sessions"] = (
            200, {"Content-Type": "application/json"},
            _json_bytes({"sessions": []}),
        )
        client = ServiceClient(service.base_url)
        assert client.login("secret123") is True
        client.get("/v1/sessions")
        cookie = service.last_request()["headers"].get("cookie", "")
        assert "agent_service_session=st_cookie_value" in cookie

    def test_login_wrong_password_returns_false(self, service):
        def login_route(handler, body):
            payload = _json_bytes({"error": "invalid_password"})
            handler.send_response(401)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)

        service.routes["/v1/auth/login"] = login_route
        client = ServiceClient(service.base_url)
        assert client.login("wrong") is False

    def test_auth_status_401_returns_none(self, service):
        service.routes["/v1/auth/config"] = (
            401, {"Content-Type": "application/json"},
            _json_bytes({"error": "unauthorized"}),
        )
        client = ServiceClient(service.base_url)
        assert client.auth_status() is None

    def test_non_2xx_raises_api_error_with_json_message(self, service):
        service.routes["/v1/agents/nope"] = (
            404, {"Content-Type": "application/json"},
            _json_bytes({"error": "Agent not found: nope"}),
        )
        client = ServiceClient(service.base_url)
        with pytest.raises(ApiError) as excinfo:
            client.get("/v1/agents/nope")
        assert excinfo.value.status == 404
        assert "Agent not found" in excinfo.value.message

    def test_connection_refused_raises_url_error(self):
        # A closed port: nothing listens on 1 (privileged, definitely down).
        client = ServiceClient("http://127.0.0.1:1")
        with pytest.raises(urllib.error.URLError):
            client.get("/v1/env")


# ===========================================================================
# Canned SSE streams (POST /v1/infer/stream)
# ===========================================================================

def _sse_stream_route(chunks, delay=0.0):
    """Build a route callable that streams *chunks* (bytes) with flushes."""

    def route(handler, body):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Connection", "close")
        handler.end_headers()
        for chunk in chunks:
            handler.wfile.write(chunk)
            handler.wfile.flush()
            if delay:
                time.sleep(delay)
        return None

    return route


def _wait_handle(handle: InferHandle, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while not handle.done():
        if time.time() > deadline:
            return False
        handle.wait(0.05)
    return True


def _collect(chunks, body=None):
    """Build ``run()``; calling it runs one infer_stream against a canned
    byte stream on a throwaway fake service and returns ``(events, handle)``."""

    def _run():
        service = FakeService()
        service.start()
        try:
            service.routes["/v1/infer/stream"] = _sse_stream_route(chunks)
            client = ServiceClient(service.base_url, token="as_t")
            events = []
            handle = client.infer_stream(
                body or {}, lambda ev, data: events.append((ev, data))
            )
            _wait_handle(handle)
            return events, handle
        finally:
            service.stop()

    return _run


class TestSSEParsing:
    def test_full_sequence_init_message_usage_done(self):
        init = {"session_id": "s-1", "type": "init", "title": "你好",
                "stream_seq": 2}
        chunks = [
            b"event: init\ndata: " + _json_bytes(init) + b"\n\n",
            b": keepalive\n\n",
            b"id: 1\ndata: " + _json_bytes(
                {"role": "user", "content": "你好"}
            ) + b"\n\n",
            b"id: 2\ndata: " + _json_bytes(
                {"role": "assistant", "content": "Hello"}
            ) + b"\n\n",
            b"id: 3\nevent: usage\ndata: " + _json_bytes(
                {"prompt_tokens": 7, "completion_tokens": 3,
                 "total_tokens": 10, "overall_ms": 420}
            ) + b"\n\n",
            b"data: [DONE]\n\n",
        ]
        events, handle = _collect(chunks)()
        kinds = [ev for ev, _ in events]
        assert kinds == ["init", "message", "message", "usage", "done"]
        assert events[0][1]["session_id"] == "s-1"
        assert events[1][1]["content"] == "你好"
        assert events[3][1]["overall_ms"] == 420
        assert handle.session_id == "s-1"
        assert handle.done()

    def test_id_lines_and_heartbeats_are_tolerated(self):
        chunks = [
            b"event: init\ndata: {\"session_id\": \"s-9\"}\n\n",
            b": keepalive\n\n",
            b": another comment\n\n",
            b"id: 42\nid: 43\ndata: " + _json_bytes(
                {"role": "assistant", "content": "x"}
            ) + b"\n\n",
            b"data: [DONE]\n\n",
        ]
        events, _ = _collect(chunks)()
        assert [ev for ev, _ in events] == ["init", "message", "done"]

    def test_multi_line_data_is_joined(self):
        # two data: lines joined with \n per the SSE spec → one JSON dict
        chunks = [
            b"id: 1\n"
            b'data: {"role": "assistant",\n'
            b'data: "content": "multi line"}\n\n'
            b"data: [DONE]\n\n",
        ]
        events, _ = _collect(chunks)()
        assert events[0][0] == "message"
        assert events[0][1] == {"role": "assistant", "content": "multi line"}

    def test_utf8_split_across_chunks(self):
        # 你好世界 in UTF-8 is 12 bytes; feed 3-byte chunks so every
        # multi-byte character is split across chunk boundaries.
        frame = (b"event: init\ndata: "
                 + _json_bytes({"session_id": "s-u", "title": "你好世界"})
                 + b"\n\ndata: [DONE]\n\n")
        chunks = [frame[i:i + 3] for i in range(0, len(frame), 3)]
        events, _ = _collect(chunks)()
        assert events[0][0] == "init"
        assert events[0][1]["title"] == "你好世界"
        assert events[-1][0] == "done"

    def test_data_done_without_space(self):
        chunks = [
            b"event: init\ndata: {\"session_id\": \"s-x\"}\n\n",
            b"data:[DONE]\n\n",
        ]
        events, _ = _collect(chunks)()
        assert [ev for ev, _ in events] == ["init", "done"]

    def test_error_event_frame(self):
        chunks = [
            b"event: init\ndata: {\"session_id\": \"s-e\"}\n\n",
            b"event: error\ndata: " + _json_bytes(
                {"message": "model exploded"}
            ) + b"\n\n",
            b"data: [DONE]\n\n",
        ]
        events, _ = _collect(chunks)()
        assert ("error", {"message": "model exploded"}) in events
        assert events[-1][0] == "done"

    def test_invalid_json_data_becomes_error_event(self):
        chunks = [
            b"event: init\ndata: {\"session_id\": \"s-b\"}\n\n",
            b"data: {not json at all}\n\n",
            b"data: [DONE]\n\n",
        ]
        events, _ = _collect(chunks)()
        assert ("error", {"message": "invalid SSE data: {not json at all}"}) \
            in events

    def test_non_2xx_json_error_body(self):
        service = FakeService()
        service.start()
        try:
            def route(handler, body):
                payload = _json_bytes({"error": "invalid model: no-such"})
                handler.send_response(400)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(payload)))
                handler.end_headers()
                handler.wfile.write(payload)
                return None

            service.routes["/v1/infer/stream"] = route
            client = ServiceClient(service.base_url)
            events = []
            handle = client.infer_stream(
                {"model_id": "no-such"},
                lambda ev, data: events.append((ev, data)),
            )
            assert _wait_handle(handle)
        finally:
            service.stop()
        assert len(events) == 1
        ev, data = events[0]
        assert ev == "error"
        assert "400" in data["message"]
        assert "invalid model" in data["message"]
        assert handle.done()

    def test_connection_interrupted_before_done(self):
        # Complete frames, then the connection is closed without [DONE].
        chunks = [
            b"event: init\ndata: {\"session_id\": \"s-i\"}\n\n",
            b"id: 1\ndata: " + _json_bytes(
                {"role": "assistant", "content": "partial"}
            ) + b"\n\n",
            # (no [DONE]; server closes the connection)
        ]
        events, handle = _collect(chunks)()
        kinds = [ev for ev, _ in events]
        assert kinds[:2] == ["init", "message"]
        assert kinds[-1] == "error"
        assert "before [DONE]" in events[-1][1]["message"]
        assert handle.done()

    def test_request_carries_bearer_and_json_body(self):
        seen = {}

        def route(handler, body):
            seen["headers"] = {k.lower(): v for k, v in handler.headers.items()}
            seen["body"] = body
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(b"data: [DONE]\n\n")
            return None

        service = FakeService()
        service.start()
        try:
            service.routes["/v1/infer/stream"] = route
            client = ServiceClient(service.base_url, token="as_body")
            events = []
            handle = client.infer_stream(
                {"model_id": "m", "stream": True, "session_id": "new"},
                lambda ev, data: events.append((ev, data)),
            )
            assert _wait_handle(handle)
        finally:
            service.stop()
        assert seen["headers"].get("authorization") == "Bearer as_body"
        sent = json.loads(seen["body"].decode("utf-8"))
        assert sent == {"model_id": "m", "stream": True, "session_id": "new"}
        assert events == [("done", {})]


class TestInferHandle:
    def test_abort_posts_infer_abort(self):
        service = FakeService()
        service.start()
        try:
            def route(handler, body):
                handler.send_response(200)
                handler.send_header("Content-Type", "text/event-stream")
                handler.send_header("Connection", "close")
                handler.end_headers()
                handler.wfile.write(
                    b"event: init\ndata: {\"session_id\": \"s-abc\"}\n\n"
                )
                # keep the stream open a moment so abort can be issued
                time.sleep(1.5)
                handler.wfile.write(b"data: [DONE]\n\n")
                return None

            service.routes["/v1/infer/stream"] = route
            service.routes["/v1/infer/abort"] = (
                200, {"Content-Type": "application/json"},
                _json_bytes({"ok": True}),
            )
            client = ServiceClient(service.base_url)
            handle = client.infer_stream({}, lambda ev, data: None)
            # wait for the init frame to reveal the session id
            deadline = time.time() + 5
            while handle.session_id is None and time.time() < deadline:
                handle.wait(0.05)
            assert handle.session_id == "s-abc"
            handle.abort()
            abort_req = [
                r for r in service.requests if r["path"].startswith(
                    "/v1/infer/abort")
            ][-1]
            assert abort_req["method"] == "POST"
            assert json.loads(abort_req["body"]) == {
                "session_id": "s-abc", "forced": False,
            }
            assert _wait_handle(handle)
        finally:
            service.stop()

    def test_abort_without_session_id_is_noop(self):
        handle = InferHandle(
            ServiceClient("http://127.0.0.1:1"), {}, lambda ev, data: None
        )
        handle.abort()  # must not raise
        assert _wait_handle(handle, timeout=5)
        # the stream thread reported a connection error
        handle.done()


class TestConvenienceMethods:
    def test_env(self, service):
        service.routes["/v1/env"] = (
            200, {"Content-Type": "application/json"},
            _json_bytes({"env": {"AGENTS_URL": "http://x:7988"}}),
        )
        client = ServiceClient(service.base_url)
        assert client.env() == {"env": {"AGENTS_URL": "http://x:7988"}}

    def test_agents_models_tools_unwrap_envelopes(self, service):
        service.routes["/v1/agents"] = (
            200, {"Content-Type": "application/json"},
            _json_bytes({"agents": [{"agent_id": "a1", "nickname": "阿一"}]}),
        )
        service.routes["/v1/models"] = (
            200, {"Content-Type": "application/json"},
            _json_bytes({"models": [{"model_id": "m1", "model_name": "M1"}]}),
        )
        service.routes["/v1/tools"] = (
            200, {"Content-Type": "application/json"},
            _json_bytes({"tools": [{"tool_id": "read_file"}]}),
        )
        client = ServiceClient(service.base_url)
        assert client.agents() == [{"agent_id": "a1", "nickname": "阿一"}]
        assert client.models() == [{"model_id": "m1", "model_name": "M1"}]
        assert client.tools() == [{"tool_id": "read_file"}]

    def test_sessions_uses_page_params_and_unwraps(self, service):
        service.routes["/v1/sessions"] = (
            200, {"Content-Type": "application/json"},
            _json_bytes({"sessions": [{"session_id": "s1"}],
                         "page": 1, "page_size": 20, "total": 1,
                         "has_more": False}),
        )
        client = ServiceClient(service.base_url)
        assert client.sessions(limit=20) == [{"session_id": "s1"}]
        req = service.last_request()
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(req["path"]).query
        )
        assert query == {"page": ["1"], "page_size": ["20"]}

    def test_conversation(self, service):
        service.routes["/v1/sessions/2024-01-01_12"] = (
            200, {"Content-Type": "application/json"},
            _json_bytes({"meta": {"session_id": "2024-01-01_12"},
                         "messages": []}),
        )
        client = ServiceClient(service.base_url)
        data = client.conversation("2024-01-01_12")
        assert data["meta"]["session_id"] == "2024-01-01_12"

    def test_tunnel_endpoints(self, service):
        service.routes["/v1/tunnel/parent/status"] = (
            200, {"Content-Type": "application/json"},
            _json_bytes({"configured": False, "state": "idle"}),
        )
        service.routes["/v1/tunnel/parent/register"] = (
            200, {"Content-Type": "application/json"},
            _json_bytes({"ok": True}),
        )
        service.routes["/v1/tunnel/parent/unregister"] = (
            200, {"Content-Type": "application/json"},
            _json_bytes({"ok": True}),
        )
        client = ServiceClient(service.base_url)
        assert client.tunnel_status() == {"configured": False, "state": "idle"}
        assert client.tunnel_register("http://parent:7988") == {"ok": True}
        req = [r for r in service.requests
               if r["path"].startswith("/v1/tunnel/parent/register")][-1]
        assert json.loads(req["body"]) == {"parent": "http://parent:7988"}
        assert client.tunnel_unregister() == {"ok": True}

    def test_tunnel_register_400_raises_api_error(self, service):
        service.routes["/v1/tunnel/parent/register"] = (
            400, {"Content-Type": "application/json"},
            _json_bytes({"ok": False, "error": "invalid parent URL"}),
        )
        client = ServiceClient(service.base_url)
        with pytest.raises(ApiError) as excinfo:
            client.tunnel_register("not-a-url")
        assert excinfo.value.status == 400
        assert "invalid parent URL" in excinfo.value.message
