"""Minimal outbound WebSocket client (RFC 6455), standard library only.

Used by the tunnel client for two purposes:

* dialing the parent environment's tunnel data channel
  (``ws(s)://parent/v1/tunnel/ws?token=...``), and
* dialing this environment's own WebSocket endpoints over loopback
  (e.g. ``/v1/terminals/ws``) when bridging a tunnel stream to a local
  WebSocket endpoint.

Outbound frames are always masked (RFC 6455 §5.3); inbound frames from a
server are expected unmasked but a mask bit is tolerated.

An HTTP proxy is honoured when the standard environment variables are set
(``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``NO_PROXY``): the TCP connection is
opened with ``CONNECT host:port`` and the WebSocket handshake then runs **end
to end with the target**, so the proxy never sees the request path (and never
sees the tunnel token carried in the query string).  Loopback targets and
``NO_PROXY`` matches are always dialed directly.
"""

from __future__ import annotations

import base64
import ipaddress
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
    OP_PONG,
    OP_TEXT,
    ws_accept_key,
    ws_recv_frame,
    ws_send_frame,
)


class WSClientError(Exception):
    """Handshake or protocol failure on an outbound WebSocket connection."""


# ---------------------------------------------------------------------------
# Proxy resolution (HTTP CONNECT)
# ---------------------------------------------------------------------------

def _is_loopback(host: str) -> bool:
    """True for ``localhost`` / ``127.0.0.0/8`` / ``::1``.

    Loopback is never proxied: this client also dials *this* environment's own
    endpoints (e.g. ``/v1/terminals/ws``), and sending those to a proxy would
    silently reach some *other* machine's agent service instead.
    """
    name = (host or "").lower().rstrip(".")
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _split_host_port(entry: str) -> Tuple[str, Optional[int]]:
    """Split a ``NO_PROXY`` entry into host and optional port."""
    if entry.startswith("["):                       # [::1] or [::1]:8080
        host, _, rest = entry[1:].partition("]")
        rest = rest[1:] if rest.startswith(":") else ""
    else:
        host, sep, rest = entry.rpartition(":")
        if not sep:
            host, rest = entry, ""
    try:
        return host, int(rest) if rest else None
    except ValueError:
        return host, None


def _no_proxy_matches(host: str, port: int) -> bool:
    """Match ``host:port`` against ``NO_PROXY`` / ``no_proxy``.

    Understands ``*``, hostnames (a bare name also covers its subdomains, with
    or without a leading dot), IP literals, CIDR networks such as
    ``172.28.0.0/16``, and optional ``:port`` suffixes.
    """
    raw = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    if not raw:
        return False
    name = host.lower().rstrip(".")
    for entry in raw.replace(" ", "").split(","):
        entry = entry.lower()
        if not entry:
            continue
        if entry == "*":
            return True
        entry_host, entry_port = _split_host_port(entry)
        if not entry_host:
            continue
        if entry_port is not None and entry_port != port:
            continue
        if entry_host.startswith("."):
            if name == entry_host[1:] or name.endswith(entry_host):
                return True
        elif name == entry_host or name.endswith("." + entry_host):
            return True
        try:
            addr = ipaddress.ip_address(name)
        except ValueError:
            continue
        try:
            if "/" in entry_host:
                if addr in ipaddress.ip_network(entry_host, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry_host):
                return True
        except ValueError:
            continue
    return False


def _proxy_for(scheme: str, host: str, port: int) -> Optional[str]:
    """The HTTP proxy to dial *host:port* through, or ``None`` to go direct."""
    if not host or _is_loopback(host) or _no_proxy_matches(host, port):
        return None
    if scheme == "wss":
        names = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY")
    else:
        names = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY")
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if not value:
            continue
        if "://" not in value:
            value = "http://" + value               # bare host:port is common
        return value
    return None


def _connect_via_proxy(proxy_url: str, host: str, port: int,
                       timeout: float) -> Tuple[socket.socket, bytes]:
    """Open ``host:port`` through an HTTP proxy's ``CONNECT`` method.

    Returns the connected socket plus any bytes the proxy already forwarded
    (they belong to the target's stream and must not be lost).
    """
    parsed = urllib.parse.urlsplit(proxy_url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise WSClientError(f"Unsupported proxy scheme {parsed.scheme!r} in {proxy_url!r}")
    proxy_host = parsed.hostname
    if not proxy_host:
        raise WSClientError(f"Proxy URL has no host: {proxy_url!r}")
    proxy_port = parsed.port or (443 if scheme == "https" else 80)

    try:
        sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    except OSError as exc:
        raise WSClientError(
            f"Cannot connect to proxy {proxy_host}:{proxy_port} for {host}:{port}: {exc}"
        ) from exc
    if scheme == "https":
        try:
            sock = ssl.create_default_context().wrap_socket(
                sock, server_hostname=proxy_host)
        except (ssl.SSLError, OSError) as exc:
            sock.close()
            raise WSClientError(f"Proxy TLS handshake failed: {exc}") from exc

    lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
    if parsed.username:
        userinfo = (f"{urllib.parse.unquote(parsed.username)}:"
                    f"{urllib.parse.unquote(parsed.password or '')}")
        lines.append("Proxy-Authorization: Basic "
                     + base64.b64encode(userinfo.encode("utf-8")).decode("ascii"))
    try:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    except OSError as exc:
        sock.close()
        raise WSClientError(f"CONNECT request to proxy failed: {exc}") from exc

    buf = b""
    while b"\r\n\r\n" not in buf:
        try:
            chunk = sock.recv(4096)
        except OSError as exc:
            sock.close()
            raise WSClientError(f"CONNECT response failed: {exc}") from exc
        if not chunk:
            sock.close()
            raise WSClientError(f"Proxy closed the connection during CONNECT to {host}:{port}")
        buf += chunk
        if len(buf) > 65536:
            sock.close()
            raise WSClientError("CONNECT response too large")
    head, leftover = buf.split(b"\r\n\r\n", 1)
    status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = status_line.split(None, 2)
    try:
        status = int(parts[1])
    except (IndexError, ValueError) as exc:
        sock.close()
        raise WSClientError(f"Malformed CONNECT response: {status_line!r}") from exc
    if not 200 <= status < 300:
        sock.close()
        raise WSClientError(f"Proxy refused CONNECT {host}:{port}: {status_line}")

    sock.settimeout(timeout)
    return sock, leftover


class _PrefixedSocket:
    """Socket wrapper that replays already-buffered bytes before reading more.

    Two sources of buffered bytes exist on a proxied dial: whatever the proxy
    forwarded right after its ``CONNECT`` response, and whatever the target
    sent beyond the blank line ending the handshake response.  Both belong to
    the frame stream and must reach ``ws_recv_frame`` rather than be dropped.
    """

    __slots__ = ("_sock", "_buf")

    def __init__(self, sock: socket.socket, prefetch: bytes = b"") -> None:
        self._sock = sock
        self._buf = prefetch

    def push_front(self, data: bytes) -> None:
        if data:
            self._buf = data + self._buf

    def recv(self, count: int) -> bytes:
        if self._buf:
            chunk, self._buf = self._buf[:count], self._buf[count:]
            return chunk
        return self._sock.recv(count)

    def send(self, data: bytes) -> int:
        return self._sock.send(data)

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def __getattr__(self, name: str):        # settimeout / close / getpeername ...
        return getattr(self._sock, name)


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

        prefetch = b""
        proxy_url = _proxy_for(parsed.scheme, host, port)
        if proxy_url:
            sock, prefetch = _connect_via_proxy(proxy_url, host, port, timeout)
        else:
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

        # Bytes already handed over (proxy prefetch) must be read back before
        # the wire, so the handshake and the first frames stay in order.
        reader = _PrefixedSocket(sock, prefetch)

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
            reader.sendall(request)
        except OSError as exc:
            sock.close()
            raise WSClientError(f"Handshake send failed: {exc}") from exc

        # Read the response head (up to the blank line), guarding against a
        # pathologically large header block.
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = reader.recv(4096)
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

        # Anything the target sent past the handshake head is the start of the
        # frame stream (proxies glue the 101 response and the first frames).
        reader.push_front(leftover)
        client = cls(reader, url)
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
