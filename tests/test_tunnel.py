"""Tests for the parent↔child WS reverse tunnel (Phase A: tunnel core).

Covers:

* protocol helpers (tunnel ids, frame encode/decode, chunking),
* wsutil frame primitives (round-trip over a socketpair, masked/unmasked,
  large payloads),
* end-to-end with two real RuntimeHTTPServer instances: explicit child
  registration → env record + online status, HTTP round-trips over the
  tunnel, terminal stream bridging, parent-side delete (child must stop
  dialing and never resurrect the record), child-side unregister, and the
  rejected-hello path (dialing without a record clears the child's flag).
"""

from __future__ import annotations

import contextlib
import base64
import json
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

import pytest

from runtime import wsutil
from runtime.tunnel_protocol import (
    CHUNK_SIZE,
    decode_frame,
    encode_frame,
    iter_chunks,
    is_tunnel_env_id,
    new_tunnel_id,
    tunnel_env_id,
    tunnel_id_from_env_id,
)
from runtime.server import RuntimeHTTPServer

TURN = time.time()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _request(server, method, path, payload=None, headers=None):
    url = f"http://127.0.0.1:{server.port}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except (ValueError, json.JSONDecodeError):
            return exc.code, {"raw": exc.read().decode("utf-8", errors="replace")}


def _wait_hello(server):
    for _ in range(50):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.port}/v1/setup?op=hello", timeout=1
            ) as resp:
                return json.loads(resp.read())
        except OSError:
            time.sleep(0.1)
    raise AssertionError("server never came up")


@contextlib.contextmanager
def _real_server(tmp_path, name="data", workspace=None):
    """Start a real RuntimeHTTPServer (builtins registered, isolated paths)."""
    data_dir = tmp_path / name
    data_dir.mkdir(exist_ok=True)
    if workspace:
        ws = workspace if isinstance(workspace, str) else str(workspace)
        os.makedirs(ws, exist_ok=True)
        (data_dir / "env.json").write_text(
            json.dumps({"AGENTS_WORKSPACE": ws}), encoding="utf-8"
        )
    with patch("runtime.server._MODELS_PATH", str(data_dir / "models.json")), \
         patch("runtime.server._TOOLS_PATH", str(data_dir / "tools.json")), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", str(data_dir / "prompt_templates.json")), \
         patch("runtime.server._DATA_DIR", str(data_dir)), \
         patch("runtime.server._ENV_PATH", str(data_dir / "env.json")), \
         patch("runtime.server._REMOTE_ENVS_PATH", str(data_dir / "remote_envs.json")), \
         patch("runtime.server._AUTH_PATH", str(data_dir / "auth_token.json")), \
         patch("runtime.server._AGENTS_DIR", str(data_dir / "agents")):
        srv = RuntimeHTTPServer()
        srv.start_background(host="127.0.0.1", port=0)
    try:
        _wait_hello(srv)
        yield srv, data_dir
    finally:
        srv.stop()


def _set_child_source(child_srv, child_data, parent_url: str) -> None:
    env_path = child_data / "env.json"
    try:
        env = json.loads(env_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        env = {}
    env["SETUP_SOURCE"] = parent_url
    env_path.write_text(json.dumps(env), encoding="utf-8")


def _child_status(child_srv) -> dict:
    status, body = _request(child_srv, "GET", "/v1/tunnel/parent/status")
    assert status == 200, body
    return body


def _wait_until(pred, timeout=15.0, interval=0.1, message="condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}; last={last!r}")


def _parent_tunnel_envs(parent_srv) -> list:
    status, body = _request(parent_srv, "GET", "/v1/remote-envs")
    assert status == 200, body
    return [e for e in body["envs"] if is_tunnel_env_id(e.get("id", ""))]


# ---------------------------------------------------------------------------
# Protocol unit tests
# ---------------------------------------------------------------------------

def test_tunnel_id_roundtrip():
    tid = new_tunnel_id()
    assert len(tid) == 16
    assert is_tunnel_env_id(tunnel_env_id(tid))
    assert tunnel_id_from_env_id(tunnel_env_id(tid)) == tid
    assert not is_tunnel_env_id("http://1.2.3.4:7988")
    assert tunnel_id_from_env_id("tunnel:zzzz") is None
    assert tunnel_id_from_env_id("tunnel:123456789012345") is None  # 15 chars


def test_frame_encode_decode():
    obj = {"op": "hello", "tunnel_id": "a" * 16, "snapshot": {"app_title": "母→子"}}
    data = encode_frame(obj)
    assert isinstance(data, bytes)
    assert decode_frame(data) == obj
    with pytest.raises(ValueError):
        decode_frame(b"[1,2,3]")
    with pytest.raises(ValueError):
        decode_frame(b"not json")


def test_iter_chunks():
    assert list(iter_chunks(b"")) == []
    body = os.urandom(CHUNK_SIZE * 2 + 1)
    chunks = list(iter_chunks(body))
    assert len(chunks) == 3
    assert b"".join(chunks) == body
    assert all(len(c) <= CHUNK_SIZE for c in chunks)


# ---------------------------------------------------------------------------
# wsutil frame primitives
# ---------------------------------------------------------------------------

def _ws_roundtrip(payload: bytes, opcode: int, mask: bool):
    a, b = socket.socketpair()
    try:
        wsutil.ws_send_frame(a, payload, opcode, mask=mask)
        got = wsutil.ws_recv_frame(b, expect_masked=mask)
        assert got is not None
        got_op, got_data = got
        assert got_op == opcode
        assert got_data == payload
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("mask", [False, True])
def test_wsutil_text_and_binary_roundtrip(mask):
    _ws_roundtrip("hello 隧道".encode("utf-8"), wsutil.OP_TEXT, mask)
    _ws_roundtrip(os.urandom(300), wsutil.OP_BINARY, mask)


def test_wsutil_large_payload():
    big = os.urandom(70_000)  # exercises the 64-bit length branch
    _ws_roundtrip(big, wsutil.OP_BINARY, True)
    _ws_roundtrip(big, wsutil.OP_BINARY, False)


def test_wsutil_close_and_ping():
    a, b = socket.socketpair()
    try:
        wsutil.ws_send_close(a)
        got = wsutil.ws_recv_frame(b)
        assert got is None  # close → None
        a2, b2 = socket.socketpair()
        wsutil.ws_send_ping(a2, b"hb")
        got = wsutil.ws_recv_frame(b2)
        assert got == (wsutil.OP_PING, b"hb")
    finally:
        for s in (a, b, a2, b2):
            s.close()


def test_wsutil_accept_key_deterministic():
    key = "dGhlIHNhbXBsZSBub25jZQ=="  # RFC 6455 example
    # RFC 6455 §1.3 example
    assert wsutil.ws_accept_key(key) == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


# ---------------------------------------------------------------------------
# End-to-end: parent + child real servers
# ---------------------------------------------------------------------------

def test_register_online_call_and_stream(tmp_path):
    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
        parent_srv, _ = parent
        child_srv, child_data = child
        _set_child_source(child_srv, child_data, f"http://127.0.0.1:{parent_srv.port}/")

        # 1. explicit registration (browser → child)
        status, body = _request(child_srv, "POST", "/v1/tunnel/parent/register")
        assert status == 200, body
        assert body["ok"] is True
        env_id = body["env_id"]
        assert env_id.startswith("tunnel:")

        # 2. record appears on the parent and goes online
        def _online():
            envs = _parent_tunnel_envs(parent_srv)
            env = next((e for e in envs if e["id"] == env_id), None)
            return env if env and env.get("online") is True else None
        env = _wait_until(_online, message="tunnel env online")
        assert env["transport"] == "ws-tunnel"
        assert env["tunnel_id"] == tunnel_id_from_env_id(env_id)
        assert env.get("backend_build"), "hello snapshot should be populated"

        # 3. HTTP round-trip over the tunnel
        manager = parent_srv._tunnel_manager
        status_code, headers, raw = manager.call_env(env_id, "GET", "/v1/tools")
        assert status_code == 200
        tools = json.loads(raw)
        tool_list = tools.get("tools") if isinstance(tools, dict) else tools
        assert any(
            (t.get("name") if isinstance(t, dict) else t) == "write_file"
            for t in tool_list
        )

        # 4. POST with a body over the tunnel
        status_code, _h, raw = manager.call_env(
            env_id, "POST", "/v1/env", headers={"Content-Type": "application/json"},
            body=json.dumps({"key": "TUNNEL_TEST", "value": "1"}).encode("utf-8"),
        )
        assert status_code == 200
        assert json.loads(raw)["env"].get("TUNNEL_TEST") == "1"

        # 5. terminal stream bridge
        tag = f"tun-{int(time.time() * 1000)}"
        stream = manager.open_stream_env(env_id, f"/v1/terminals/ws?terminal_id={tag}")
        marker = f"TUNNEL_ECHO_{tag}".encode("ascii")
        try:
            stream.send(b"echo " + marker + b"\r")
            buf = b""
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                item = stream.recv(timeout=2.0)
                if item:
                    _t, chunk = item
                    buf += chunk
                    if marker in buf:
                        break
            assert marker in buf, f"echo marker not seen; got {buf[-400:]!r}"
        finally:
            stream.close()

        # 6. parent-side delete: child must clear its flag, record must not
        #    resurrect on reconnect.
        status, _b = _request(parent_srv, "DELETE", f"/v1/remote-envs/{env_id}")
        assert status == 200
        _wait_until(lambda: _parent_tunnel_envs(parent_srv) == [],
                    message="parent env record removed")
        def _disabled():
            s = _child_status(child_srv)
            return s if not s["enabled"] else None
        st = _wait_until(_disabled, message="child cleared TUNNEL_ENABLED")
        assert st["state"] in {"idle", "unregistered"}
        # give the (stopped) client a few reconnect cycles to try to revive
        time.sleep(3)
        assert _parent_tunnel_envs(parent_srv) == []


def test_child_unregister(tmp_path):
    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
        parent_srv, _ = parent
        child_srv, child_data = child
        _set_child_source(child_srv, child_data, f"http://127.0.0.1:{parent_srv.port}/")

        status, body = _request(child_srv, "POST", "/v1/tunnel/parent/register")
        assert status == 200, body
        env_id = body["env_id"]
        def _online():
            envs = _parent_tunnel_envs(parent_srv)
            env = next((e for e in envs if e["id"] == env_id), None)
            return env if env and env.get("online") is True else None
        _wait_until(_online, message="tunnel env online")

        # child-initiated removal
        status, body = _request(child_srv, "POST", "/v1/tunnel/parent/unregister")
        assert status == 200, body
        assert body["ok"] is True
        _wait_until(lambda: _parent_tunnel_envs(parent_srv) == [],
                    message="record removed after child unregister")
        st = _child_status(child_srv)
        assert st["enabled"] is False
        time.sleep(2)
        assert _parent_tunnel_envs(parent_srv) == []


def test_rejected_hello_clears_child_flag(tmp_path):
    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
        parent_srv, _ = parent
        child_srv, child_data = child
        _set_child_source(child_srv, child_data, f"http://127.0.0.1:{parent_srv.port}/")

        # Enable the tunnel WITHOUT registering: the parent has no record for
        # this child, so the hello must be rejected and the child must clear
        # its flag (no resurrection of removed registrations).
        env_path = child_data / "env.json"
        env = json.loads(env_path.read_text(encoding="utf-8"))
        env["TUNNEL_ENABLED"] = "1"
        env_path.write_text(json.dumps(env), encoding="utf-8")

        def _unregistered():
            s = _child_status(child_srv)
            return s if s["state"] == "unregistered" else None
        st = _wait_until(_unregistered, timeout=20,
                         message="child observed not-registered rejection")
        assert st["enabled"] is False
        assert _parent_tunnel_envs(parent_srv) == []


def test_register_requires_setup_source(tmp_path):
    with _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
        child_srv, child_data = child
        # no SETUP_SOURCE configured
        status, body = _request(child_srv, "POST", "/v1/tunnel/parent/register")
        assert status == 400
        assert body.get("error")
        st = _child_status(child_srv)
        assert st["configured"] is False
        assert st["enabled"] is False


def test_register_with_parent_address_persists_setup_source(tmp_path):
    """POST /v1/tunnel/parent/register with {"parent": url} (the UI's 隧道连接
    box): the address is validated, persisted as SETUP_SOURCE, and used."""
    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
        parent_srv, _ = parent
        child_srv, child_data = child

        # 1. child has no SETUP_SOURCE yet; register with an explicit address
        addr = f"http://127.0.0.1:{parent_srv.port}/v1/setup"
        status, body = _request(
            child_srv, "POST", "/v1/tunnel/parent/register",
            payload={"parent": addr},
        )
        assert status == 200, body
        assert body["ok"] is True
        env_id = body["env_id"]

        # 2. the address is persisted as SETUP_SOURCE and reflected in status
        env = json.loads((child_data / "env.json").read_text(encoding="utf-8"))
        assert env["SETUP_SOURCE"] == addr
        st = _child_status(child_srv)
        assert st["configured"] is True
        assert st["parent"] == f"http://127.0.0.1:{parent_srv.port}"

        # 3. the child comes online on the parent using that address
        def _online():
            envs = _parent_tunnel_envs(parent_srv)
            env = next((e for e in envs if e["id"] == env_id), None)
            return env if env and env.get("online") is True else None
        _wait_until(_online, message="tunnel env online after address register")

        # 4. an invalid address is rejected and never persisted
        status, body = _request(
            child_srv, "POST", "/v1/tunnel/parent/register",
            payload={"parent": "not-a-url"},
        )
        assert status == 400
        assert body.get("error")
        env = json.loads((child_data / "env.json").read_text(encoding="utf-8"))
        assert env["SETUP_SOURCE"] == addr

        # 5. an empty parent falls back to the persisted SETUP_SOURCE
        status, body = _request(
            child_srv, "POST", "/v1/tunnel/parent/register", payload={},
        )
        assert status == 200, body
        assert body["ok"] is True
        assert body["env_id"] == env_id


# ---------------------------------------------------------------------------
# Browser bridge: /v1/tunnel-proxy/{env_id}/... (HTTP + terminal WS)
# ---------------------------------------------------------------------------

class _WsBrowserSock:
    """Browser-style WebSocket client socket (masked frames), with a pending
    buffer for bytes that arrived glued to the handshake response."""

    def __init__(self, sock, pending=b""):
        self.sock = sock
        self.pending = pending

    def recv(self, n=4096, flags=0):
        if self.pending:
            data, self.pending = self.pending[:n], self.pending[n:]
            return data
        return self.sock.recv(n, flags)

    def sendall(self, data):
        self.sock.sendall(data)

    def settimeout(self, t):
        self.sock.settimeout(t)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _ws_browser_connect(port: int, path: str) -> _WsBrowserSock:
    """Do the browser-side WebSocket handshake and return a ready socket."""
    raw = socket.create_connection(("127.0.0.1", port), timeout=15)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    raw.sendall(req.encode("ascii"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = raw.recv(4096)
        if not chunk:
            raw.close()
            raise AssertionError("connection closed during WS handshake")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    if " 101 " not in status_line:
        raw.close()
        raise AssertionError(f"WS handshake failed: {status_line!r}")
    raw.settimeout(15)
    return _WsBrowserSock(raw, rest)


def test_tunnel_proxy_browser_bridge(tmp_path):
    """The parent's browser bridge: same-origin HTTP proxy + terminal WS into
    a registered child (what the UI uses in tunnel mode)."""
    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
        parent_srv, _ = parent
        child_srv, child_data = child
        (tmp_path / "child_ws" / "bridge_file.txt").write_text("bridge ok")
        _set_child_source(child_srv, child_data, f"http://127.0.0.1:{parent_srv.port}/")
        status, body = _request(child_srv, "POST", "/v1/tunnel/parent/register")
        assert status == 200, body
        env_id = body["env_id"]
        base = f"http://127.0.0.1:{parent_srv.port}/v1/tunnel-proxy/{env_id}"

        def _online():
            e = next((e for e in _parent_tunnel_envs(parent_srv) if e["id"] == env_id), None)
            return e if e and e.get("online") is True else None
        _wait_until(_online, message="tunnel env online")

        # 1. workspace list / content / mkdir through the same-origin bridge
        with urllib.request.urlopen(
            f"{base}/v1/workspace/list?path=.&page=1&page_size=50&restrict=0", timeout=30,
        ) as resp:
            data = json.loads(resp.read())
        names = [e.get("name") for e in data.get("entries", data.get("files", []))]
        assert "bridge_file.txt" in names, names
        with urllib.request.urlopen(
            f"{base}/v1/workspace/content?path=bridge_file.txt&restrict=0", timeout=30,
        ) as resp:
            assert resp.read() == b"bridge ok"
        req = urllib.request.Request(
            f"{base}/v1/workspace/mkdir",
            data=json.dumps({"parent_path": ".", "name": "bridge_dir"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            assert resp.status == 200
        assert (tmp_path / "child_ws" / "bridge_dir").is_dir()

        # 2. error paths: unknown tunnel env -> 404, non-tunnel id -> 400
        for bad, expected in (("tunnel:0000000000000000", 404), ("env-x", 400)):
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{parent_srv.port}/v1/tunnel-proxy/{bad}/v1/env",
                    timeout=10)
                raise AssertionError("expected HTTP error")
            except urllib.error.HTTPError as exc:
                assert exc.code == expected, (bad, exc.code)

        # 3. offline child -> 502, then back online
        tc = child_srv._server.tunnel_client
        tc.stop()
        def _offline():
            e = next((e for e in _parent_tunnel_envs(parent_srv) if e["id"] == env_id), None)
            return e if e and not e.get("online") else None
        _wait_until(_offline, message="parent marked env offline")
        try:
            urllib.request.urlopen(f"{base}/v1/env", timeout=10)
            raise AssertionError("expected HTTP error")
        except urllib.error.HTTPError as exc:
            assert exc.code == 502, exc.code
        tc.start()
        _wait_until(_online, message="tunnel env online again")

        # 4. terminal WebSocket bridge: browser WS -> parent -> tunnel -> child PTY
        tag = f"pb-{int(time.time() * 1000)}"
        marker = f"BRIDGE_ECHO_{tag}".encode("ascii")
        ws = _ws_browser_connect(
            parent_srv.port, f"{base}/v1/terminals/ws?terminal_id={tag}&cols=100&rows=30")
        try:
            buf = b""
            deadline = time.monotonic() + 30
            while marker not in buf and time.monotonic() < deadline:
                try:
                    frame = wsutil.ws_recv_frame(ws, expect_masked=False)
                except socket.timeout:
                    break
                if frame is None:
                    break
                buf += frame[1]
                if b"__terminal_id" in buf:
                    wsutil.ws_send_frame(ws, b"echo " + marker + b"\r", wsutil.OP_TEXT, mask=True)
            assert marker in buf, f"echo marker not seen; got {buf[-400:]!r}"
        finally:
            try:
                wsutil.ws_send_close(ws, 1000, mask=True)
            except OSError:
                pass
            ws.close()


def test_tunnel_proxy_forwards_bodyless_delete(tmp_path):
    """Regression: a bridged DELETE with no body must reach the child.

    ``_handle_tunnel_proxy`` used to demand ``Content-Length > 0`` for every
    non-GET/HEAD method, so a bodyless ``DELETE /v1/terminals/{id}`` was
    answered with 400 by the *parent* and never forwarded. Destroying a
    terminal in tunnel mode therefore left the child's PTY session alive
    (a dead session the UI could not get rid of).
    """
    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
        parent_srv, _ = parent
        child_srv, child_data = child
        _set_child_source(child_srv, child_data, f"http://127.0.0.1:{parent_srv.port}/")
        status, body = _request(child_srv, "POST", "/v1/tunnel/parent/register")
        assert status == 200, body
        env_id = body["env_id"]
        base = f"http://127.0.0.1:{parent_srv.port}/v1/tunnel-proxy/{env_id}"

        def _online():
            e = next((e for e in _parent_tunnel_envs(parent_srv) if e["id"] == env_id), None)
            return e if e and e.get("online") is True else None
        _wait_until(_online, message="tunnel env online")

        # Create a real PTY session on the child through the terminal WS bridge.
        tag = f"del-{int(time.time() * 1000)}"
        ws = _ws_browser_connect(
            parent_srv.port, f"{base}/v1/terminals/ws?terminal_id={tag}&cols=80&rows=24")
        try:
            def _child_has_terminal():
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{child_srv.port}/v1/terminals", timeout=10) as resp:
                    data = json.loads(resp.read())
                return next((t for t in data.get("terminals", []) if t["session_id"] == tag), None)
            _wait_until(_child_has_terminal, message="child terminal registered")
        finally:
            try:
                wsutil.ws_send_close(ws, 1000, mask=True)
            except OSError:
                pass
            ws.close()

        # Bodyless DELETE through the bridge: must succeed and take effect.
        req = urllib.request.Request(f"{base}/v1/terminals/{tag}", method="DELETE")
        with urllib.request.urlopen(req, timeout=30) as resp:
            assert resp.status == 200, resp.status
            payload = json.loads(resp.read())
        assert payload.get("status") == "deleted", payload

        # The child's PTY session is really gone (no stale dead session left).
        def _child_terminal_gone():
            with urllib.request.urlopen(
                f"http://127.0.0.1:{child_srv.port}/v1/terminals", timeout=10) as resp:
                data = json.loads(resp.read())
            return not any(t["session_id"] == tag for t in data.get("terminals", []))
        _wait_until(_child_terminal_gone, message="child terminal removed")

        # A second bodyless DELETE now yields the child's own 404 (proving the
        # request reached the child), not a parent-side 400 empty-body error.
        try:
            req = urllib.request.Request(f"{base}/v1/terminals/{tag}", method="DELETE")
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("expected HTTP error")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404, exc.code


def test_tunnel_proxy_bridge_with_child_auth(tmp_path):
    """Browser bridge into a child with authorization enabled: no child-side
    token is configured on the parent — the child self-authorizes requests
    arriving over the tunnel (its own session cookie for HTTP, a fresh setup
    token for the terminal WS), so the bridge works out of the box.  Direct
    unauthenticated calls to the child must still 401."""
    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
        parent_srv, _ = parent
        child_srv, child_data = child
        (tmp_path / "child_ws" / "secret.txt").write_text("top secret")
        _set_child_source(child_srv, child_data, f"http://127.0.0.1:{parent_srv.port}/")

        # Enable child auth -> fresh API key
        status, body = _request(child_srv, "POST", "/v1/auth/config", {"password": "childpass1"})
        assert status == 200, body
        api_key = body.get("api_key")
        assert api_key, body

        # auth is now enabled on the child: register with the API key
        status, body = _request(
            child_srv, "POST", "/v1/tunnel/parent/register",
            headers={"Authorization": f"Bearer {api_key}"})
        assert status == 200, body
        env_id = body["env_id"]
        base = f"http://127.0.0.1:{parent_srv.port}/v1/tunnel-proxy/{env_id}"
        def _online():
            e = next((e for e in _parent_tunnel_envs(parent_srv) if e["id"] == env_id), None)
            return e if e and e.get("online") is True else None
        _wait_until(_online, message="tunnel env online")

        # No token is configured anywhere: the child's own API still rejects
        # direct unauthenticated calls (self-auth only covers the tunnel path).
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{child_srv.port}/v1/workspace/list?path=.&restrict=0",
                timeout=10)
            raise AssertionError("expected 401 for direct unauthenticated call")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401, exc.code

        # The bridge works immediately: list + content + POST
        with urllib.request.urlopen(
            f"{base}/v1/workspace/list?path=.&page=1&page_size=50&restrict=0", timeout=30,
        ) as resp:
            data = json.loads(resp.read())
        names = [e.get("name") for e in data.get("entries", data.get("files", []))]
        assert "secret.txt" in names, names
        with urllib.request.urlopen(
            f"{base}/v1/workspace/content?path=secret.txt&restrict=0", timeout=30,
        ) as resp:
            assert resp.read() == b"top secret"
        req = urllib.request.Request(
            f"{base}/v1/workspace/mkdir",
            data=json.dumps({"parent_path": ".", "name": "auth_dir"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            assert resp.status == 200
        assert (tmp_path / "child_ws" / "auth_dir").is_dir()

        # Terminal WS bridge: the child self-authenticates the local handshake
        tag = f"pa-{int(time.time() * 1000)}"
        marker = f"AUTH_ECHO_{tag}".encode("ascii")
        ws = _ws_browser_connect(
            parent_srv.port, f"{base}/v1/terminals/ws?terminal_id={tag}&cols=100&rows=30")
        try:
            buf = b""
            deadline = time.monotonic() + 30
            while marker not in buf and time.monotonic() < deadline:
                try:
                    frame = wsutil.ws_recv_frame(ws, expect_masked=False)
                except socket.timeout:
                    break
                if frame is None:
                    break
                buf += frame[1]
                if b"__terminal_id" in buf:
                    wsutil.ws_send_frame(ws, b"echo " + marker + b"\r", wsutil.OP_TEXT, mask=True)
            assert marker in buf, f"echo marker not seen; got {buf[-400:]!r}"
        finally:
            try:
                wsutil.ws_send_close(ws, 1000, mask=True)
            except OSError:
                pass
            ws.close()


def test_tunnel_proxy_bridge_percent_encoded_env_id(tmp_path):
    """Regression: the browser builds bridge URLs with encodeURIComponent
    (``tunnel%3A...``), so the raw request path carries a percent-encoded env
    id.  The parent must strip the *encoded* prefix when rebuilding the child
    path — otherwise the child receives ``GET /`` and answers 200 + the static
    index.html (text/html), and the frontend's ``res.json()`` yields null
    ("Cannot read properties of null (reading 'tools')").

    Covers both the HTTP bridge and the terminal WS bridge (the WS shortcut
    used to hand the still-encoded id to the handler and 400 with
    "Not a tunnel environment").
    """
    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
        parent_srv, _ = parent
        child_srv, child_data = child
        (tmp_path / "child_ws" / "enc.txt").write_text("encoded")
        _set_child_source(child_srv, child_data, f"http://127.0.0.1:{parent_srv.port}/")
        status, body = _request(child_srv, "POST", "/v1/tunnel/parent/register")
        assert status == 200, body
        env_id = body["env_id"]
        enc_id = urllib.parse.quote(env_id, safe="")
        assert enc_id != env_id  # the "tunnel:" colon is what the browser encodes
        base = f"http://127.0.0.1:{parent_srv.port}/v1/tunnel-proxy/{enc_id}"

        def _online():
            e = next((e for e in _parent_tunnel_envs(parent_srv) if e["id"] == env_id), None)
            return e if e and e.get("online") is True else None
        _wait_until(_online, message="tunnel env online")

        # HTTP bridge: /v1/tools must return real JSON, not the SPA index.html.
        with urllib.request.urlopen(f"{base}/v1/tools", timeout=30) as resp:
            assert resp.status == 200
            tools = json.loads(resp.read())
        assert tools.get("tools"), tools

        with urllib.request.urlopen(
            f"{base}/v1/workspace/list?path=.&page=1&page_size=50&restrict=0", timeout=30,
        ) as resp:
            data = json.loads(resp.read())
        names = [e.get("name") for e in data.get("entries", data.get("files", []))]
        assert "enc.txt" in names, names

        # Terminal WS bridge with the encoded env id must upgrade (previously 400).
        tag = f"enc-{int(time.time() * 1000)}"
        marker = f"ENC_ECHO_{tag}".encode("ascii")
        ws = _ws_browser_connect(
            parent_srv.port, f"{base}/v1/terminals/ws?terminal_id={tag}&cols=100&rows=30")
        try:
            buf = b""
            deadline = time.monotonic() + 30
            while marker not in buf and time.monotonic() < deadline:
                try:
                    frame = wsutil.ws_recv_frame(ws, expect_masked=False)
                except socket.timeout:
                    break
                if frame is None:
                    break
                buf += frame[1]
                if b"__terminal_id" in buf:
                    wsutil.ws_send_frame(ws, b"echo " + marker + b"\r", wsutil.OP_TEXT, mask=True)
            assert marker in buf, f"echo marker not seen; got {buf[-400:]!r}"
        finally:
            try:
                wsutil.ws_send_close(ws, 1000, mask=True)
            except OSError:
                pass
            ws.close()


def test_tunnel_envs_hello_endpoint(tmp_path):
    """POST /v1/remote-envs/{id}/hello refreshes the snapshot of a tunnel
    env over the reverse tunnel (and 404s unknown ids)."""
    with _real_server(tmp_path, "parent_data") as parent,          _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
        parent_srv, _ = parent
        child_srv, child_data = child
        _set_child_source(child_srv, child_data, f"http://127.0.0.1:{parent_srv.port}/")
        status, body = _request(child_srv, "POST", "/v1/tunnel/parent/register")
        assert status == 200, body
        env_id = body["env_id"]
        def _online():
            e = next((e for e in _parent_tunnel_envs(parent_srv) if e["id"] == env_id), None)
            return e if e and e.get("online") is True else None
        _wait_until(_online, message="tunnel env online")

        status, body = _request(parent_srv, "POST", f"/v1/remote-envs/{env_id}/hello")
        assert status == 200, body
        snap = body["snapshot"]
        assert snap.get("backend_build") or snap.get("frontend_build"), snap
        # snapshot was persisted on the record
        rec = next(e for e in _parent_tunnel_envs(parent_srv) if e["id"] == env_id)
        assert rec.get("backend_build") == snap.get("backend_build")

        # unknown tunnel id -> 404; offline env -> 502
        status, _b = _request(parent_srv, "POST", "/v1/remote-envs/tunnel:0000000000000000/hello")
        assert status == 404
        child_srv._server.tunnel_client.stop()
        def _offline():
            e = next((e for e in _parent_tunnel_envs(parent_srv) if e["id"] == env_id), None)
            return e if e and not e.get("online") else None
        _wait_until(_offline, message="parent marked env offline")
        status, body = _request(parent_srv, "POST", f"/v1/remote-envs/{env_id}/hello")
        assert status == 502, body


# ---------------------------------------------------------------------------
# Frame protocol robustness (atomic request sequences, control frames)
# ---------------------------------------------------------------------------

class _FakeSendSock:
    """Records sendall() calls; slow enough to expose lock behavior."""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.events = []
        self._lock = threading.Lock()

    def sendall(self, data: bytes) -> None:
        with self._lock:
            if self.delay:
                time.sleep(self.delay)
            self.events.append(time.monotonic())


def test_send_request_sequence_is_atomic(tmp_path):
    """Two concurrent tunnel calls must not interleave header/body frames:
    the second request's frames may only go out after the first request's
    complete sequence (header + body + terminator) finished."""
    from runtime.tunnel_manager import _TunnelConn

    sock = _FakeSendSock(delay=0.01)
    conn = _TunnelConn(sock, "testtid")
    body = os.urandom(3000)  # one 256KB-or-less chunk + terminator

    def _send_a():
        conn.send_request("GET", "/a", {}, body, "rid-a")

    def _send_b():
        time.sleep(0.02)  # start while A is mid-flight
        conn.send_request("GET", "/b", {}, None, "rid-b")

    ta = threading.Thread(target=_send_a)
    tb = threading.Thread(target=_send_b)
    ta.start(); tb.start()
    ta.join(10); tb.join(10)
    # A sent header + body chunk + terminator (3 frames); B sent header +
    # terminator (2 frames). Atomicity: B's first frame went out only after
    # A's last frame.
    assert len(sock.events) == 5, sock.events
    assert sock.events[3] >= sock.events[2] - 1e-6, (
        "request B's frames interleaved into request A's sequence "
        "(would corrupt the child's inline body read)"
    )


def test_send_frames_activity_refresh(tmp_path):
    """refresh_activity keeps last_frame_at fresh during long body sends
    (the sweeper must not stale-kill an in-flight upload), while plain
    control sends must NOT refresh it (a silent peer must still be reaped)."""
    from runtime.tunnel_manager import _TunnelConn
    from runtime import wsutil

    conn = _TunnelConn(_FakeSendSock(), "testtid")
    conn.last_frame_at = 0.0
    conn.send_frames([(wsutil.OP_BINARY, b"x")])  # default: no refresh
    assert conn.last_frame_at == 0.0
    conn.send_frames([(wsutil.OP_BINARY, b"y")], refresh_activity=True)
    assert conn.last_frame_at > 0.0


def test_try_ping_does_not_block_on_inflight_send(tmp_path):
    """The sweeper's ping must not stall behind a long body upload (single
    sweeper thread serves all connections)."""
    from runtime.tunnel_manager import _TunnelConn
    from runtime import wsutil

    sock = _FakeSendSock()
    conn = _TunnelConn(sock, "testtid")
    release = threading.Event()
    orig_sendall = sock.sendall

    def slow_sendall(data):
        release.wait(timeout=5)
        orig_sendall(data)

    sock.sendall = slow_sendall
    started = threading.Event()

    def _long_send():
        started.set()
        conn.send_frames(
            [(wsutil.OP_TEXT, b"header"), (wsutil.OP_BINARY, b"body")],
            refresh_activity=True,
        )

    t = threading.Thread(target=_long_send)
    t.start()
    assert started.wait(5)
    time.sleep(0.05)  # let the long send take the lock and enter sendall
    assert conn.try_ping(timeout=0.1) is False
    release.set()
    t.join(10)


class _FakeTunnelWS:
    """Minimal WSClient stand-in for child-side body-read unit tests."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.pongs = []

    def recv(self):
        if not self.frames:
            return None
        return self.frames.pop(0)

    def send_pong(self, payload=b""):
        self.pongs.append(payload)


def test_child_body_read_tolerates_pings_and_pongs():
    """A parent liveness ping arriving mid-body must be answered with a pong
    and the body read must continue (a multi-minute transfer must not be
    killed by the 30s ping cadence)."""
    from runtime.tunnel_client import TunnelClient, _BodyReadFailed
    from runtime import wsutil

    client = TunnelClient.__new__(TunnelClient)  # bypass __init__ (needs server)
    client._send_lock = threading.Lock()

    ws = _FakeTunnelWS([
        (wsutil.OP_BINARY, b"abc"),
        (wsutil.OP_PING, b"hb"),
        (wsutil.OP_PONG, b""),
        (wsutil.OP_BINARY, b"de"),
        (wsutil.OP_BINARY, b""),  # terminator
    ])
    body = client._read_req_body(ws)
    assert body == b"abcde"
    assert ws.pongs == [b"hb"]

    # Any other non-binary frame mid-body is still a protocol error.
    ws2 = _FakeTunnelWS([
        (wsutil.OP_BINARY, b"a"),
        (wsutil.OP_TEXT, b'{"op":"req","id":"x"}'),
    ])
    with pytest.raises(_BodyReadFailed):
        client._read_req_body(ws2)


def test_concurrent_tunnel_calls_do_not_interleave(tmp_path):
    """E2E: several concurrent calls on the same tunnel all complete with
    intact responses (request sends are atomic; the child worker pool
    processes them concurrently)."""
    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
        parent_srv, _ = parent
        child_srv, child_data = child
        _set_child_source(child_srv, child_data, f"http://127.0.0.1:{parent_srv.port}/")
        status, body = _request(child_srv, "POST", "/v1/tunnel/parent/register")
        assert status == 200, body
        env_id = body["env_id"]
        def _online():
            env = next((e for e in _parent_tunnel_envs(parent_srv) if e["id"] == env_id), None)
            return env if env and env.get("online") is True else None
        _wait_until(_online, message="tunnel env online")

        manager = parent_srv._tunnel_manager
        results = []
        errors = []

        def _one(i):
            try:
                code, _h, raw = manager.call_env(
                    env_id, "GET", f"/v1/tools?i={i}", timeout=60,
                )
                assert code == 200, (i, code)
                data = json.loads(raw)
                assert isinstance(data, dict) and "tools" in data
                results.append(i)
            except Exception as exc:
                errors.append((i, exc))

        threads = [threading.Thread(target=_one, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(90)
        assert not errors, errors
        assert sorted(results) == [0, 1, 2, 3, 4, 5]


def test_child_worker_pool_runs_concurrently(tmp_path):
    """While a slow local call (sleep 3) is in flight on the child, a
    concurrent fast call must not wait for it (worker pool, not a serial
    worker)."""
    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
        parent_srv, _ = parent
        child_srv, child_data = child
        _set_child_source(child_srv, child_data, f"http://127.0.0.1:{parent_srv.port}/")
        status, body = _request(child_srv, "POST", "/v1/tunnel/parent/register")
        assert status == 200, body
        env_id = body["env_id"]
        def _online():
            env = next((e for e in _parent_tunnel_envs(parent_srv) if e["id"] == env_id), None)
            return env if env and env.get("online") is True else None
        _wait_until(_online, message="tunnel env online")

        manager = parent_srv._tunnel_manager

        def _call_tool(cmd):
            code, _h, raw = manager.call_env(
                env_id, "POST", "/v1/tools/call",
                {"Content-Type": "application/json"},
                json.dumps({"tool_id": "exec_shell",
                            "arguments": {"command": cmd}}).encode("utf-8"),
                timeout=60,
            )
            return code, raw

        slow = {}

        def _slow():
            code, raw = _call_tool("sleep 3")
            slow["code"] = code
            slow["raw"] = raw

        t = threading.Thread(target=_slow)
        t.start()
        time.sleep(1.2)  # let the slow call actually start on the child
        t0 = time.monotonic()
        code, raw = _call_tool("echo fast-ok")
        elapsed = time.monotonic() - t0
        assert code == 200, raw
        assert elapsed < 2.5, f"fast call waited for the slow one: {elapsed:.1f}s"
        t.join(30)
        assert slow.get("code") == 200, slow.get("raw")
        assert b"sleep 3" in slow.get("raw", b"") or slow["code"] == 200
