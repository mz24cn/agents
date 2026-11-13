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
import hashlib
import http.client
import json
import os
import socket
import ssl
import struct
import threading
import types
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
from runtime.env_manager import local_tz_offset_minutes

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
        # The hello frame carries the zone those stamps were formatted in, so a
        # child in another zone is not read hours off by the parent (see
        # snapshot_from_hello): the tunnel needs the field exactly like op=hello.
        assert env.get("tz_offset") == local_tz_offset_minutes()

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

        # 5. terminal stream bridge (multiplexed: one socket, one channel)
        tag = f"tun-{int(time.time() * 1000)}"
        stream = manager.open_stream_env(env_id, "/v1/terminals/ws")
        marker = f"TUNNEL_ECHO_{tag}".encode("ascii")
        try:
            stream.send(json.dumps(
                {"op": "open", "c": tag, "cols": 80, "rows": 24},
            ).encode("utf-8"))
            stream.send(json.dumps({
                "op": "input", "c": tag, "d": "echo " + marker.decode("ascii") + "\r",
            }).encode("utf-8"))
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
# Browser bridge: /v1/env-proxy/{env_id}/... (HTTP + terminal WS)
# ---------------------------------------------------------------------------

def test_tunnel_register_with_password_in_parent_url(tmp_path):
    """母环境地址写成 ``http://账号:母环境密码@host:port/``（不带 ``?token=``）。

    这种写法的凭据既可能是环境前面那道 Basic 认证网关的，也可能是母环境自身的
    访问密码。两者都换算成 ``Authorization: Basic`` 送出（WS 握手与 register /
    unregister 都带上）；userinfo 不能留在 host 上（http.client 会把整串当主机名
    解析），也不能进母环境的记录 id。
    """
    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
        parent_srv, _ = parent
        child_srv, child_data = child

        # 母环境开启访问密码：没有凭据的反向连接会被 401 拒掉
        status, config = _request(parent_srv, "POST", "/v1/auth/config", {"password": "parent-pass"})
        assert status == 200, config
        assert config.get("setup_token")
        parent_api_key = config.get("api_key")
        assert parent_api_key, config

        addr = f"http://gateway:parent-pass@127.0.0.1:{parent_srv.port}/"
        status, body = _request(child_srv, "POST", "/v1/tunnel/parent/register", {"parent": addr})
        assert status == 200, body
        env_id = body["env_id"]

        # 原样落盘（不改写），凭据留在 SETUP_SOURCE 里
        env = json.loads((child_data / "env.json").read_text(encoding="utf-8"))
        assert env["SETUP_SOURCE"] == addr

        def _online():
            status, listing = _request(
                parent_srv, "GET", "/v1/remote-envs",
                headers={"Authorization": f"Bearer {parent_api_key}"},
            )
            assert status == 200, listing
            entry = next((e for e in listing["envs"] if e.get("id") == env_id), None)
            return entry if entry and entry.get("online") is True else None

        entry = _wait_until(_online, message="tunnel online authorized by the URL credential")
        # 密码不落母环境的记录
        assert "parent-pass" not in json.dumps(entry)

        # 隧道另一侧照常可调用（母环境反手请求子端 hello）
        status, hello = _request(
            parent_srv, "POST", f"/v1/remote-envs/{urllib.parse.quote(env_id, safe='')}/hello",
            headers={"Authorization": f"Bearer {parent_api_key}"},
        )
        assert status == 200, hello
        assert hello.get("ok") is True, hello

        status, body = _request(child_srv, "POST", "/v1/tunnel/parent/unregister")
        assert status == 200, body
        assert body.get("ok") is True, body


def test_tunnel_register_with_password_and_token_in_parent_url(tmp_path):
    """母环境地址同时带 ``账号:口令@`` 和 ``?token=``（前面有网关、母环境又开了授权）。

    一个请求只有一个 ``Authorization``：URL 里的凭据占它。这里它故意不是母环境的
    口令，所以 register / unregister 能不能成，全看 token 有没有跟着走
    ``X-Agent-Service-Token``（WS 拨号是 GET，token 仍在查询参数里）。
    """
    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
        parent_srv, _ = parent
        child_srv, child_data = child

        status, config = _request(parent_srv, "POST", "/v1/auth/config", {"password": "parent-pass"})
        assert status == 200, config
        setup_token = config.get("setup_token")
        assert setup_token, config
        parent_api_key = config.get("api_key")
        assert parent_api_key, config

        addr = (f"http://gateway:gateway-secret@127.0.0.1:{parent_srv.port}"
                f"/v1/setup?token={setup_token}")
        status, body = _request(child_srv, "POST", "/v1/tunnel/parent/register", {"parent": addr})
        assert status == 200, body
        env_id = body["env_id"]

        env = json.loads((child_data / "env.json").read_text(encoding="utf-8"))
        assert env["SETUP_SOURCE"] == addr

        def _online():
            status, listing = _request(
                parent_srv, "GET", "/v1/remote-envs",
                headers={"Authorization": f"Bearer {parent_api_key}"},
            )
            assert status == 200, listing
            entry = next((e for e in listing["envs"] if e.get("id") == env_id), None)
            return entry if entry and entry.get("online") is True else None

        entry = _wait_until(_online, message="tunnel online with credential + token")
        # id 与记录里都不该出现口令或 token
        assert "gateway-secret" not in json.dumps(entry)

        status, body = _request(child_srv, "POST", "/v1/tunnel/parent/unregister")
        assert status == 200, body
        assert body.get("ok") is True, body


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


def _ws_channel_echo(ws, channel: str, command: bytes, marker: bytes,
                     timeout: float = 30.0, env_id: str | None = None) -> bytes:
    """Open a terminal channel on a multiplexed socket, type ``command`` into
    it, and collect output frames until ``marker`` shows up (or time runs
    out). Returns everything that came back, marker included.

    ``env_id`` routes the channel to a child environment (the terminal socket
    is the same one the tab's local terminals use)."""
    open_frame = {"op": "open", "c": channel, "cols": 100, "rows": 30}
    if env_id:
        open_frame["env_id"] = env_id
    wsutil.ws_send_frame(ws, json.dumps(open_frame).encode("utf-8"),
                         wsutil.OP_TEXT, mask=True)
    buf = b""
    opened = False
    deadline = time.monotonic() + timeout
    while marker not in buf and time.monotonic() < deadline:
        try:
            frame = wsutil.ws_recv_frame(ws, expect_masked=False)
        except socket.timeout:
            break
        if frame is None:
            break
        if not opened:
            try:
                control = json.loads(frame[1].decode("utf-8", "ignore"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                control = {}
            if control.get("op") == "opened" and control.get("c") == channel:
                opened = True
                wsutil.ws_send_frame(ws, json.dumps(
                    {"op": "input", "c": channel, "d": command.decode("ascii")},
                ).encode("utf-8"), wsutil.OP_TEXT, mask=True)
                continue
        buf += frame[1]
    return buf


def test_env_proxy_browser_bridge(tmp_path):
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
        base = f"http://127.0.0.1:{parent_srv.port}/v1/env-proxy/{env_id}"

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

        # 2. error paths: an id with no remote-env record -> 404 (the bridge
        # resolves ids against the registry, so it can never dial an
        # arbitrary host; direct envs are bridged too, see test_remote_envs)
        for bad in ("tunnel:0000000000000000", "env-x"):
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{parent_srv.port}/v1/env-proxy/{bad}/v1/env",
                    timeout=10)
                raise AssertionError("expected HTTP error")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404, (bad, exc.code)

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

        # 4. terminal channel routed by env_id: browser WS -> parent -> tunnel
        # -> child PTY.  Same socket the tab's local terminals use, so adding an
        # environment costs the browser no connection at all.
        tag = f"pb-{int(time.time() * 1000)}"
        marker = f"BRIDGE_ECHO_{tag}".encode("ascii")
        ws = _ws_browser_connect(parent_srv.port, "/v1/terminals/ws")
        try:
            buf = _ws_channel_echo(ws, tag, b"echo " + marker + b"\r", marker, env_id=env_id)
            assert marker in buf, f"echo marker not seen; got {buf[-400:]!r}"
        finally:
            try:
                wsutil.ws_send_close(ws, 1000, mask=True)
            except OSError:
                pass
            ws.close()


def test_env_proxy_forwards_bodyless_delete(tmp_path):
    """Regression: a bridged DELETE with no body must reach the child.

    ``_handle_env_proxy`` used to demand ``Content-Length > 0`` for every
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
        base = f"http://127.0.0.1:{parent_srv.port}/v1/env-proxy/{env_id}"

        def _online():
            e = next((e for e in _parent_tunnel_envs(parent_srv) if e["id"] == env_id), None)
            return e if e and e.get("online") is True else None
        _wait_until(_online, message="tunnel env online")

        # Create a real PTY session on the child: the tab's one terminal socket,
        # channel routed by env_id.
        tag = f"del-{int(time.time() * 1000)}"
        ws = _ws_browser_connect(parent_srv.port, "/v1/terminals/ws")
        try:
            wsutil.ws_send_frame(ws, json.dumps(
                {"op": "open", "c": tag, "cols": 80, "rows": 24, "env_id": env_id},
            ).encode("utf-8"), wsutil.OP_TEXT, mask=True)

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


def test_env_proxy_bridge_with_child_auth(tmp_path):
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
        base = f"http://127.0.0.1:{parent_srv.port}/v1/env-proxy/{env_id}"
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

        # Terminal channel over the tunnel: the child self-authenticates the
        # handshake the parent dials on our behalf.
        tag = f"pa-{int(time.time() * 1000)}"
        marker = f"AUTH_ECHO_{tag}".encode("ascii")
        ws = _ws_browser_connect(parent_srv.port, "/v1/terminals/ws")
        try:
            buf = _ws_channel_echo(ws, tag, b"echo " + marker + b"\r", marker, env_id=env_id)
            assert marker in buf, f"echo marker not seen; got {buf[-400:]!r}"
        finally:
            try:
                wsutil.ws_send_close(ws, 1000, mask=True)
            except OSError:
                pass
            ws.close()


def test_env_proxy_bridge_percent_encoded_env_id(tmp_path):
    """Regression: the browser builds bridge URLs with encodeURIComponent
    (``tunnel%3A...``), so the raw request path carries a percent-encoded env
    id.  The parent must strip the *encoded* prefix when rebuilding the child
    path — otherwise the child receives ``GET /`` and answers 200 + the static
    index.html (text/html), and the frontend's ``res.json()`` yields null
    ("Cannot read properties of null (reading 'tools')").

    Only the HTTP bridge carries an env id in the path now; the terminal moved
    to the shared ``WS /v1/terminals/ws`` with ``env_id`` in the frame, where
    encoding cannot go wrong.
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
        base = f"http://127.0.0.1:{parent_srv.port}/v1/env-proxy/{enc_id}"

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

        # The terminal has no URL to get wrong any more: env_id travels inside
        # the open frame of the shared socket, colon included.
        tag = f"enc-{int(time.time() * 1000)}"
        marker = f"ENC_ECHO_{tag}".encode("ascii")
        ws = _ws_browser_connect(parent_srv.port, "/v1/terminals/ws")
        try:
            buf = _ws_channel_echo(ws, tag, b"echo " + marker + b"\r", marker, env_id=env_id)
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
# Large / chunked request bodies over the browser bridge
# ---------------------------------------------------------------------------

def _bridge_upload(parent_srv, env_id, name, payload, tag="", plan_out=None):
    """Browser-style workspace upload through the parent tunnel bridge:
    init -> chunk PUTs (Content-Length framing) -> complete.  Returns the
    (status, body) of the final complete call.  ``plan_out`` (a dict) receives
    the init response, so a test can assert on the chunk plan as well."""
    base = f"http://127.0.0.1:{parent_srv.port}/v1/env-proxy/{env_id}"

    def _raw(method, path, data=None, headers=None):
        req = urllib.request.Request(
            base + path, data=data, headers=dict(headers or {}), method=method)
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"raw": raw.decode(errors="replace")}

    # target dir: the child's workspace root (discover it via the bridge)
    _st, env = _raw("GET", "/v1/env")
    ws_root = (env or {}).get("env", {}).get("AGENTS_WORKSPACE", "")
    st, init = _raw("POST", "/v1/workspace/upload/init",
                    json.dumps({"workspace_id": "default", "file_name": name,
                                "file_size": len(payload),
                                "target_dir_path": ws_root,
                                "target_path": name}).encode(),
                    {"Content-Type": "application/json"})
    assert st == 200, (st, init)
    if plan_out is not None:
        plan_out.update(init)
    for chunk in init["chunks"]:
        st, body = _raw(
            "PUT", f"/v1/workspace/upload/{init['upload_id']}/chunk/{chunk['parallel_id']}",
            payload[chunk["offset"]: chunk["offset"] + chunk["size"]],
            {"Content-Type": "application/octet-stream",
             "X-Upload-Offset": str(chunk["offset"]),
             "X-Upload-Size": str(chunk["size"]),
             "X-File-Size": str(len(payload))})
        assert st == 200, (tag, "chunk", chunk["parallel_id"], st, body)
    return _raw("POST", f"/v1/workspace/upload/{init['upload_id']}/complete",
                b"{}", {"Content-Type": "application/json"})


def test_tunnel_proxy_large_chunk_upload_over_inline_limit(tmp_path):
    """Regression (remote tunnel + ~10MB file): a chunk bigger than the
    child's SMALL_BODY_INLINE threshold spills to a temp file; on Python
    3.13+ http.client then framed that file body as
    Transfer-Encoding: chunked (it no longer seeks file-like bodies to
    derive Content-Length), which the child's stdlib http.server cannot
    frame -- the local call 400s, the half-sent body breaks the keep-alive
    connection (broken pipe -> 502), and upload/complete failed with
    "some chunks are missing".  The child must now send an explicit
    Content-Length so the upload lands intact."""
    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
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

        # 10MB single chunk: > SMALL_BODY_INLINE (8MB) on the child.
        import random
        random.seed(101)
        payload = bytes(random.getrandbits(8) for _ in range(10 * 1024 * 1024))
        st, comp = _bridge_upload(parent_srv, env_id, "large.bin", payload, tag="large")
        assert st == 200, (st, comp)
        assert comp.get("status") == "completed", comp
        on_disk = (tmp_path / "child_ws" / "large.bin").read_bytes()
        assert len(on_disk) == len(payload)
        import hashlib
        assert hashlib.sha256(on_disk).hexdigest() == hashlib.sha256(payload).hexdigest()


def test_tunnel_proxy_browser_chunked_body(tmp_path):
    """Some mobile browsers send Blob PUT bodies with
    Transfer-Encoding: chunked and no Content-Length.  The parent bridge
    must consume that framing (dropping it would 400 the child AND desync
    the keep-alive connection to the browser) and forward the full body."""
    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
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

        base = f"http://127.0.0.1:{parent_srv.port}/v1/env-proxy/{env_id}"
        req = urllib.request.Request(
            base + "/v1/env")
        with urllib.request.urlopen(req, timeout=30) as resp:
            ws_root = json.loads(resp.read()).get("env", {}).get("AGENTS_WORKSPACE", "")
        payload = os.urandom(123457)  # odd size: 15 x 8192 + 2017
        st, init = _request(
            parent_srv, "POST", f"/v1/env-proxy/{env_id}/v1/workspace/upload/init",
            {"workspace_id": "default", "file_name": "chunked.bin",
             "file_size": len(payload), "target_dir_path": ws_root,
             "target_path": "chunked.bin"})
        assert st == 200, (st, init)
        chunk = init["chunks"][0]

        # Raw socket: PUT with Transfer-Encoding: chunked, no Content-Length.
        raw = socket.create_connection(("127.0.0.1", parent_srv.port), timeout=120)
        req = (
            f"PUT {base}/v1/workspace/upload/{init['upload_id']}/chunk/0 HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\n"
            "Content-Type: application/octet-stream\r\n"
            "Transfer-Encoding: chunked\r\n"
            f"X-Upload-Offset: {chunk['offset']}\r\n"
            f"X-Upload-Size: {chunk['size']}\r\n"
            f"X-File-Size: {len(payload)}\r\n"
            "\r\n"
        ).encode("ascii")
        raw.sendall(req)
        for i in range(0, len(payload), 8192):
            piece = payload[i:i + 8192]
            raw.sendall(f"{len(piece):x}\r\n".encode("ascii") + piece + b"\r\n")
        raw.sendall(b"0\r\n\r\n")
        resp = b""
        raw.settimeout(120)
        while b"\r\n\r\n" not in resp:
            d = raw.recv(65536)
            if not d:
                break
            resp += d
        raw.close()
        head, _, rest = resp.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        assert " 200 " in status_line, (status_line, resp[:300])

        st, comp = _request(
            parent_srv, "POST",
            f"/v1/env-proxy/{env_id}/v1/workspace/upload/{init['upload_id']}/complete", {})
        assert st == 200, (st, comp)
        on_disk = (tmp_path / "child_ws" / "chunked.bin").read_bytes()
        assert on_disk == payload


def test_env_proxy_multi_chunk_upload_lands_intact(tmp_path, monkeypatch):
    """A size-driven plan (UPLOAD_PARALLEL_SIZE well below the file size)
    really moves the bytes: 12 MB through the tunnel as 3 chunks of 4 MB,
    assembled on the child and verified by hash."""
    monkeypatch.setenv("UPLOAD_PARALLEL_SIZE", "5MB")
    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
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

        import hashlib
        payload = os.urandom(12 * 1024 * 1024)
        plan = {}
        st, comp = _bridge_upload(parent_srv, env_id, "multi.bin", payload, tag="multi", plan_out=plan)
        assert st == 200, (st, comp)
        assert [c["size"] for c in plan["chunks"]] == [4 * 1024 * 1024] * 3
        on_disk = (tmp_path / "child_ws" / "multi.bin").read_bytes()
        assert len(on_disk) == len(payload)
        assert hashlib.sha256(on_disk).hexdigest() == hashlib.sha256(payload).hexdigest()


def test_env_proxy_upload_plan_keeps_chunks_under_the_proxy_cap(tmp_path):
    """Regression (multi-GB upload into a tunnel child): the child's chunk
    plan used to be capped at ``parallel_max_threads`` chunks, so the chunk
    *size* grew with the file -- a 2.7 GB file was planned as 5 PUTs of 553 MB.
    The parent's /v1/env-proxy bridge refuses any request body above
    ``_MAX_PUSH_BODY_BYTES`` (512 MB), and it says so while the browser is
    still writing, which the browser reports as an opaque "upload network
    error" at 0%: every file above ``parallel_max_threads * 512 MB`` was
    un-uploadable through a child environment.  The plan is size-driven now, so
    no chunk may ever cross that line -- which is exactly what the bridge hands
    back to the browser here (no bytes uploaded, the plan alone is the test)."""
    from runtime.handler_base import _MAX_PUSH_BODY_BYTES

    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
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

        base = f"http://127.0.0.1:{parent_srv.port}/v1/env-proxy/{env_id}"
        req = urllib.request.Request(base + "/v1/env")
        with urllib.request.urlopen(req, timeout=30) as resp:
            ws_root = json.loads(resp.read()).get("env", {}).get("AGENTS_WORKSPACE", "")

        file_size = int(2.7 * 1024 ** 3)  # the size from the report
        st, init = _request(
            parent_srv, "POST", f"/v1/env-proxy/{env_id}/v1/workspace/upload/init",
            {"workspace_id": "default", "file_name": "big.zip",
             "file_size": file_size, "target_dir_path": ws_root,
             "target_path": "big.zip"})
        assert st == 200, (st, init)
        assert init["chunk_count"] > 1
        for chunk in init["chunks"]:
            assert chunk["size"] <= _MAX_PUSH_BODY_BYTES, (
                f"chunk {chunk['parallel_id']} ({chunk['size']} B) is bigger than "
                f"what the parent forwards ({_MAX_PUSH_BODY_BYTES} B)")
        assert sum(c["size"] for c in init["chunks"]) == file_size

        st, done = _request(
            parent_srv, "DELETE", f"/v1/env-proxy/{env_id}/v1/workspace/upload/{init['upload_id']}")
        assert st == 200, (st, done)


def test_env_proxy_rejects_chunk_body_over_the_push_cap(tmp_path):
    """What the over-cap body looks like from the browser's side: the parent
    answers 413 and drops the connection while the body is still being
    written, so the XHR dies in ``onerror`` ("upload network error") rather
    than showing the child's verdict.  Kept as documentation of the failure
    the chunk plan must never provoke."""
    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
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

        base = f"http://127.0.0.1:{parent_srv.port}/v1/env-proxy/{env_id}"
        req = urllib.request.Request(base + "/v1/env")
        with urllib.request.urlopen(req, timeout=30) as resp:
            ws_root = json.loads(resp.read()).get("env", {}).get("AGENTS_WORKSPACE", "")

        payload = os.urandom(100_000)
        st, init = _request(
            parent_srv, "POST", f"/v1/env-proxy/{env_id}/v1/workspace/upload/init",
            {"workspace_id": "default", "file_name": "oversize.bin",
             "file_size": len(payload), "target_dir_path": ws_root,
             "target_path": "oversize.bin"})
        assert st == 200, (st, init)
        chunk = init["chunks"][0]

        # Shrink the bridge's body cap: the 100 KB PUT above is then the
        # 553 MB chunk of the report, without moving gigabytes in a test.
        import runtime.handler_base as handler_base
        original_cap = handler_base._MAX_PUSH_BODY_BYTES
        handler_base._MAX_PUSH_BODY_BYTES = 1_000
        try:
            raw = socket.create_connection(("127.0.0.1", parent_srv.port), timeout=30)
            raw.sendall((
                f"PUT {base}/v1/workspace/upload/{init['upload_id']}/chunk/0 HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Content-Type: application/octet-stream\r\n"
                f"Content-Length: {chunk['size']}\r\n"
                f"X-Upload-Offset: {chunk['offset']}\r\n"
                f"X-Upload-Size: {chunk['size']}\r\n"
                f"X-File-Size: {len(payload)}\r\n"
                "\r\n"
            ).encode("ascii"))
            # Send a slice and stall: exactly the mid-upload state in which a
            # browser learns the connection is gone.
            raw.sendall(payload[:2000])
            raw.settimeout(30)
            response = b""
            while True:
                piece = raw.recv(65536)
                if not piece:
                    break
                response += piece
                head, sep, rest = response.partition(b"\r\n\r\n")
                if not sep:
                    continue
                declared = 0
                for line in head.split(b"\r\n")[1:]:
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"content-length":
                        declared = int(value.strip() or 0)
                if len(rest) >= declared:
                    break
            raw.close()
        finally:
            handler_base._MAX_PUSH_BODY_BYTES = original_cap

        status_line = response.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        assert " 413 " in status_line, (status_line, response[:300])
        assert b"Request body too large" in response


def test_read_chunked_body_unit():
    """_read_chunked_body framing: plain, trailers, extensions, and the
    failure modes (bad size line, oversized, truncated mid-body)."""
    from runtime.handler_base import HandlerBaseMixin

    import io

    def _host(raw: bytes):
        class _H(HandlerBaseMixin):
            def __init__(self):
                self.rfile = io.BytesIO(raw)
                self.close_connection = False
            def _send_json_error(self, status, message):
                pass
        return _H()

    plain = b"5\r\nhello\r\n6\r\nworld!\r\n0\r\n\r\n"
    assert _host(plain)._read_chunked_body(1 << 20) == b"helloworld!"

    trailer = b"3\r\nabc\r\n0\r\nX-Trail: 1\r\n\r\n"
    assert _host(trailer)._read_chunked_body(1 << 20) == b"abc"

    extended = b"4;ext=1\r\nabcd\r\n0\r\n\r\n"
    assert _host(extended)._read_chunked_body(1 << 20) == b"abcd"

    empty = b"0\r\n\r\n"
    assert _host(empty)._read_chunked_body(1 << 20) == b""

    assert _host(b"xyz\r\n0\r\n\r\n")._read_chunked_body(1 << 20) is None      # bad size
    assert _host(b"5\r\nhe")._read_chunked_body(1 << 20) is None               # truncated
    assert _host(b"5\r\nhello\r\n")._read_chunked_body(1 << 20) is None        # no terminator
    assert _host(b"100\r\n" + b"q" * 100 + b"\r\n0\r\n\r\n")._read_chunked_body(10) is None  # oversize


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


# ---------------------------------------------------------------------------
# Streaming responses must not block other calls (send-lock regression)
# ---------------------------------------------------------------------------

class _SlowStreamMixin:
    """Handler mixin: GET /v1/tunnel-test/slow?ticks=N streams one line
    every 0.25s with NO Content-Length (read-until-EOF, like a remote chat
    SSE turn), then closes."""

    def do_GET(self):
        path = urllib.parse.urlparse(self.path)
        if path.path.rstrip("/") == "/v1/tunnel-test/slow":
            self._slow_stream()
            return
        super().do_GET()

    def _slow_stream(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        ticks = int((q.get("ticks") or ["16"])[0])
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        # No Content-Length (read-until-EOF, like a remote chat SSE turn):
        # the body ends when the handler closes the connection.
        self.close_connection = True
        for i in range(ticks):
            try:
                self.wfile.write(f"tick-{i}\n".encode("ascii"))
                self.wfile.flush()
            except OSError:
                return
            time.sleep(0.25)


def _install_slow_stream(child_srv):
    """Give the child's HTTP server the slow-streaming endpoint (new
    connections pick up the patched handler class)."""
    base = child_srv._server.RequestHandlerClass
    child_srv._server.RequestHandlerClass = type(
        "SlowStreamHandler", (_SlowStreamMixin, base), {})


def _pair_online(tmp_path):
    parent = _real_server(tmp_path, "parent_data")
    child = _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws")
    parent_srv, _ = parent.__enter__()
    child_srv, child_data = child.__enter__()
    _set_child_source(child_srv, child_data, f"http://127.0.0.1:{parent_srv.port}/")
    status, body = _request(child_srv, "POST", "/v1/tunnel/parent/register")
    assert status == 200, body
    env_id = body["env_id"]

    def _online():
        e = next((e for e in _parent_tunnel_envs(parent_srv) if e["id"] == env_id), None)
        return e if e and e.get("online") is True else None
    _wait_until(_online, message="tunnel env online")
    return parent, child, parent_srv, child_srv, env_id


def test_welcome_advertises_resp_chunk_cap(tmp_path):
    """The parent's welcome frame advertises CAP_RESP_CHUNK_ID and the
    child stores it for the live connection (enables per-chunk framing)."""
    from runtime.tunnel_protocol import CAP_RESP_CHUNK_ID
    parent, child, parent_srv, child_srv, env_id = _pair_online(tmp_path)
    try:
        client = child_srv._server.tunnel_client
        assert CAP_RESP_CHUNK_ID in client._live_caps, client._live_caps
    finally:
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)


def test_streaming_response_does_not_block_other_calls(tmp_path):
    """Remote-execution mode: a chat turn streams for minutes over the
    tunnel while the file manager does init/PUT/complete + directory
    listings on the SAME tunnel.  Before the fix the child held
    _send_lock across the whole streaming response (the old parent routes
    bare binary body frames with one active-body cursor), so every other
    response queued behind the turn: uploads stalled at 100% (bytes sent,
    response pending) until the 600s parent timeout, while the child's
    local work had already completed."""
    parent, child, parent_srv, child_srv, env_id = _pair_online(tmp_path)
    try:
        _install_slow_stream(child_srv)
        manager = parent_srv._tunnel_manager
        stream = {}

        def _stream():
            try:
                st, _h, chunks = manager.call_env_stream(
                    env_id, "GET", "/v1/tunnel-test/slow?ticks=20")
                body = b""
                while True:
                    c = chunks.get(timeout=60)
                    if c is None:
                        break
                    body += c
                stream["status"], stream["body"] = st, body
            except Exception as exc:  # pragma: no cover - debug aid
                stream["error"] = repr(exc)

        t = threading.Thread(target=_stream)
        t.start()
        time.sleep(1.5)  # mid-stream (20 ticks x 0.25s = 5s total)

        t0 = time.monotonic()
        code, headers, raw = manager.call_env(env_id, "GET", "/v1/tools")
        elapsed = time.monotonic() - t0
        assert code == 200, raw[:200]
        assert b"write_file" in raw
        assert elapsed < 2.0, (
            f"fast call waited {elapsed:.1f}s behind the streaming response "
            "(send-lock stall regression)")

        t.join(30)
        assert "error" not in stream, stream
        assert stream["status"] == 200
        body = stream["body"].decode("ascii")
        assert body.count("tick-") == 20, body[-200:]
        assert "tick-0\n" in body and "tick-19\n" in body
    finally:
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)


def test_upload_completes_during_active_stream(tmp_path):
    """End-to-end (browser-style, through the parent's tunnel proxy): a
    workspace upload must finish while a long streaming response is in
    flight on the same tunnel -- the exact ChatPage file-manager scenario."""
    parent, child, parent_srv, child_srv, env_id = _pair_online(tmp_path)
    try:
        _install_slow_stream(child_srv)
        manager = parent_srv._tunnel_manager

        def _stream():
            st, _h, chunks = manager.call_env_stream(
                env_id, "GET", "/v1/tunnel-test/slow?ticks=28")  # 7s
            while True:
                if chunks.get(timeout=60) is None:
                    break

        t = threading.Thread(target=_stream)
        t.start()
        time.sleep(1.0)

        payload = os.urandom(2 * 1024 * 1024)
        t0 = time.monotonic()
        st, comp = _bridge_upload(parent_srv, env_id, "during-stream.bin", payload)
        elapsed = time.monotonic() - t0
        assert st == 200, (st, comp)
        assert comp.get("status") == "completed", comp
        assert elapsed < 4.0, (
            f"upload took {elapsed:.1f}s while the stream was still active "
            "(send-lock stall regression)")
        on_disk = (tmp_path / "child_ws" / "during-stream.bin").read_bytes()
        assert on_disk == payload

        t.join(30)
    finally:
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)


def test_legacy_framing_upload_still_lands(tmp_path):
    """Compatibility: with the child forced onto the OLD frame format
    (no CAP_RESP_CHUNK_ID -- what a new child does against an old parent),
    uploads over the tunnel must still complete correctly.  (They may be
    slow while a stream is active -- that is the pre-fix behaviour -- but
    they must not corrupt or fail.)"""
    parent, child, parent_srv, child_srv, env_id = _pair_online(tmp_path)
    try:
        client = child_srv._server.tunnel_client
        client._live_caps = set()  # simulate an old parent (no caps)
        payload = os.urandom(512 * 1024)
        st, comp = _bridge_upload(parent_srv, env_id, "legacy.bin", payload)
        assert st == 200, (st, comp)
        assert comp.get("status") == "completed", comp
        on_disk = (tmp_path / "child_ws" / "legacy.bin").read_bytes()
        assert on_disk == payload
    finally:
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Request bodies must not hold the tunnel's send lock (upload stall)
# ---------------------------------------------------------------------------

def _spy_send_frames(conn):
    """Record every send_frames() call on *conn*.

    One call == one acquisition of the connection-wide send lock, so the
    recorded shapes show exactly how long the tunnel is monopolised.
    """
    calls = []
    orig = conn.send_frames

    def spy(frames, refresh_activity=False):
        calls.append(list(frames))
        return orig(frames, refresh_activity=refresh_activity)

    conn.send_frames = spy
    return calls


def test_send_request_framing_depends_on_child_caps():
    """Routed framing (CAP_REQ_CHUNK_ID) emits one header/binary pair per
    chunk, each pair under its own lock; without the capability the whole
    header + body + terminator sequence stays atomic, because an old child
    reads request bodies inline on its reader thread."""
    from runtime.tunnel_manager import _TunnelConn
    from runtime.tunnel_protocol import (
        CAP_REQ_CHUNK_ID,
        OP_REQ,
        OP_REQ_CHUNK,
        REQ_BODY_CHUNKED,
    )

    body = os.urandom(CHUNK_SIZE * 2 + 7)  # three chunks

    old = _TunnelConn(_FakeSendSock(), "tid")
    legacy_calls = _spy_send_frames(old)
    old.send_request("PUT", "/x", {"H": "1"}, body, "rid-old")
    assert [len(c) for c in legacy_calls] == [1 + 3 + 1], [len(c) for c in legacy_calls]
    header = legacy_calls[0][0]
    assert decode_frame(header[1]) == {
        "op": OP_REQ, "id": "rid-old", "method": "PUT", "path": "/x",
        "headers": {"H": "1"},
    }
    binary = [d for opcode, d in legacy_calls[0][1:] if opcode == wsutil.OP_BINARY]
    assert binary[-1] == b""  # terminator
    assert b"".join(binary[:-1]) == body

    new = _TunnelConn(_FakeSendSock(), "tid", caps={CAP_REQ_CHUNK_ID})
    calls = _spy_send_frames(new)
    new.send_request("PUT", "/x", {"H": "1"}, body, "rid-new")
    assert [len(c) for c in calls] == [1, 2, 2, 2, 1], [len(c) for c in calls]
    assert decode_frame(calls[0][0][1])["body"] == REQ_BODY_CHUNKED
    for head, binr in [c for c in calls if len(c) == 2]:
        assert head[0] == wsutil.OP_TEXT and binr[0] == wsutil.OP_BINARY
        assert decode_frame(head[1]) == {
            "op": OP_REQ_CHUNK, "id": "rid-new", "eof": False,
        }
    assert decode_frame(calls[-1][0][1]) == {
        "op": OP_REQ_CHUNK, "id": "rid-new", "eof": True,
    }
    assert b"".join(c[1][1] for c in calls if len(c) == 2) == body


def test_child_advertises_req_chunk_cap(tmp_path):
    """The child's hello advertises CAP_REQ_CHUNK_ID and the parent keeps it
    on the live connection (that is what enables the routed framing)."""
    from runtime.tunnel_protocol import CAP_REQ_CHUNK_ID
    parent, child, parent_srv, child_srv, env_id = _pair_online(tmp_path)
    try:
        conn = parent_srv._tunnel_manager.conn_for_env(env_id)
        assert CAP_REQ_CHUNK_ID in conn.caps, conn.caps
    finally:
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)


def test_bridge_upload_sends_body_in_per_chunk_pairs(tmp_path):
    """End-to-end: a bridge upload's body leaves the parent as short
    header+binary pairs, never as one lock-long sequence."""
    parent, child, parent_srv, child_srv, env_id = _pair_online(tmp_path)
    try:
        conn = parent_srv._tunnel_manager.conn_for_env(env_id)
        calls = _spy_send_frames(conn)
        payload = os.urandom(CHUNK_SIZE * 3 + 11)
        st, comp = _bridge_upload(parent_srv, env_id, "pairs.bin", payload)
        assert st == 200, (st, comp)
        assert comp.get("status") == "completed", comp
        assert (tmp_path / "child_ws" / "pairs.bin").read_bytes() == payload
        assert max(len(c) for c in calls) <= 2, (
            "a request body was sent under a single lock hold: "
            f"frames per send_frames() call = {[len(c) for c in calls]}")
        pairs = [c for c in calls if len(c) == 2]
        by_rid: dict = {}
        for head, binr in pairs:
            by_rid.setdefault(decode_frame(head[1])["id"], []).append(binr[1])
        biggest = max(by_rid.values(), key=lambda chunks: sum(len(x) for x in chunks))
        assert len(biggest) >= 4, {r: len(v) for r, v in by_rid.items()}
        assert b"".join(biggest) == payload
    finally:
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)


def test_legacy_request_framing_still_lands(tmp_path):
    """Compatibility, other direction: a child that does not advertise
    CAP_REQ_CHUNK_ID (an older build) gets the atomic legacy sequence, and its
    inline body reader must still assemble a multi-frame body."""
    parent, child, parent_srv, child_srv, env_id = _pair_online(tmp_path)
    try:
        conn = parent_srv._tunnel_manager.conn_for_env(env_id)
        conn.caps = set()  # as if the child were an older build
        payload = os.urandom(CHUNK_SIZE * 2 + 5)
        st, comp = _bridge_upload(parent_srv, env_id, "legacy-req.bin", payload)
        assert st == 200, (st, comp)
        assert comp.get("status") == "completed", comp
        assert (tmp_path / "child_ws" / "legacy-req.bin").read_bytes() == payload
    finally:
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)


class _ThrottledSock:
    """Socket proxy that delays every frame (simulates a slow tunnel link)."""

    def __init__(self, sock, per_frame: float):
        self._sock = sock
        self._per_frame = per_frame

    def sendall(self, data, *a, **k):
        time.sleep(self._per_frame)
        return self._sock.sendall(data, *a, **k)

    def __getattr__(self, name):
        return getattr(self._sock, name)


def test_large_upload_does_not_stall_other_bridge_requests(tmp_path):
    """The reported field failure: a big chunk PUT over a slow link, while the
    file manager's own listings and /v1/env polls pile up behind it.

    Before the fix the parent held the connection-wide send lock across the
    whole body, so those requests were answered at the instant the upload
    finished -- the user's log showed eight requests answered inside one 8ms
    window, 129s after the PUT started, and the page reload that followed
    killed the upload before ``complete`` was ever sent.
    """
    parent, child, parent_srv, child_srv, env_id = _pair_online(tmp_path)
    try:
        conn = parent_srv._tunnel_manager.conn_for_env(env_id)
        conn.sock = _ThrottledSock(conn.sock, 0.03)
        base = f"http://127.0.0.1:{parent_srv.port}/v1/env-proxy/{env_id}"

        def _call(method, path, data=None, headers=None):
            req = urllib.request.Request(
                base + path, data=data, headers=dict(headers or {}), method=method)
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status, json.loads(resp.read() or b"null")

        _st, env = _call("GET", "/v1/env")
        ws_root = env["env"]["AGENTS_WORKSPACE"]
        payload = os.urandom(4 * 1024 * 1024)
        st, init = _call(
            "POST", "/v1/workspace/upload/init",
            json.dumps({"workspace_id": "default", "file_name": "hol.bin",
                        "file_size": len(payload), "target_dir_path": ws_root,
                        "target_path": "hol.bin"}).encode(),
            {"Content-Type": "application/json"})
        assert st == 200, init
        chunk = init["chunks"][0]
        put = {}

        def _put():
            t0 = time.monotonic()
            st, body = _call(
                "PUT",
                f"/v1/workspace/upload/{init['upload_id']}/chunk/{chunk['parallel_id']}",
                payload,
                {"Content-Type": "application/octet-stream",
                 "X-Upload-Offset": str(chunk["offset"]),
                 "X-Upload-Size": str(chunk["size"]),
                 "X-File-Size": str(len(payload))})
            put["elapsed"] = time.monotonic() - t0
            put["res"] = (st, body)

        t = threading.Thread(target=_put)
        t.start()
        time.sleep(0.15)  # the body is on the wire by now
        t0 = time.monotonic()
        st_small, _ = _call("GET", "/v1/env")
        small = time.monotonic() - t0
        t.join(120)
        assert st_small == 200
        assert put.get("res", (0,))[0] == 200, put
        assert put["elapsed"] > 0.3, put  # the throttle really was in effect
        assert small < 0.5 * put["elapsed"], (
            f"a small request waited {small:.2f}s behind a "
            f"{put['elapsed']:.2f}s upload body "
            "(send-lock head-of-line blocking regression)")
        st, comp = _call(
            "POST", f"/v1/workspace/upload/{init['upload_id']}/complete",
            b"{}", {"Content-Type": "application/json"})
        assert st == 200, (st, comp)
        assert comp.get("status") == "completed", comp
        assert (tmp_path / "child_ws" / "hol.bin").read_bytes() == payload
    finally:
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)


class _EchoMixin:
    """Handler mixin: POST /v1/tunnel-test/echo -> {"size", "sha256"}."""

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path.rstrip("/") == "/v1/tunnel-test/echo":
            n = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(n) if n else b""
            payload = json.dumps({
                "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_POST()


def _install_echo(child_srv):
    base = child_srv._server.RequestHandlerClass
    child_srv._server.RequestHandlerClass = type("EchoHandler", (_EchoMixin, base), {})


def test_concurrent_request_bodies_route_by_rid(tmp_path):
    """Two multi-frame request bodies in flight at once stay intact: the child
    routes each binary frame to the rid of the chunk header that preceded it,
    which is what lets the parent release the send lock between pairs."""
    parent, child, parent_srv, child_srv, env_id = _pair_online(tmp_path)
    try:
        _install_echo(child_srv)
        manager = parent_srv._tunnel_manager
        bodies = {
            "a": os.urandom(CHUNK_SIZE * 3 + 5),
            "b": os.urandom(CHUNK_SIZE * 2 + 3),
        }
        results = {}

        def _send(key):
            code, _h, raw = manager.call_env(
                env_id, "POST", "/v1/tunnel-test/echo",
                {"Content-Type": "application/octet-stream"}, bodies[key], timeout=60)
            results[key] = (code, json.loads(raw or b"null"))

        threads = [threading.Thread(target=_send, args=(k,)) for k in bodies]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        for key, body in bodies.items():
            code, data = results.get(key, (None, None))
            assert code == 200, (key, data)
            assert data == {
                "size": len(body), "sha256": hashlib.sha256(body).hexdigest(),
            }, (key, data)
    finally:
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Streaming request bodies end-to-end
# ---------------------------------------------------------------------------
# The parent reads the browser request body off its socket and forwards it to
# the child chunk by chunk (OP_REQ_CHUNK pairs), instead of buffering the
# whole body before the first byte reaches the child.  That is what stops a
# slow tunnel upload from sitting at "100%" on the browser while the parent
# had long finished reading it.

def _read_http_response(raw, timeout=30):
    """Read one HTTP response off a raw socket; return (status_line, body)."""
    raw.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = raw.recv(65536)
        if not d:
            break
        buf += d
    head, _, body = buf.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    clen = 0
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            try:
                clen = int(line.split(b":", 1)[1].strip())
            except ValueError:
                clen = 0
    while len(body) < clen:
        d = raw.recv(65536)
        if not d:
            break
        body += d
    return status_line, body


def _raw_chunked_post(port, path, payload, piece=8192):
    """POST a Transfer-Encoding: chunked body on a raw socket (no
    Content-Length -- what some mobile browsers do for Blob uploads).
    Returns (status_line, response_body)."""
    raw = socket.create_connection(("127.0.0.1", port), timeout=120)
    try:
        raw.sendall((
            f"POST {path} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/octet-stream\r\n"
            "Transfer-Encoding: chunked\r\n"
            "\r\n"
        ).encode("ascii"))
        for i in range(0, len(payload), piece):
            p = payload[i:i + piece]
            raw.sendall(f"{len(p):x}\r\n".encode("ascii") + p + b"\r\n")
        raw.sendall(b"0\r\n\r\n")
        return _read_http_response(raw)
    finally:
        raw.close()


def _echo_body(base, payload, method="POST"):
    req = urllib.request.Request(
        base + "/v1/tunnel-test/echo", data=payload,
        headers={"Content-Type": "application/octet-stream"}, method=method)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.status, json.loads(resp.read())


def test_tunnel_proxy_streams_large_body_intact(tmp_path):
    """(i) A multi-frame body crosses the browser bridge byte-for-byte: the
    parent streams it to the child as short {header, binary} pairs (never one
    buffered blob, never one lock-long sequence)."""
    from runtime.tunnel_protocol import CAP_REQ_CHUNK_ID
    parent, child, parent_srv, child_srv, env_id = _pair_online(tmp_path)
    try:
        _install_echo(child_srv)
        conn = parent_srv._tunnel_manager.conn_for_env(env_id)
        assert CAP_REQ_CHUNK_ID in conn.caps, conn.caps
        calls = _spy_send_frames(conn)
        payload = os.urandom(CHUNK_SIZE * 3 + 1234)
        base = f"http://127.0.0.1:{parent_srv.port}/v1/env-proxy/{env_id}"
        status, data = _echo_body(base, payload)
        assert status == 200
        assert data == {"size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest()}, data
        assert max(len(c) for c in calls) <= 2, (
            "the request body was sent as one buffered blob: "
            f"frames per send_frames() call = {[len(c) for c in calls]}")
    finally:
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)


def test_tunnel_proxy_small_body_and_get(tmp_path):
    """(ii) A small body still goes through the streaming path intact, and a
    bodyless GET still works on the same bridge."""
    parent, child, parent_srv, child_srv, env_id = _pair_online(tmp_path)
    try:
        _install_echo(child_srv)
        base = f"http://127.0.0.1:{parent_srv.port}/v1/env-proxy/{env_id}"
        payload = b'{"hello":"world"}'
        status, data = _echo_body(base, payload)
        assert status == 200
        assert data == {"size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest()}, data
        with urllib.request.urlopen(f"{base}/v1/tools", timeout=30) as resp:
            assert resp.status == 200
            assert isinstance(json.loads(resp.read()), dict)
    finally:
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)


def test_tunnel_proxy_streams_chunked_body(tmp_path):
    """(iii) A Transfer-Encoding: chunked body (no Content-Length, as some
    mobile browsers send) is decoded on the fly and forwarded chunk by chunk;
    the child reconstructs the exact bytes."""
    from runtime.tunnel_protocol import CAP_REQ_CHUNK_ID
    parent, child, parent_srv, child_srv, env_id = _pair_online(tmp_path)
    try:
        _install_echo(child_srv)
        conn = parent_srv._tunnel_manager.conn_for_env(env_id)
        assert CAP_REQ_CHUNK_ID in conn.caps
        calls = _spy_send_frames(conn)
        payload = os.urandom(CHUNK_SIZE + 5000)
        status_line, body = _raw_chunked_post(
            parent_srv.port, f"/v1/env-proxy/{env_id}/v1/tunnel-test/echo", payload)
        assert " 200 " in status_line, (status_line, body[:200])
        assert json.loads(body) == {"size": len(payload),
                                    "sha256": hashlib.sha256(payload).hexdigest()}
        assert max(len(c) for c in calls) <= 2, [len(c) for c in calls]
    finally:
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)


def test_tunnel_proxy_buffers_without_req_chunk_cap(tmp_path):
    """(iv) Backward compatibility: a child that does NOT advertise
    CAP_REQ_CHUNK_ID gets the historical path -- the parent buffers the whole
    body and sends it as one atomic legacy sequence."""
    parent, child, parent_srv, child_srv, env_id = _pair_online(tmp_path)
    try:
        _install_echo(child_srv)
        conn = parent_srv._tunnel_manager.conn_for_env(env_id)
        conn.caps = set()  # as if the child were an older build
        assert not parent_srv._tunnel_manager.env_supports_req_chunks(env_id)
        calls = _spy_send_frames(conn)
        payload = os.urandom(CHUNK_SIZE * 2 + 7)
        base = f"http://127.0.0.1:{parent_srv.port}/v1/env-proxy/{env_id}"
        status, data = _echo_body(base, payload)
        assert status == 200
        assert data == {"size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest()}, data
        # header + every body chunk + terminator in ONE send_frames() call
        assert max(len(c) for c in calls) > 2, [len(c) for c in calls]
    finally:
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)


def test_tunnel_proxy_browser_disconnect_mid_body_aborts(tmp_path):
    """A browser that hangs up mid-upload is aborted cleanly: the parent
    answers 400, releases the child's half-received body, and the tunnel stays
    usable for the next request."""
    parent, child, parent_srv, child_srv, env_id = _pair_online(tmp_path)
    try:
        _install_echo(child_srv)
        client = child_srv._server.tunnel_client
        raw = socket.create_connection(("127.0.0.1", parent_srv.port), timeout=30)
        path = f"/v1/env-proxy/{env_id}/v1/tunnel-test/echo"
        raw.sendall((
            f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            "Content-Type: application/octet-stream\r\n"
            "Content-Length: 5000000\r\n\r\n"
        ).encode("ascii"))
        raw.sendall(b"x" * 4096)
        raw.shutdown(socket.SHUT_WR)  # body is short; the client is gone
        status_line, _body = _read_http_response(raw, timeout=30)
        raw.close()
        assert " 400 " in status_line, status_line
        _wait_until(lambda: not client._bodies,
                    message="child released the aborted request body")
        status, _env = _request(parent_srv, "GET", f"/v1/env-proxy/{env_id}/v1/env")
        assert status == 200
    finally:
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)


def test_tunnel_proxy_chunked_body_truncated_aborts(tmp_path):
    """A chunked body that ends before its terminating chunk (client gone)
    makes the parent drop the connection and release the child's half body,
    without desyncing the tunnel for later requests."""
    parent, child, parent_srv, child_srv, env_id = _pair_online(tmp_path)
    try:
        _install_echo(child_srv)
        client = child_srv._server.tunnel_client
        raw = socket.create_connection(("127.0.0.1", parent_srv.port), timeout=30)
        path = f"/v1/env-proxy/{env_id}/v1/tunnel-test/echo"
        raw.sendall((
            f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            "Content-Type: application/octet-stream\r\n"
            "Transfer-Encoding: chunked\r\n\r\n"
        ).encode("ascii"))
        raw.sendall(b"100\r\n" + b"y" * 0x100 + b"\r\n")  # one chunk, no 0-chunk
        raw.shutdown(socket.SHUT_WR)
        status_line, _body = _read_http_response(raw, timeout=30)
        raw.close()
        assert " 400 " in status_line, status_line
        _wait_until(lambda: not client._bodies,
                    message="child released the truncated chunked body")
        status, _env = _request(parent_srv, "GET", f"/v1/env-proxy/{env_id}/v1/env")
        assert status == 200
    finally:
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)


def test_tunnel_proxy_forwards_body_before_fully_read(tmp_path):
    """The parent forwards the body *as it arrives*: a chunk reaches the
    child (as a tunnel frame) while the browser still has bytes to send --
    that is what makes the browser's upload progress track the real tunnel
    rate instead of jumping to 100% immediately."""
    parent, child, parent_srv, child_srv, env_id = _pair_online(tmp_path)
    try:
        _install_echo(child_srv)
        conn = parent_srv._tunnel_manager.conn_for_env(env_id)
        forwarded = []
        orig = conn.send_frames

        def spy(frames, refresh_activity=False):
            for opcode, data in frames:
                if opcode == wsutil.OP_BINARY and data:
                    forwarded.append(len(data))
            return orig(frames, refresh_activity=refresh_activity)

        conn.send_frames = spy

        total = CHUNK_SIZE * 4
        raw = socket.create_connection(("127.0.0.1", parent_srv.port), timeout=30)
        path = f"/v1/env-proxy/{env_id}/v1/tunnel-test/echo"
        raw.sendall((
            f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            "Content-Type: application/octet-stream\r\n"
            f"Content-Length: {total}\r\n\r\n"
        ).encode("ascii"))
        half = total // 2
        sent = 0
        while sent < half:
            n = min(65536, half - sent)
            raw.sendall(b"z" * n)
            sent += n
        # the parent must have pushed a body frame to the child already
        _wait_until(lambda: forwarded, timeout=10, message="parent forwarded a body chunk")
        assert sum(forwarded) < total  # ... while half the body was still unsent
        while sent < total:
            n = min(65536, total - sent)
            raw.sendall(b"z" * n)
            sent += n
        status_line, body = _read_http_response(raw, timeout=60)
        raw.close()
        assert " 200 " in status_line, (status_line, body[:200])
        assert json.loads(body)["size"] == total
    finally:
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)


def test_streaming_body_readers_unit():
    """The module-level body readers: block splitting, the fixed-length and
    chunked framing, and their failure modes."""
    import io

    from runtime.handler_base import (
        iter_raw_body, iter_chunked_body, _BodyReaderError, _BodyTooLargeError,
    )

    # fixed length, split into blocks
    assert b"".join(iter_raw_body(io.BytesIO(b"abcdef"), 6, 1 << 20, 4)) == b"abcdef"
    with pytest.raises(_BodyTooLargeError):
        list(iter_raw_body(io.BytesIO(b""), 100, 10))
    with pytest.raises(_BodyReaderError):
        list(iter_raw_body(io.BytesIO(b"ab"), 4, 1 << 20))  # short body

    # chunked: plain, extensions, trailers, empty, and the failure modes
    assert b"".join(
        iter_chunked_body(io.BytesIO(b"3\r\nabc\r\n0\r\n\r\n"), 1 << 20)) == b"abc"
    assert b"".join(iter_chunked_body(
        io.BytesIO(b"4;ext=1\r\nabcd\r\n0\r\nX-T: 1\r\n\r\n"), 1 << 20)) == b"abcd"
    assert list(iter_chunked_body(io.BytesIO(b"0\r\n\r\n"), 1 << 20)) == []
    with pytest.raises(_BodyTooLargeError):
        list(iter_chunked_body(io.BytesIO(b"100\r\n" + b"q" * 100 + b"\r\n"), 10))
    with pytest.raises(_BodyReaderError):
        list(iter_chunked_body(io.BytesIO(b"5\r\nhe"), 1 << 20))       # truncated
    with pytest.raises(_BodyReaderError):
        list(iter_chunked_body(io.BytesIO(b"5\r\nhello"), 1 << 20))    # no CRLF
    with pytest.raises(_BodyReaderError):
        list(iter_chunked_body(io.BytesIO(b"xyz\r\n"), 1 << 20))       # bad size


# ---------------------------------------------------------------------------
# Loopback target resolution: the two premises the data plane used to hardcode
# ---------------------------------------------------------------------------

def _loopback_client(port, protocol="http", server_address=None, bind_host="0.0.0.0"):
    """A stand-in for the object handed to ``TunnelClient``.

    In production that object is the ``RuntimeHTTPServer`` *wrapper*, and it
    exposes ``port`` / ``protocol`` / ``bind_host`` -- not the socket-level
    ``server_address``.  The stand-in follows that shape; the ``bind_host=None``
    case is the only one that falls back to ``server_address``.
    """
    from runtime.tunnel_client import TunnelClient
    kwargs = {"port": port, "protocol": protocol}
    if bind_host is not None:
        kwargs["bind_host"] = bind_host
    if server_address is not None:
        kwargs["server_address"] = server_address
    tc = TunnelClient.__new__(TunnelClient)
    tc._server = types.SimpleNamespace(**kwargs)
    return tc


def test_loopback_target_follows_bind_and_protocol():
    assert _loopback_client(7988)._loopback_target() == ("127.0.0.1", 7988, False)
    assert _loopback_client(
        7988, "https", bind_host="192.168.1.7")._loopback_target() == ("192.168.1.7", 7988, True)
    assert _loopback_client(7988, "https")._loopback_target() == ("127.0.0.1", 7988, True)
    assert _loopback_client(7988, "http", bind_host="::")._loopback_target() == ("::1", 7988, False)
    # Duck-typed callers that only carry the socket tuple still resolve.
    assert _loopback_client(7988, bind_host=None, server_address=("10.0.0.5", 7988)
                            )._loopback_target() == ("10.0.0.5", 7988, False)
    # Neither: the loopback default, but no longer silently (a missing bind
    # address used to be swallowed and looked like an unreachable child).
    assert _loopback_client(7988, bind_host=None)._loopback_target() == ("127.0.0.1", 7988, False)
    assert hasattr(RuntimeHTTPServer, "bind_host"), (
        "RuntimeHTTPServer must expose its bind address: the tunnel self-call "
        "derives its target from the wrapper, and without this it is pinned to "
        "127.0.0.1 for every child -- including one bound to an interface IP")


def test_loopback_target_reads_the_real_wrapper(tmp_path):
    """Contract lock against the production object, not a stand-in.

    The self-call target is read off whatever is passed to ``TunnelClient``, so
    the unit tests above can stay green on a stand-in that happens to define an
    attribute the real server object does not have.  That is exactly how the
    bind-derived half of the fix stayed inert: a child bound to a concrete
    address answered every forwarded call with ``ConnectionRefusedError(111)``.
    """
    with _real_server(tmp_path, name="loopback-contract") as (srv, data_dir):
        from runtime.tunnel_client import TunnelClient
        assert srv.bind_host == "127.0.0.1" and srv.protocol == "http"
        tc = TunnelClient(srv, data_dir=str(data_dir))
        assert tc._loopback_target() == ("127.0.0.1", srv.port, False)


def test_ipv6_loopback_host_stays_dialable_in_a_ws_url():
    """A ``::`` bind resolves to ``::1``, and that has to survive URL parsing.

    The stream bridge dials ``ws://<host>:<port><path>``; an unbracketed IPv6
    literal makes urlsplit read ``::1:7988`` as the port, and the dial dies as
    ``Invalid port in WebSocket URL`` -- a broken terminal bridge with no TLS
    or tunnel fault behind it.
    """
    from runtime.tunnel_client import ws_authority
    for host in ("127.0.0.1", "192.168.1.7", "10.0.0.5", "::1", "fe80::1"):
        url = f"wss://{ws_authority(host)}:7988/v1/terminals/ws?token=x"
        parsed = urllib.parse.urlsplit(url)
        assert parsed.hostname == host, url
        assert parsed.port == 7988, url


def test_loopback_call_uses_tls_when_the_child_listens_with_tls():
    """A TLS-only child used to get a plaintext request: ConnectionResetError(104).

    The child's own ``protocol`` is now the authority, and the loopback context
    does not verify certificates by default (a self-signed cert against
    127.0.0.1 cannot be validated meaningfully).
    """
    from runtime import tunnel_client as tcmod
    chosen = {}

    class _Spy:
        def __init__(self, host, port, timeout=None, context=None):
            chosen.update(host=host, port=port, timeout=timeout, context=context)

        def connect(self):
            raise OSError("stop here: the address/protocol choice is what matters")

    plain_used = {}

    class _SpyPlain:
        def __init__(self, host, port, timeout=None):
            plain_used.update(host=host, port=port, timeout=timeout)

        def connect(self):
            raise OSError("stop here")

    real_https, real_http = http.client.HTTPSConnection, http.client.HTTPConnection
    http.client.HTTPSConnection = _Spy
    http.client.HTTPConnection = _SpyPlain
    try:
        try:
            _loopback_client(8399, "https")._local_call("GET", "/v1/models", {}, None)
        except OSError:
            pass
        assert chosen.get("host") == "127.0.0.1" and chosen.get("port") == 8399
        assert chosen.get("context") is not None
        assert chosen["context"].verify_mode == ssl.CERT_NONE
        assert plain_used == {}, "plaintext must not be used for a TLS child"

        try:
            _loopback_client(8400, "http", bind_host="10.0.0.5")._local_call(
                "GET", "/v1/models", {}, None)
        except OSError:
            pass
        assert plain_used.get("host") == "10.0.0.5", plain_used
        assert plain_used.get("timeout") == tcmod.TunnelClient._LOCAL_CONNECT_TIMEOUT
    finally:
        http.client.HTTPSConnection, http.client.HTTPConnection = real_https, real_http


def test_unhandled_local_call_error_answers_instead_of_timing_out():
    """A non-OSError used to leave the parent waiting out its 600 s timeout.

    ``_req_worker`` swallowed it with only a log line, so the parent had no resp
    frame and learned about an instant failure after the full forward timeout.
    """
    from runtime.tunnel_client import TunnelClient
    sent = []

    class _Ws:
        def send(self, *a, **k):
            return None

    tc = TunnelClient.__new__(TunnelClient)
    tc._server = types.SimpleNamespace(port=1, protocol="http",
                                       server_address=("0.0.0.0", 1))

    def _send_resp(ws, rid, status, headers, body):
        sent.append((rid, status, json.loads(body.decode())))

    def _boom(*a, **k):
        raise ValueError("bug that is not an OSError")

    tc._send_resp = _send_resp
    tc._local_call = _boom
    tc._execute_req(_Ws(), {"id": "r1", "method": "GET", "path": "/", "headers": {},
                            "body": None})
    assert sent and sent[0][0] == "r1" and sent[0][1] == 500
    assert "not an OSError" in sent[0][2]["error"]


def test_oserror_answer_names_the_loopback_target():
    from runtime.tunnel_client import TunnelClient
    sent = []
    tc = TunnelClient.__new__(TunnelClient)
    tc._server = types.SimpleNamespace(port=8391, protocol="https",
                                       server_address=("172.30.1.9", 8391))

    def _send_resp(ws, rid, status, headers, body):
        sent.append((status, json.loads(body.decode())))

    def _boom(*a, **k):
        raise ConnectionRefusedError(111, "Connection refused")

    tc._send_resp = _send_resp
    tc._local_call = _boom
    tc._execute_req(object(), {"id": "r2", "method": "GET", "path": "/", "headers": {},
                               "body": None})
    assert sent and sent[0][0] == 502
    assert "https://172.30.1.9:8391" in sent[0][1]["hint"]
