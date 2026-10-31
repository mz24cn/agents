"""Tests for the multiplexed terminal WebSocket (``WS /v1/terminals/ws``).

One socket per browser tab carries every terminal that tab has opened, so N
terminals cost one of the browser's ~6 per-origin connections instead of N
(docs/plan-connection-budget.md).  These tests drive the endpoint with a raw
WebSocket client and check the parts that only exist because of the
multiplexing: per-channel routing, per-channel detach, re-attach reusing the
same PTY, and the auth gate.
"""

import base64
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.request
from unittest.mock import patch

import pytest

from runtime import handler_base, server_state, wsutil
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer


def _runtime() -> Runtime:
    return Runtime(ModelRegistry(), ToolRegistry())


# ------------------------------------------------------------------
# Fixtures / helpers
# ------------------------------------------------------------------


@pytest.fixture()
def server(tmp_path):
    """RuntimeHTTPServer on a random port, with terminal state torn down after."""
    before = set(server_state._terminal_sessions)
    with patch("runtime.server._MODELS_PATH", str(tmp_path / "models.json")), \
         patch("runtime.server._TOOLS_PATH", str(tmp_path / "tools.json")), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", str(tmp_path / "prompt_templates.json")), \
         patch("runtime.server._MCP_SERVERS_PATH", str(tmp_path / "mcp_servers.json")), \
         patch("runtime.server._ENV_PATH", str(tmp_path / "env.json")), \
         patch("runtime.server._AUTH_PATH", str(tmp_path / "auth_token.json")), \
         patch("runtime.server._DATA_DIR", str(tmp_path)):
        before = set(server_state._terminal_sessions)
        srv = RuntimeHTTPServer(_runtime())
        srv.start_background(host="127.0.0.1", port=0)
        try:
            yield srv
        finally:
            for tid in list(server_state._terminal_sessions):
                if tid not in before:
                    server_state.unregister_terminal_session(tid)
            srv.stop()


class MuxClient:
    """Minimal masked WebSocket client for the mux endpoint."""

    def __init__(self, port, path="/v1/terminals/ws", cookie=None):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        cookie_line = f"Cookie: {cookie}\r\n" if cookie else ""
        self.sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                f"{cookie_line}"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
        )
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            head += chunk
        self.status = head.split(b"\r\n", 1)[0].decode() if head else ""
        self.buf = head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""
        self.messages: list[dict] = []

    # -- framing -----------------------------------------------------

    def _read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(n - len(self.buf), 4096))
            if not chunk:
                raise AssertionError("mux socket closed by server")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _recv_frame(self):
        b1, b2 = self._read_exact(2)
        opcode = b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        assert not (b2 & 0x80), "server frames must not be masked"
        payload = self._read_exact(length)
        return opcode, payload

    def _pump(self, timeout: float) -> None:
        self.sock.settimeout(timeout)
        opcode, payload = self._recv_frame()
        if opcode == wsutil.OP_TEXT:
            self.messages.append(json.loads(payload.decode("utf-8")))

    # -- protocol ----------------------------------------------------

    def send(self, payload: dict) -> None:
        wsutil.ws_send_text(self.sock, json.dumps(payload), mask=True)

    def open(self, terminal_id, cols=80, rows=24, workspace=None, env_id=None) -> None:
        msg = {"op": "open", "c": terminal_id, "cols": cols, "rows": rows}
        if workspace:
            msg["workspace"] = workspace
        if env_id:
            msg["env_id"] = env_id
        self.send(msg)

    def input(self, terminal_id, data) -> None:
        self.send({"op": "input", "c": terminal_id, "d": data})

    def resize(self, terminal_id, cols, rows) -> None:
        self.send({"op": "resize", "c": terminal_id, "cols": cols, "rows": rows})

    def close_channel(self, terminal_id) -> None:
        self.send({"op": "close", "c": terminal_id})

    def raw_text(self, text: str) -> None:
        wsutil.ws_send_text(self.sock, text, mask=True)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    # -- assertions --------------------------------------------------

    def wait_for(self, pred, timeout=8.0, note=""):
        """Return the first buffered/received message satisfying ``pred``."""
        deadline = time.monotonic() + timeout
        while True:
            for msg in self.messages:
                if pred(msg):
                    return msg
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"timeout waiting for {note or pred}; got {self.messages}")
            self._pump(remaining)

    def opened(self, terminal_id) -> dict:
        return self.wait_for(
            lambda m: m.get("op") == "opened" and m.get("c") == terminal_id,
            note=f"opened {terminal_id}",
        )

    def output_text(self, terminal_id) -> str:
        return "".join(
            m["d"] for m in self.messages
            if m.get("c") == terminal_id and isinstance(m.get("d"), str) and "op" not in m
        )

    def wait_for_output(self, terminal_id, marker, timeout=10.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            if marker in self.output_text(terminal_id):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"marker {marker!r} never reached channel {terminal_id}: "
                    f"{self.output_text(terminal_id)!r}"
                )
            self._pump(remaining)


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


def test_mux_carries_two_terminals_on_one_socket(server):
    """Two terminals, one socket, output routed by channel id."""
    client = MuxClient(server.port)
    try:
        assert client.status.endswith("101 Switching Protocols"), client.status
        client.open("mux-a")
        client.open("mux-b")
        client.opened("mux-a")
        client.opened("mux-b")

        client.input("mux-a", "echo MARKER_ALPHA_7431\n")
        client.input("mux-b", "echo MARKER_BETA_9820\n")
        client.wait_for_output("mux-a", "MARKER_ALPHA_7431")
        client.wait_for_output("mux-b", "MARKER_BETA_9820")

        assert "MARKER_BETA_9820" not in client.output_text("mux-a")
        assert "MARKER_ALPHA_7431" not in client.output_text("mux-b")
    finally:
        client.close()


def test_mux_resize_reaches_the_pty(server):
    client = MuxClient(server.port)
    try:
        client.open("mux-size", cols=80, rows=24)
        client.opened("mux-size")
        client.resize("mux-size", 132, 43)

        deadline = time.monotonic() + 5
        import fcntl
        import termios
        while time.monotonic() < deadline:
            info = server_state.get_terminal_for_session("mux-size")
            if info:
                try:
                    rows, cols, _, _ = struct.unpack(
                        "HHHH", fcntl.ioctl(info["master_fd"], termios.TIOCGWINSZ, b"\x00" * 8)
                    )
                    if (cols, rows) == (132, 43):
                        return
                except OSError:
                    pass
            time.sleep(0.05)
        raise AssertionError("winsize never reached the PTY")
    finally:
        client.close()


def test_mux_close_op_detaches_one_channel_and_keeps_the_pty(server):
    """``close`` frees the channel slot but never kills the shell."""
    client = MuxClient(server.port)
    try:
        client.open("mux-keep")
        client.open("mux-live")
        client.opened("mux-keep")
        client.opened("mux-live")

        client.close_channel("mux-keep")
        client.wait_for(
            lambda m: m.get("op") == "closed" and m.get("c") == "mux-keep",
            note="closed mux-keep",
        )

        # The PTY itself survives (that is what makes reopening instant).
        info = server_state.get_terminal_for_session("mux-keep")
        assert info is not None
        assert info.get("sock") is None
        assert info.get("send_output") is None
        os.kill(info["pid"], 0)

        # Input to a detached channel is ignored, the other channel still works.
        client.input("mux-keep", "echo SHOULD_NOT_RUN\n")
        client.input("mux-live", "echo MARKER_LIVE_5150\n")
        client.wait_for_output("mux-live", "MARKER_LIVE_5150")
        assert "SHOULD_NOT_RUN" not in client.output_text("mux-keep")
    finally:
        client.close()


def test_mux_socket_loss_detaches_all_but_reopen_reuses_the_pty(server):
    """Dropping the socket detaches every channel; reopening re-attaches them."""
    first = MuxClient(server.port)
    try:
        first.open("mux-reopen")
        first.opened("mux-reopen")
        first.input("mux-reopen", "echo MARKER_BEFORE_3301\n")
        first.wait_for_output("mux-reopen", "MARKER_BEFORE_3301")
    finally:
        first.close()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        info = server_state.get_terminal_for_session("mux-reopen")
        if info and info.get("sock") is None:
            break
        time.sleep(0.05)
    info = server_state.get_terminal_for_session("mux-reopen")
    assert info is not None and info.get("sock") is None
    pid = info["pid"]
    os.kill(pid, 0)  # still alive

    second = MuxClient(server.port)
    try:
        second.open("mux-reopen")
        second.opened("mux-reopen")
        same = server_state.get_terminal_for_session("mux-reopen")
        assert same is not None and same["pid"] == pid, "reopen must reuse the PTY"
        second.input("mux-reopen", "echo MARKER_AFTER_8802\n")
        second.wait_for_output("mux-reopen", "MARKER_AFTER_8802")
    finally:
        second.close()


def test_mux_rejects_bad_frames_without_dropping_the_socket(server):
    client = MuxClient(server.port)
    try:
        client.raw_text("not json at all")
        client.wait_for(lambda m: m.get("error") == "malformed frame", note="malformed frame")

        client.send({"op": "input", "d": "orphan keystroke"})
        client.wait_for(lambda m: m.get("error") == "missing channel id", note="missing id")

        client.send({"op": "bogus", "c": "mux-x"})
        client.wait_for(
            lambda m: m.get("op") == "error" and m.get("c") == "mux-x",
            note="unknown op error",
        )

        # Still usable afterwards.
        client.open("mux-x")
        client.opened("mux-x")
        client.input("mux-x", "echo MARKER_RECOVERED_6620\n")
        client.wait_for_output("mux-x", "MARKER_RECOVERED_6620")
    finally:
        client.close()


def test_mux_channel_limit_is_enforced(server, monkeypatch):
    monkeypatch.setattr(handler_base, "_MAX_MUX_CHANNELS", 2)
    client = MuxClient(server.port)
    try:
        client.open("mux-l1")
        client.open("mux-l2")
        client.opened("mux-l1")
        client.opened("mux-l2")
        client.open("mux-l3")
        err = client.wait_for(
            lambda m: m.get("op") == "error" and m.get("c") == "mux-l3",
            note="channel limit error",
        )
        assert "too many terminals" in err.get("error", "")
    finally:
        client.close()


def test_endpoint_only_speaks_the_channel_protocol(server):
    """The old per-terminal framing is gone: a bare frame is not keystrokes.

    ``/v1/terminals/ws`` used to take the terminal id from the query string and
    treat every frame as raw keyboard input.  It now only speaks channel
    frames, so a legacy client gets an error instead of a shell — and the
    socket survives to be driven properly.
    """
    client = MuxClient(server.port, path="/v1/terminals/ws?terminal_id=mux-legacy")
    try:
        assert client.status.endswith("101 Switching Protocols"), client.status
        client.raw_text("echo not-keystrokes\r")
        err = client.wait_for(lambda m: m.get("op") == "error", note="malformed frame error")
        assert "malformed" in err.get("error", ""), err
        client.open("mux-legacy")
        client.opened("mux-legacy")
    finally:
        client.close()


def test_mux_requires_authorization_when_auth_enabled(tmp_path):
    """The mux socket spawns shells too, so it sits behind the same /v1/ gate.

    ``_AUTH_PATH`` must be redirected: this test writes a password, and the
    module-level default points at the real ``~/.agents_runtime``.
    """
    before = set(server_state._terminal_sessions)
    with patch("runtime.server._MODELS_PATH", str(tmp_path / "models.json")), \
         patch("runtime.server._TOOLS_PATH", str(tmp_path / "tools.json")), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", str(tmp_path / "prompt_templates.json")), \
         patch("runtime.server._MCP_SERVERS_PATH", str(tmp_path / "mcp_servers.json")), \
         patch("runtime.server._ENV_PATH", str(tmp_path / "env.json")), \
         patch("runtime.server._AUTH_PATH", str(tmp_path / "auth_token.json")), \
         patch("runtime.server._DATA_DIR", str(tmp_path)):
        srv = RuntimeHTTPServer(_runtime())
        srv.start_background(host="127.0.0.1", port=0)
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.port}/v1/auth/config",
                data=json.dumps({"password": "test-password-123"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                assert resp.status == 200

            anonymous = MuxClient(srv.port)
            assert anonymous.status == "HTTP/1.1 401 Unauthorized", anonymous.status
            anonymous.close()

            with urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{srv.port}/v1/auth/login",
                    data=json.dumps({"password": "test-password-123"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=15,
            ) as resp:
                set_cookie = resp.headers.get("Set-Cookie", "")
            cookie = "agent_service_session=" + set_cookie.split("agent_service_session=", 1)[1].split(";", 1)[0]

            authed = MuxClient(srv.port, cookie=cookie)
            try:
                assert authed.status == "HTTP/1.1 101 Switching Protocols", authed.status
            finally:
                authed.close()
        finally:
            for tid in list(server_state._terminal_sessions):
                if tid not in before:
                    server_state.unregister_terminal_session(tid)
            srv.stop()


# ---------------------------------------------------------------------------
# env_id: the same socket serving child environments
# ---------------------------------------------------------------------------


def test_env_id_local_is_the_default(server):
    """``env_id: "local"`` is what the frontend now always sends, and every
    pre-existing client sends nothing: both mean "PTY in this process"."""
    client = MuxClient(server.port)
    try:
        client.open("mux-env-local", env_id="local")
        client.opened("mux-env-local")
        client.input("mux-env-local", "echo ENV_LOCAL_MARKER\r")
        client.wait_for_output("mux-env-local", "ENV_LOCAL_MARKER")
    finally:
        client.close()


def test_unknown_environment_fails_only_its_channel(server):
    """A bad env_id answers the channel that asked for it.

    The socket and the tab's other terminals must survive: one environment that
    cannot be reached may never freeze a tab (that was the old bridge's failure
    mode -- a failed handshake took the whole terminal surface down).
    """
    client = MuxClient(server.port)
    try:
        client.open("mux-keep-alive")
        client.opened("mux-keep-alive")

        client.open("mux-bad-env", env_id="env-does-not-exist")
        err = client.wait_for(
            lambda m: m.get("op") == "error" and m.get("c") == "mux-bad-env",
            note="error frame for the unknown environment",
        )
        assert "unreachable" in err.get("error", ""), err

        # Same socket, other channel: still a working shell.
        client.input("mux-keep-alive", "echo STILL_HERE\r")
        client.wait_for_output("mux-keep-alive", "STILL_HERE")

        # Retryable: an env that becomes reachable later can be asked for again.
        client.open("mux-bad-env", env_id="env-does-not-exist")
        client.wait_for(
            lambda m: m.get("op") == "error" and m.get("c") == "mux-bad-env",
            note="second error frame",
        )
    finally:
        client.close()


def test_env_channels_count_toward_the_socket_cap(server):
    """Remote channels are channels: the per-socket limit covers them too.

    The dial is blocked so the channels stay pending -- otherwise the failed
    handshakes would retire them and the ledger would look empty.
    """
    import runtime.handler_tunnel as handler_tunnel
    from runtime.terminal_env_bridge import EnvBridgeError

    release = threading.Event()

    def blocking_dial(self, env_id):
        release.wait(timeout=15)
        raise EnvBridgeError("dial blocked by the test")

    try:
        # The socket (and its bridge) must be created INSIDE the patch: the
        # bridge captures the dial function when the socket is established.
        with patch.object(handler_tunnel.HandlerTunnelMixin, "_mux_env_transport", blocking_dial):
            client = MuxClient(server.port)
            for i in range(handler_base._MAX_MUX_CHANNELS):
                client.open(f"mux-cap-{i}", env_id="env-slow")
            client.open("mux-cap-over", env_id="env-slow")
            err = client.wait_for(
                lambda m: m.get("op") == "error" and m.get("c") == "mux-cap-over"
                and "too many" in m.get("error", ""),
                note="channel cap rejecting the remote channel",
            )
            assert err, err
    finally:
        release.set()
        client.close()


def test_switching_a_channel_between_local_and_an_environment(server):
    """A session's environment binding changes -> the same terminal id is
    re-opened; the old attach is released and the PTY session survives."""
    client = MuxClient(server.port)
    try:
        client.open("mux-switch")
        client.opened("mux-switch")
        client.input("mux-switch", "echo SWITCH_FIRST\r")
        client.wait_for_output("mux-switch", "SWITCH_FIRST")

        client.open("mux-switch", env_id="env-does-not-exist")
        err = client.wait_for(
            lambda m: m.get("op") == "error" and m.get("c") == "mux-switch",
            note="environment error after the switch",
        )
        assert "unreachable" in err.get("error", ""), err

        # Back to local: the same terminal session is re-attached.
        client.open("mux-switch", env_id="local")
        client.opened("mux-switch")
        client.input("mux-switch", "echo SWITCH_SECOND\r")
        client.wait_for_output("mux-switch", "SWITCH_SECOND")
    finally:
        client.close()
