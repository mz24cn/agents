"""TunnelManager — parent-side registry of child tunnel connections.

Each connected child holds one WebSocket (dialed by the child toward
``/v1/tunnel/ws``).  The manager offers:

* ``attach`` / ``detach`` — bind a handshake-completed socket to a tunnel
  id (rejecting ids with no remote-env record, replacing duplicate
  connections of the same id),
* ``call_env`` — one HTTP request/response round-trip over the tunnel
  (header frame + streaming binary body, empty binary frame as body
  terminator),
* ``open_stream_env`` — open a bidirectional byte stream to one of the
  child's local WebSocket endpoints (terminal bridging),
* ``send_deregister`` — tell an online child it has been removed.

A lazily-started sweeper thread pings idle connections (PING_INTERVAL) and
force-closes ones silent for longer than STALE_AFTER.

Trust boundary: the tunnel endpoints (``/v1/tunnel/*``,
``/v1/tunnel-proxy/*``) sit behind the same ``/v1/`` authorization gate as
every other API. When the parent has auth enabled, children authenticate
with the token carried in their ``SETUP_SOURCE`` link. When the parent has
no password set (auth disabled), the gate passes everything: any client
that can reach the port may register a tunnel and proxy requests to the
child's local endpoints. Do not expose such a parent to untrusted networks.

Zero third-party dependencies — only Python standard library.
"""

from __future__ import annotations

import base64
import logging
import queue
import threading
import time
import uuid
from typing import Optional, Tuple

from runtime.tunnel_protocol import (
    DEFAULT_CALL_TIMEOUT,
    INFLIGHT_MAX,
    OP_DEREGISTER,
    OP_PING,
    OP_PONG,
    OP_REQ,
    OP_RESP,
    OP_REPLACED,
    OP_STREAM_CLOSE,
    OP_STREAM_DATA,
    OP_STREAM_ERROR,
    OP_STREAM_OPEN,
    OP_STREAM_READY,
    OP_WELCOME,
    PING_INTERVAL,
    REJECT_NOT_REGISTERED,
    STALE_AFTER,
    decode_frame,
    encode_frame,
    iter_chunks,
    tunnel_env_id,
    tunnel_id_from_env_id,
)
from runtime import wsutil

logger = logging.getLogger("runtime.tunnel_manager")


class TunnelError(Exception):
    """Base error for tunnel operations."""


class TunnelOfflineError(TunnelError):
    """The child's tunnel connection is not available."""


class TunnelTimeoutError(TunnelError):
    """A tunnel operation did not complete in time."""


class _Pending:
    __slots__ = ("event", "result", "error", "_resp_header", "_body_parts", "_stream")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Optional[Tuple[int, dict, bytes]] = None
        self.error: Optional[Exception] = None
        self._resp_header: Optional[Tuple[int, dict]] = None
        self._body_parts: Optional[list] = None
        # Streaming variant: chunk queue (bytes), None = EOF.
        self._stream: Optional["queue.Queue"] = None


class _TunnelStream:
    """One bridged child WebSocket endpoint (e.g. a terminal)."""

    def __init__(self, conn: "_TunnelConn", sid: str) -> None:
        self.conn = conn
        self.sid = sid
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._ready_event = threading.Event()
        self._ready_status: int = 0
        self._closed = False

    # -- child → parent direction (filled by the reader loop) ------------

    def mark_ready(self, status: int) -> None:
        self._ready_status = status
        self._ready_event.set()

    def feed(self, frame_type: str, data: bytes) -> None:
        if not self._closed:
            self._queue.put((frame_type, data))

    def mark_remote_closed(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put(None)

    def fail(self) -> None:
        if not self._closed:
            self._closed = True
            self._ready_event.set()
            self._queue.put(None)

    # -- parent → child direction (consumed by bridge endpoints) ---------

    def send(self, data: bytes) -> None:
        if self._closed or not data:
            return
        self.conn.send_json({
            "op": OP_STREAM_DATA,
            "sid": self.sid,
            "data": base64.b64encode(data).decode("ascii"),
        })

    def recv(self, timeout: Optional[float] = 5.0) -> Optional[Tuple[str, bytes]]:
        """Next child→parent chunk as ``(frame_type, bytes)``; None on EOF/close.

        frame_type is "text" or "bin" — the bridge must preserve the local
        WebSocket frame type (the terminal protocol speaks text frames).
        """
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        return item

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.conn.send_json({"op": OP_STREAM_CLOSE, "sid": self.sid})
        except TunnelError:
            pass
        self._queue.put(None)


class _TunnelConn:
    """State of one child tunnel connection (one WS socket)."""

    def __init__(self, sock, tunnel_id: str) -> None:
        self.sock = sock
        self.tunnel_id = tunnel_id
        self._send_lock = threading.Lock()
        self._in_flight: dict[str, _Pending] = {}
        self._in_flight_lock = threading.Lock()
        self._streams: dict[str, _TunnelStream] = {}
        self._streams_lock = threading.Lock()
        self.active_body_id: Optional[str] = None  # reader-loop-local
        self.last_frame_at = time.monotonic()
        self.last_ping_at = 0.0
        self.closed = False

    # ------------------------------------------------------------------
    # Sending (any thread)
    # ------------------------------------------------------------------

    def send_frame(self, opcode: int, data: bytes) -> None:
        with self._send_lock:
            if self.closed:
                raise TunnelOfflineError("tunnel connection closed")
            wsutil.ws_send_frame(self.sock, data, opcode)

    def send_frames(self, frames: list, refresh_activity: bool = False) -> None:
        """Atomically send a sequence of frames (one lock acquisition).

        A request's header + body chunks + terminator must reach the child
        contiguously: the child reads request bodies inline on its reader
        thread and has no per-frame routing, so interleaving a second
        request's frames mid-body corrupts both requests.

        ``refresh_activity=True`` (request bodies only) keeps ``last_frame_at``
        fresh while a long body is being sent, so the sweeper does not treat
        an in-flight upload as a stale, dead connection.  Control frames
        (pings) must NOT refresh it, or a silent peer could stay "alive"
        forever on outbound traffic alone.
        """
        with self._send_lock:
            if self.closed:
                raise TunnelOfflineError("tunnel connection closed")
            for opcode, data in frames:
                wsutil.ws_send_frame(self.sock, data, opcode)
                if refresh_activity:
                    self.last_frame_at = time.monotonic()

    def send_json(self, obj: dict) -> None:
        self.send_frame(wsutil.OP_TEXT, encode_frame(obj))

    def try_ping(self, timeout: float = 2.0) -> bool:
        """Send one liveness ping without blocking on an in-flight body send.

        Returns False when the send lock is busy (a long request body is
        being uploaded — the connection is active by definition) or the send
        failed.  The sweeper is a single thread for all connections and must
        not stall behind a multi-minute upload of one connection.
        """
        if not self._send_lock.acquire(timeout=timeout):
            return False
        try:
            if self.closed:
                return False
            wsutil.ws_send_frame(self.sock, b"", wsutil.OP_PING)
            return True
        except OSError:
            return False
        finally:
            self._send_lock.release()

    def send_request(self, method: str, path: str, headers: dict,
                     body: Optional[bytes], rid: str) -> None:
        """Send one complete ``req`` frame sequence (header + body +
        terminator) atomically."""
        frames = [(
            wsutil.OP_TEXT,
            encode_frame({
                "op": OP_REQ,
                "id": rid,
                "method": method,
                "path": path,
                "headers": headers,
            }),
        )]
        if body:
            frames.extend((wsutil.OP_BINARY, chunk) for chunk in iter_chunks(body))
        frames.append((wsutil.OP_BINARY, b""))  # body terminator
        self.send_frames(frames, refresh_activity=True)

    # ------------------------------------------------------------------
    # HTTP round-trips
    # ------------------------------------------------------------------

    def call(
        self,
        method: str,
        path: str,
        headers: dict,
        body: Optional[bytes],
        timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> Tuple[int, dict, bytes]:
        rid = uuid.uuid4().hex
        pending = _Pending()
        with self._in_flight_lock:
            if self.closed:
                raise TunnelOfflineError("tunnel connection closed")
            if len(self._in_flight) >= INFLIGHT_MAX:
                raise TunnelError("too many in-flight tunnel calls")
            self._in_flight[rid] = pending
        try:
            self.send_request(method, path, headers, body, rid)
        except (OSError, TunnelError) as exc:
            with self._in_flight_lock:
                self._in_flight.pop(rid, None)
            if isinstance(exc, TunnelError):
                raise
            raise TunnelOfflineError(f"tunnel send failed: {exc}") from exc

        finished = pending.event.wait(timeout)
        with self._in_flight_lock:
            self._in_flight.pop(rid, None)
        if not finished or pending.error is not None:
            raise pending.error or TunnelTimeoutError(
                f"tunnel call timed out after {timeout:.0f}s"
            )
        return pending.result  # type: ignore[return-value]

    def call_stream(
        self,
        method: str,
        path: str,
        headers: dict,
        body: Optional[bytes],
        timeout: float = DEFAULT_CALL_TIMEOUT,
    ):
        """Like :meth:`call` but the response body is a chunk iterator
        (yields bytes; stops at EOF) instead of one assembled bytes —
        used by the browser bridge so large downloads do not double
        memory on the parent."""
        rid = uuid.uuid4().hex
        pending = _Pending()
        stream_q: "queue.Queue" = queue.Queue()
        pending._stream = stream_q
        with self._in_flight_lock:
            if self.closed:
                raise TunnelOfflineError("tunnel connection closed")
            if len(self._in_flight) >= INFLIGHT_MAX:
                raise TunnelError("too many in-flight tunnel calls")
            self._in_flight[rid] = pending
        try:
            self.send_request(method, path, headers, body, rid)
        except (OSError, TunnelError) as exc:
            with self._in_flight_lock:
                self._in_flight.pop(rid, None)
            if isinstance(exc, TunnelError):
                raise
            raise TunnelOfflineError(f"tunnel send failed: {exc}") from exc

        finished = pending.event.wait(timeout)
        with self._in_flight_lock:
            self._in_flight.pop(rid, None)
        if not finished or pending.error is not None:
            raise pending.error or TunnelTimeoutError(
                f"tunnel call timed out after {timeout:.0f}s"
            )
        status, hdrs = pending.result  # type: ignore[misc]
        return status, hdrs, stream_q

    # ------------------------------------------------------------------
    # Reader-loop completions
    # ------------------------------------------------------------------

    def complete_resp(self, header: dict) -> None:
        rid = str(header.get("id", ""))
        status = int(header.get("status", 0)) or 0
        hdrs = header.get("headers") or {}
        with self._in_flight_lock:
            pending = self._in_flight.get(rid)
        if pending is None:
            return
        pending._resp_header = (status, hdrs)  # type: ignore[attr-defined]

    def feed_body(self, rid: str, chunk: bytes) -> None:
        with self._in_flight_lock:
            pending = self._in_flight.get(rid)
        if pending is None:
            return
        if pending._stream is not None:
            if chunk:
                pending._stream.put(chunk)
            return
        parts = pending._body_parts
        if parts is None:
            parts = []
            pending._body_parts = parts
        parts.append(chunk)

    def finalize_body(self, rid: str) -> None:
        with self._in_flight_lock:
            pending = self._in_flight.get(rid)
        if pending is None:
            return
        header = pending._resp_header
        if header is None:
            return
        if pending._stream is not None:
            pending._stream.put(None)  # EOF sentinel
            pending.result = header  # type: ignore[assignment]
            pending.event.set()
            return
        status, hdrs = header
        parts = pending._body_parts or []
        pending.result = (status, hdrs, b"".join(parts))
        pending.event.set()

    # ------------------------------------------------------------------
    # Streams
    # ------------------------------------------------------------------

    def open_stream(
        self, path: str, headers: dict, timeout: float = 15.0,
    ) -> _TunnelStream:
        sid = uuid.uuid4().hex
        stream = _TunnelStream(self, sid)
        with self._streams_lock:
            if self.closed:
                raise TunnelOfflineError("tunnel connection closed")
            self._streams[sid] = stream
        try:
            self.send_json({"op": OP_STREAM_OPEN, "sid": sid, "path": path, "headers": headers})
        except (OSError, TunnelError) as exc:
            with self._streams_lock:
                self._streams.pop(sid, None)
            if isinstance(exc, TunnelError):
                raise
            raise TunnelOfflineError(f"tunnel send failed: {exc}") from exc
        if not stream._ready_event.wait(timeout):
            with self._streams_lock:
                self._streams.pop(sid, None)
            stream.fail()
            raise TunnelTimeoutError("stream open timed out")
        if stream._ready_status != 200:
            with self._streams_lock:
                self._streams.pop(sid, None)
            stream.fail()
            raise TunnelError(f"child refused stream open (status {stream._ready_status})")
        return stream

    def _stream(self, sid: str) -> Optional[_TunnelStream]:
        with self._streams_lock:
            return self._streams.get(str(sid))

    def complete_stream_open(self, frame: dict) -> None:
        stream = self._stream(frame.get("sid", ""))
        if stream is None:
            return
        if frame.get("op") == OP_STREAM_READY:
            stream.mark_ready(200)
        else:
            stream.mark_ready(int(frame.get("status", 500)) or 500)

    def feed_stream(self, frame: dict) -> None:
        stream = self._stream(frame.get("sid", ""))
        if stream is None:
            return
        try:
            data = base64.b64decode(frame.get("data", ""))
        except (ValueError, TypeError):
            return
        stream.feed(str(frame.get("t", "bin")), data)

    def close_stream_remote(self, sid: str) -> None:
        stream = self._stream(sid)
        if stream is None:
            return
        with self._streams_lock:
            self._streams.pop(str(sid), None)
        stream.mark_remote_closed()

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def abort_all(self) -> None:
        self.closed = True
        with self._in_flight_lock:
            pendings = list(self._in_flight.values())
            self._in_flight.clear()
        for pending in pendings:
            if pending.error is None:
                pending.error = TunnelOfflineError("tunnel connection closed")
            pending.event.set()
        with self._streams_lock:
            streams = list(self._streams.values())
            self._streams.clear()
        for stream in streams:
            stream.fail()

    def close_socket(self) -> None:
        try:
            wsutil.ws_send_close(self.sock)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class TunnelManager:
    """Parent-side registry of tunnel connections, keyed by tunnel id."""

    def __init__(self, remote_env_manager) -> None:
        self._envs = remote_env_manager
        self._lock = threading.Lock()
        self._conns: dict[str, _TunnelConn] = {}
        self._sweeper: Optional[threading.Thread] = None
        self._sweeper_stop = threading.Event()
        self._stopped = False

    # ------------------------------------------------------------------
    # Attach / detach
    # ------------------------------------------------------------------

    def attach(self, sock, tunnel_id: str) -> Tuple[str, Optional[str]]:
        """Bind *sock* (handshake done, hello received) to *tunnel_id*.

        Returns ``(action, env_id)`` with action ``"welcome"`` or
        ``"reject"``.  Rejects ids with no remote-env record — the child
        may only (re)connect after an explicit registration.
        """
        env_id = tunnel_env_id(tunnel_id)
        try:
            self._envs.get(env_id)
        except KeyError:
            return ("reject", None)
        if self._stopped:
            return ("reject", None)

        conn = _TunnelConn(sock, tunnel_id)
        old: Optional[_TunnelConn] = None
        with self._lock:
            old = self._conns.get(tunnel_id)
            self._conns[tunnel_id] = conn
        if old is not None:
            logger.info("Tunnel %s replaced a previous connection", tunnel_id)
            try:
                old.send_json({"op": OP_REPLACED})
            except TunnelError:
                pass
            old.abort_all()
            old.close_socket()
        try:
            self._envs.update_tunnel_status(env_id, True)
        except (KeyError, OSError):
            pass
        try:
            # 子端（重）连接：离线期间其工具清单可能已变化，失效代理的工具
            # 缓存，让下一次推理重新拉取。
            self._envs.invalidate_tools_cache(env_id)
        except AttributeError:
            pass
        try:
            conn.send_json({"op": OP_WELCOME, "env_id": env_id})
        except (TunnelError, OSError):
            with self._lock:
                if self._conns.get(tunnel_id) is conn:
                    del self._conns[tunnel_id]
            conn.abort_all()
            return ("reject", None)
        self._ensure_sweeper()
        return ("welcome", env_id)

    def detach(self, tunnel_id: str, conn: _TunnelConn) -> None:
        with self._lock:
            if self._conns.get(tunnel_id) is conn:
                del self._conns[tunnel_id]
                env_id = tunnel_env_id(tunnel_id)
                try:
                    self._envs.update_tunnel_status(env_id, False)
                except (KeyError, OSError):
                    pass
        conn.abort_all()

    # ------------------------------------------------------------------
    # Lookup / operations
    # ------------------------------------------------------------------

    def conn_for_env(self, env_id: str) -> Optional[_TunnelConn]:
        tunnel_id = tunnel_id_from_env_id(env_id)
        if tunnel_id is None:
            return None
        with self._lock:
            return self._conns.get(tunnel_id)

    def is_online(self, env_id: str) -> bool:
        return self.conn_for_env(env_id) is not None

    def call_env(
        self,
        env_id: str,
        method: str,
        path: str,
        headers: Optional[dict] = None,
        body: Optional[bytes] = None,
        timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> Tuple[int, dict, bytes]:
        conn = self.conn_for_env(env_id)
        if conn is None:
            raise TunnelOfflineError(f"tunnel environment is offline: {env_id}")
        return conn.call(method, path, headers or {}, body, timeout=timeout)

    def call_env_stream(
        self,
        env_id: str,
        method: str,
        path: str,
        headers: Optional[dict] = None,
        body: Optional[bytes] = None,
        timeout: float = DEFAULT_CALL_TIMEOUT,
    ):
        """Streaming variant of :meth:`call_env` (browser bridge)."""
        conn = self.conn_for_env(env_id)
        if conn is None:
            raise TunnelOfflineError(f"tunnel environment is offline: {env_id}")
        return conn.call_stream(method, path, headers or {}, body, timeout=timeout)

    def open_stream_env(
        self,
        env_id: str,
        path: str,
        headers: Optional[dict] = None,
        timeout: float = 15.0,
    ) -> _TunnelStream:
        conn = self.conn_for_env(env_id)
        if conn is None:
            raise TunnelOfflineError(f"tunnel environment is offline: {env_id}")
        return conn.open_stream(path, headers or {}, timeout=timeout)

    def send_deregister(self, env_id: str, timeout: float = 3.0) -> bool:
        """Best-effort: ask an online child to stop dialing.

        Returns True when the control frame was delivered (the child clears
        its enabled flag and closes the connection); False when the child is
        offline or the send failed (the record is deleted anyway — a late
        reconnect is rejected because the record no longer exists).
        """
        tunnel_id = tunnel_id_from_env_id(env_id)
        if tunnel_id is None:
            return False
        with self._lock:
            conn = self._conns.get(tunnel_id)
        if conn is None or conn.closed:
            return False
        try:
            conn.send_json({"op": OP_DEREGISTER})
        except (TunnelError, OSError):
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                still_current = self._conns.get(tunnel_id) is conn
            if not still_current:
                return True
            time.sleep(0.05)
        # The child may stay connected briefly (e.g. a long tool call in
        # flight); the frame was delivered, which is what matters.
        return True

    # ------------------------------------------------------------------
    # Sweeper (liveness pings + stale force-close)
    # ------------------------------------------------------------------

    def _ensure_sweeper(self) -> None:
        with self._lock:
            if self._sweeper is not None or self._stopped:
                return
            self._sweeper_stop.clear()
            self._sweeper = threading.Thread(
                target=self._sweep_loop, name="tunnel-sweeper", daemon=True
            )
            self._sweeper.start()

    def _sweep_loop(self) -> None:
        while not self._sweeper_stop.wait(5.0):
            now = time.monotonic()
            with self._lock:
                conns = list(self._conns.values())
            for conn in conns:
                if conn.closed:
                    continue
                if now - conn.last_frame_at > STALE_AFTER:
                    logger.info("Tunnel %s stale for %.0fs; closing", conn.tunnel_id, now - conn.last_frame_at)
                    conn.abort_all()
                    conn.close_socket()
                    continue
                if now - conn.last_ping_at >= PING_INTERVAL:
                    conn.last_ping_at = now
                    # try_ping does not block on the send lock: a connection
                    # in the middle of a long body upload is active and will
                    # be pinged on a later sweep.
                    conn.try_ping()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            conns = list(self._conns.values())
            self._conns.clear()
        self._sweeper_stop.set()
        for conn in conns:
            conn.abort_all()
            conn.close_socket()
