"""Tests for the voice-input bridge (runtime/handler_asr.py).

The chat page talks to ``/v1/asr/health`` and ``/v1/asr/ws`` on the runtime
port; the runtime relays to the accessories/ASR service.  These tests pin the
contract on both sides of that relay with a minimal fake ASR service:

* the health probe answers 200 with ``available`` true/false (the mic button
  reads it to explain itself),
* WebSocket frames are relayed in both directions, verbatim (text control
  frames up, JSON transcript events down, binary PCM untouched),
* the browser's query string is filtered: ``sample_rate`` / ``session_id`` /
  ``last_seg_id`` are forwarded, a browser-supplied ``token`` is not,
* an unreachable ASR service is reported as an ``asr_unreachable`` error frame
  over the opened socket rather than as an opaque failed upgrade.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from runtime import wsutil
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer
from runtime.wsclient import WSClient, WSClientError


# ------------------------------------------------------------------
# Fake ASR service (stands in for accessories/ASR/ASR_server.py)
# ------------------------------------------------------------------

class FakeAsrService:
    """``GET /health`` plus a WebSocket that answers the control frames."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self.request_paths: list[str] = []
        self.text_frames: list[dict] = []
        self.binary_frames: list[bytes] = []
        # Real ASR service pushes ``ready`` the moment the session starts, so
        # it can land in the same TCP segment as the 101 handshake response.
        self.eager_ready = False
        self._stopping = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        try:
            self._sock.close()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while not self._stopping:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                conn.close()
                return
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        lines = head.split("\r\n")
        try:
            _method, path, _version = lines[0].split(" ", 2)
        except ValueError:
            conn.close()
            return
        self.request_paths.append(path)
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()

        if path.startswith("/health"):
            body = json.dumps({"ok": True, "engine": "fake-streaming", "sessions": 0}).encode()
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
                + body
            )
            conn.close()
            return

        key = headers.get("sec-websocket-key", "")
        if not key:
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            conn.close()
            return
        handshake = wsutil.ws_handshake_response(key).encode("utf-8")
        ready_sent = False
        if self.eager_ready:
            # One write: handshake head + the first data frame, exactly what an
            # eager aiohttp service does.
            ready = wsutil._encode_frame(
                json.dumps({"type": "ready", "engine": "fake-streaming"}).encode("utf-8"),
                wsutil.OP_TEXT,
            )
            conn.sendall(handshake + ready)
            ready_sent = True
        else:
            conn.sendall(handshake)
        try:
            while True:
                frame = wsutil.ws_recv_frame(conn, expect_masked=True)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == wsutil.OP_TEXT:
                    event = json.loads(payload.decode("utf-8"))
                    self.text_frames.append(event)
                    if event.get("type") == "config":
                        if not ready_sent:
                            wsutil.ws_send_text(conn, json.dumps({"type": "ready", "engine": "fake-streaming"}))
                            ready_sent = True
                        wsutil.ws_send_text(conn, json.dumps({"type": "config_ack", "sample_rate": 16000}))
                    elif event.get("type") == "flush":
                        wsutil.ws_send_text(conn, json.dumps({"type": "final", "seg_id": 1, "text": "你好"}))
                        wsutil.ws_send_text(conn, json.dumps({"type": "flush_ack"}))
                elif opcode == wsutil.OP_BINARY:
                    self.binary_frames.append(payload)
                    wsutil.ws_send_text(conn, json.dumps({"type": "partial", "seg_id": 1, "text": "你"}))
        except (OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture()
def server(tmp_path):
    runtime = Runtime(ModelRegistry(), ToolRegistry())
    with patch("runtime.server._MODELS_PATH", str(tmp_path / "models.json")), \
         patch("runtime.server._TOOLS_PATH", str(tmp_path / "tools.json")), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", str(tmp_path / "prompt_templates.json")), \
         patch("runtime.server._DATA_DIR", str(tmp_path)):
        srv = RuntimeHTTPServer(runtime)
        srv.start_background(host="127.0.0.1", port=0)
        yield srv
        srv.stop()


@pytest.fixture()
def fake_asr():
    service = FakeAsrService()
    service.start()
    yield service
    service.stop()


def _get_json(server: RuntimeHTTPServer, path: str) -> tuple[int, dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}{path}", timeout=10) as resp:
        return resp.status, json.loads(resp.read())


def _recv_until(client: WSClient, wanted: str, limit: int = 10) -> dict | None:
    """Read text frames until one has ``type == wanted`` (None if it never comes)."""
    client.set_timeout(5.0)
    for _ in range(limit):
        try:
            frame = client.recv()
        except socket.timeout:
            return None
        if frame is None:
            return None
        opcode, payload = frame
        if opcode != wsutil.OP_TEXT:
            continue
        event = json.loads(payload.decode("utf-8"))
        if event.get("type") == wanted:
            return event
    return None


# ------------------------------------------------------------------
# GET /v1/asr/health
# ------------------------------------------------------------------

def test_health_probe_reports_service_up(server, fake_asr, monkeypatch):
    monkeypatch.setenv("AGENTS_ASR_URL", fake_asr.base_url)
    status, body = _get_json(server, "/v1/asr/health")
    assert status == 200
    assert body["available"] is True
    assert body["configured"] is True
    assert body["engine"] == "fake-streaming"
    # The UI builds the socket URL from the page origin; the bridge path is
    # what it needs to know, not the service port.
    assert body["ws_path"] == "/v1/asr/ws"


def test_health_probe_reports_service_down(server, monkeypatch):
    # A port nothing listens on: still 200, with the reason.
    monkeypatch.setenv("AGENTS_ASR_URL", "http://127.0.0.1:1")
    status, body = _get_json(server, "/v1/asr/health")
    assert status == 200
    assert body["available"] is False
    assert body["configured"] is True     # configured, just not answering
    assert body["error"] == "asr_unreachable"
    assert "127.0.0.1:1" in body["message"]


def test_voice_input_is_off_without_agents_asr_url(server, monkeypatch):
    """No AGENTS_ASR_URL: voice input is disabled, and the UI hides the button.

    Nothing is dialed in that case — the answer is configuration, not a probe
    result, so it must not be reported as a service outage.
    """
    monkeypatch.delenv("AGENTS_ASR_URL", raising=False)
    status, body = _get_json(server, "/v1/asr/health")
    assert status == 200
    assert body["configured"] is False
    assert body["available"] is False
    assert body["error"] == "not_configured"
    assert body["asr_url"] == ""


def test_bridge_reports_voice_input_not_configured(server, monkeypatch):
    monkeypatch.delenv("AGENTS_ASR_URL", raising=False)
    client = WSClient.connect(f"ws://127.0.0.1:{server.port}/v1/asr/ws", timeout=5.0)
    try:
        event = _recv_until(client, "error")
        assert event is not None
        assert event["code"] == "not_configured"
    finally:
        client.close()


# ------------------------------------------------------------------
# GET /v1/asr/ws  (WebSocket bridge)
# ------------------------------------------------------------------

def test_bridge_relays_frames_both_ways(server, fake_asr, monkeypatch):
    monkeypatch.setenv("AGENTS_ASR_URL", fake_asr.base_url)
    client = WSClient.connect(f"ws://127.0.0.1:{server.port}/v1/asr/ws?sample_rate=16000", timeout=5.0)
    try:
        client.send_text(json.dumps({"type": "config", "sample_rate": 16000, "language": "zh"}))
        ready = _recv_until(client, "ready")
        assert ready == {"type": "ready", "engine": "fake-streaming"}

        # Binary audio up, transcript events down.  Bytes must survive intact:
        # this is the PCM16LE stream the engine decodes.
        pcm = bytes(range(256)) * 3
        client.send_binary(pcm)
        partial = _recv_until(client, "partial")
        assert partial["text"] == "你"
        assert fake_asr.binary_frames == [pcm]

        client.send_text(json.dumps({"type": "flush"}))
        final = _recv_until(client, "final")
        assert final["text"] == "你好"
        assert _recv_until(client, "flush_ack") is not None
    finally:
        client.close()

    assert fake_asr.text_frames[0]["type"] == "config"


def test_bridge_forwards_query_but_not_a_browser_token(server, fake_asr, monkeypatch):
    monkeypatch.setenv("AGENTS_ASR_URL", fake_asr.base_url)
    monkeypatch.setenv("AGENTS_ASR_TOKEN", "server-side-secret")
    url = (
        f"ws://127.0.0.1:{server.port}/v1/asr/ws"
        "?sample_rate=16000&session_id=abc&last_seg_id=7&token=evil&engine=whatever"
    )
    client = WSClient.connect(url, timeout=5.0)
    try:
        client.send_text(json.dumps({"type": "config", "sample_rate": 16000}))
        assert _recv_until(client, "ready") is not None
    finally:
        client.close()

    path = fake_asr.request_paths[-1]
    assert path.startswith("/asr/ws?")
    assert "sample_rate=16000" in path
    assert "session_id=abc" in path
    assert "last_seg_id=7" in path
    assert "token=server-side-secret" in path   # injected server-side
    assert "evil" not in path
    assert "engine" not in path


def test_bridge_reports_unreachable_service_as_error_frame(server, monkeypatch):
    monkeypatch.setenv("AGENTS_ASR_URL", "http://127.0.0.1:1")
    # The upgrade succeeds (so the browser gets a readable frame instead of an
    # opaque onerror), then the bridge reports why it cannot continue.
    client = WSClient.connect(f"ws://127.0.0.1:{server.port}/v1/asr/ws", timeout=5.0)
    try:
        event = _recv_until(client, "error")
        assert event is not None
        assert event["code"] == "asr_unreachable"
    finally:
        client.close()


def test_bridge_keeps_the_first_frame_that_arrives_with_the_handshake(server, fake_asr, monkeypatch):
    """The ASR service pushes ``ready`` immediately, so it can arrive in the
    same TCP segment as the 101 response.  Bytes read while looking for the
    handshake head must be handed to frame parsing, not dropped."""
    monkeypatch.setenv("AGENTS_ASR_URL", fake_asr.base_url)
    fake_asr.eager_ready = True
    client = WSClient.connect(f"ws://127.0.0.1:{server.port}/v1/asr/ws", timeout=5.0)
    try:
        client.set_timeout(5.0)
        frame = client.recv()
        assert frame is not None and frame[0] == wsutil.OP_TEXT
        assert json.loads(frame[1].decode("utf-8"))["type"] == "ready"
        # The socket is still usable afterwards (frame boundaries intact).
        client.send_text(json.dumps({"type": "config", "sample_rate": 16000}))
        assert _recv_until(client, "config_ack") is not None
    finally:
        client.close()


def test_ws_path_is_only_routed_as_an_upgrade(server, fake_asr, monkeypatch):
    # Like /v1/terminals/ws, the bridge exists only as a WebSocket upgrade; a
    # plain GET on that path is not routed (and must not reach the ASR service).
    monkeypatch.setenv("AGENTS_ASR_URL", fake_asr.base_url)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"http://127.0.0.1:{server.port}/v1/asr/ws", timeout=10)
    assert exc.value.code == 404
    assert fake_asr.request_paths == []


def test_service_url_may_carry_a_custom_ws_path(server, fake_asr, monkeypatch):
    # A reverse proxy in front of the ASR service is described by pointing the
    # URL at its WebSocket path; the HTTP probes stay at the root.
    monkeypatch.setenv("AGENTS_ASR_URL", f"{fake_asr.base_url}/asr/ws")
    client = WSClient.connect(f"ws://127.0.0.1:{server.port}/v1/asr/ws", timeout=5.0)
    try:
        client.send_text(json.dumps({"type": "config"}))
        assert _recv_until(client, "ready") is not None
    finally:
        client.close()
    assert fake_asr.request_paths[-1] == "/asr/ws"

    status, body = _get_json(server, "/v1/asr/health")
    assert status == 200 and body["available"] is True


# ------------------------------------------------------------------
# Close handshake (raw frames: WSClient / ws_recv_frame swallow close)
# ------------------------------------------------------------------

def _raw_masked_frame(payload: bytes, opcode: int) -> bytes:
    """One masked frame, the way a browser sends them."""
    key = os.urandom(4)
    n = len(payload)
    head = bytearray([0x80 | opcode])
    if n < 126:
        head.append(0x80 | n)
    elif n < 65536:
        head.append(0x80 | 126)
        head += struct.pack("!H", n)
    else:
        head.append(0x80 | 127)
        head += struct.pack("!Q", n)
    return bytes(head) + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def _recv_all(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed mid-frame")
        buf += chunk
    return buf


def _raw_ws_connect(server, path: str) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(request.encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            break
        head += chunk
    assert b" 101 " in head.split(b"\r\n", 1)[0], head
    return sock


def _raw_opcodes(sock, limit: int = 20) -> list[int]:
    """Opcodes of the frames the server sends back, until the socket is done."""
    sock.settimeout(3.0)
    seen: list[int] = []
    for _ in range(limit):
        try:
            header = _recv_all(sock, 2)
            length = header[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", _recv_all(sock, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", _recv_all(sock, 8))[0]
            if header[1] & 0x80:  # masked (servers do not mask; handled anyway)
                _recv_all(sock, 4)
            if length:
                _recv_all(sock, length)
        except (OSError, ConnectionError):
            break
        seen.append(header[0] & 0x0F)
    return seen


def test_client_close_is_answered_with_exactly_one_close_frame(server, fake_asr, monkeypatch):
    # A browser calling ws.close() used to get *two* close frames: the
    # best-effort reply inside ws_recv_frame plus the bridge's own teardown
    # close.  Chrome logs the second one as
    #   "WebSocket connection to 'wss://...' failed: Close received after close"
    # which looks exactly like a failed recording, so the bridge must answer
    # the close handshake with a single frame.
    monkeypatch.setenv("AGENTS_ASR_URL", fake_asr.base_url)
    sock = _raw_ws_connect(server, "/v1/asr/ws?sample_rate=16000")
    try:
        sock.sendall(_raw_masked_frame(json.dumps({"type": "flush"}).encode(), wsutil.OP_TEXT))
        sock.sendall(_raw_masked_frame(struct.pack("!H", 1000), wsutil.OP_CLOSE))
        opcodes = _raw_opcodes(sock)
    finally:
        sock.close()
    assert opcodes.count(wsutil.OP_CLOSE) == 1, opcodes
    assert opcodes[-1] == wsutil.OP_CLOSE, opcodes
