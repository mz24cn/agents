"""Minimal outbound WebSocket client (RFC 6455), standard library only.

Used by the tunnel client for two purposes:

* dialing the parent environment's tunnel data channel
  (``ws(s)://parent/v1/tunnel/ws?token=...``), and
* dialing this environment's own WebSocket endpoints over loopback
  (e.g. ``/v1/terminals/ws``) when bridging a tunnel stream to a local
  WebSocket endpoint.

Outbound frames are always masked (RFC 6455 §5.3); inbound frames from a
server are expected unmasked but a mask bit is tolerated.
"""

from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
import threading
import urllib.parse
from typing import Optional, Tuple

from runtime.wsutil import (
    OP_BINARY,
    OP_CLOSE,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    ws_accept_key,
    ws_recv_frame,
    ws_send_frame,
)


class WSClientError(Exception):
    """Handshake or protocol failure on an outbound WebSocket connection."""


class WSClient:
    """A single outbound WebSocket connection.

    Usage::

        ws = WSClient.connect("ws://127.0.0.1:7988/v1/terminals/ws?terminal_id=x")
        frame = ws.recv()            # (opcode, payload) or None on close/EOF
        ws.send_text("...")
        ws.close()
    """

    def __init__(self, sock: socket.socket, url: str) -> None:
        self._sock = sock
        self.url = url
        self._closed = False
        # Multiple threads may write to the same connection (e.g. the tunnel
        # reader pongs while a worker answers a req frame); serialize frame
        # writes so they never interleave on the wire.
        self._send_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    @classmethod
    def connect(
        cls,
        url: str,
        timeout: float = 15.0,
        extra_headers: Optional[dict] = None,
    ) -> "WSClient":
        """Dial *url* (``ws://`` / ``wss://``) and perform the handshake."""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise WSClientError(f"Unsupported WebSocket URL scheme: {parsed.scheme!r}")
        if not parsed.netloc:
            raise WSClientError(f"WebSocket URL has no host: {url!r}")
        try:
            port = parsed.port
        except ValueError:
            raise WSClientError(f"Invalid port in WebSocket URL: {url!r}") from None
        host = parsed.hostname
        default_port = 443 if parsed.scheme == "wss" else 80
        port = port if port is not None else default_port
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise WSClientError(f"Cannot connect to {host}:{port}: {exc}") from exc

        if parsed.scheme == "wss":
            try:
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(sock, server_hostname=host)
            except ssl.SSLCertVerificationError as exc:
                # LAN deployments frequently self-sign; retry once without
                # verification so the tunnel stays usable, at the cost of the
                # hostname/certificate check for this connection.
                import logging
                logging.getLogger("runtime.wsclient").warning(
                    "TLS certificate verification failed for %s (%s); "
                    "retrying without verification", host, exc,
                )
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                try:
                    sock = ctx.wrap_socket(sock, server_hostname=host)
                except OSError as exc2:
                    raise WSClientError(f"TLS handshake failed: {exc2}") from exc2
            sock.settimeout(timeout)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        for name, value in (extra_headers or {}).items():
            lines.append(f"{name}: {value}")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
        try:
            sock.sendall(request)
        except OSError as exc:
            sock.close()
            raise WSClientError(f"Handshake send failed: {exc}") from exc

        # Read the response head (up to the blank line), guarding against a
        # pathologically large header block.
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise WSClientError("Connection closed during handshake")
            buf += chunk
            if len(buf) > 65536:
                sock.close()
                raise WSClientError("WebSocket handshake response too large")
        head, leftover = buf.split(b"\r\n\r\n", 1)
        try:
            status_line, _, header_block = head.partition(b"\r\n")
            status_parts = status_line.decode("latin-1").split(None, 2)
            status_code = int(status_parts[1])
        except (IndexError, ValueError) as exc:
            sock.close()
            raise WSClientError(f"Malformed handshake response: {status_line!r}") from exc
        if status_code != 101:
            sock.close()
            raise WSClientError(f"Handshake rejected with status {status_code}")
        accept = ""
        for line in header_block.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"sec-websocket-accept":
                accept = value.strip().decode("latin-1")
        if accept != ws_accept_key(key):
            sock.close()
            raise WSClientError("Sec-WebSocket-Accept mismatch")

        client = cls(sock, url)
        # Handshake done — switch to an idle-appropriate timeout: recv() uses
        # it as the liveness bound (the caller treats timeout as disconnect).
        return client

    # ------------------------------------------------------------------
    # Frame I/O
    # ------------------------------------------------------------------

    def set_timeout(self, timeout: Optional[float]) -> None:
        try:
            self._sock.settimeout(timeout)
        except OSError:
            pass

    def send_text(self, text: str) -> None:
        with self._send_lock:
            ws_send_frame(self._sock, text.encode("utf-8", errors="replace"), OP_TEXT, mask=True)

    def send_binary(self, data: bytes) -> None:
        with self._send_lock:
            ws_send_frame(self._sock, data, OP_BINARY, mask=True)

    def send_close(self, code: int = 1000) -> None:
        if self._closed:
            return
        try:
            with self._send_lock:
                ws_send_frame(self._sock, struct.pack("!H", code & 0xFFFF), OP_CLOSE, mask=True)
        except OSError:
            pass
        self._closed = True

    def send_pong(self, payload: bytes = b"") -> None:
        with self._send_lock:
            ws_send_frame(self._sock, payload, OP_PONG, mask=True)

    def recv(self) -> Optional[Tuple[int, bytes]]:
        """Receive one frame: ``(opcode, payload)`` or ``None`` on close/EOF.

        Raises ``socket.timeout`` when no frame arrives within the socket
        timeout (the caller treats it as a dead connection).
        """
        if self._closed:
            return None
        frame = ws_recv_frame(self._sock, expect_masked=False)
        if frame is None:
            self._closed = True
            return None
        return frame

    def close(self) -> None:
        if self._closed:
            return
        self.send_close()
        try:
            self._sock.close()
        except OSError:
            pass
        self._closed = True
