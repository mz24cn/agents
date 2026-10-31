"""Fan-out of one browser terminal socket to child environments.

A browser tab keeps exactly **one** terminal WebSocket
(``WS /v1/terminals/ws``, see docs/plan-connection-budget.md): a WebSocket
counts against the ~6 HTTP/1.1 connections the browser allows per origin, so
"one socket per terminal" -- and just as much "one socket per environment" --
leaves the tab's ordinary requests (file panel, navigation, the JSON API)
queued behind sockets that never free up.  Routing therefore happens inside the
protocol instead of inside the connection count: every ``open`` frame carries
an ``env_id``.

* ``"local"`` (default) -- the PTY lives in this process.
* any other id          -- a registered child environment.  The **parent** dials
  that child's own mux socket and carries every channel this tab holds in that
  env over one connection, opened on the first such channel.

Forwarding is verbatim: a child frame already carries its channel id, so the
parent stays a router rather than a protocol translator.

Two rules keep the shared socket trustworthy:

1. **A child failure never kills the socket.**  An unreachable env answers with
   an ``error`` frame on the affected channels only; the tab's other terminals
   (local or in another env) keep running.
2. **An idle terminal is not a dead one.**  Nothing here times a link out for
   lack of traffic -- a PTY sits quiet for minutes while nobody types.
   Teardown comes from a channel closing, the browser going away, or a real EOF
   from the child.
"""

from __future__ import annotations

import json
import threading
from typing import Callable, Optional

# ``env_id`` value for terminals served by this process.  The registry can never
# hold it: environment ids are ``env_*`` / ``tunnel:*``.
LOCAL_ENV_ID = "local"

# Frames buffered for a child whose handshake is still in flight.  Only control
# frames land here (a user typing into a terminal that is still connecting);
# past the cap they are dropped rather than held for a whole session.
PENDING_MAX = 64

# How long the pump waits for the next child frame before looping.  Purely a
# wakeup granularity: exceeding it says nothing about the child being alive, so
# it must never end the link.
POLL_SECONDS = 30.0

# Timeout of the parent -> child WebSocket handshake.
DIAL_TIMEOUT = 15.0


class EnvBridgeError(Exception):
    """A child environment could not be reached (message is browser-safe)."""


# ---------------------------------------------------------------------------
# Transports: the two ways a parent reaches a child's mux socket
# ---------------------------------------------------------------------------

class WSCTransport:
    """A WebSocket the parent dialed itself (direct ``http(s)://`` child)."""

    def __init__(self, client) -> None:
        from runtime.wsutil import OP_PING

        self._client = client
        self._ping = OP_PING

    def send_text(self, text: str) -> None:
        self._client.send_text(text)

    def recv_text(self, timeout: float = POLL_SECONDS) -> Optional[str]:
        # ``WSClient.recv`` blocks until a frame arrives or the peer is gone;
        # protocol-level pings are answered here, which is what keeps a quiet
        # link alive.
        while True:
            frame = self._client.recv()
            if frame is None:
                return None
            opcode, data = frame
            if opcode == self._ping:
                self._client.send_pong(data)
                continue
            return data.decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


class TunnelTransport:
    """A stream over the child's reverse WS tunnel (NAT-ed / firewalled child)."""

    def __init__(self, stream) -> None:
        self._stream = stream

    def send_text(self, text: str) -> None:
        self._stream.send(text.encode("utf-8"))

    def recv_text(self, timeout: float = POLL_SECONDS) -> Optional[str]:
        while True:
            item = self._stream.recv(timeout=timeout)
            if item is not None:
                _frame_type, data = item
                return data.decode("utf-8", errors="replace")
            # ``recv`` answers None both for "nothing yet" and for EOF, and a
            # terminal goes quiet for minutes: only the stream saying it is
            # closed may end the link.
            if getattr(self._stream, "closed", False):
                return None

    def close(self) -> None:
        try:
            self._stream.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The bridge itself (one instance per browser mux socket)
# ---------------------------------------------------------------------------

class _Link:
    """One parent -> child connection, shared by this tab's channels in that env."""

    __slots__ = ("env_id", "state", "transport", "send_lock", "pending", "channels")

    def __init__(self, env_id: str) -> None:
        self.env_id = env_id
        # connecting -> open -> dead.  Dead links leave the table, so a later
        # channel for the same env re-dials instead of reusing a half-closed
        # connection.
        self.state = "connecting"
        self.transport = None
        # Serializes writes to the child: the socket loop forwards input frames
        # while the handshake worker flushes buffered opens.
        self.send_lock = threading.RLock()
        self.pending: list[str] = []
        self.channels: set[str] = set()


class EnvMuxBridge:
    """Route the remote channels of one browser socket to their environments.

    ``dial(env_id)`` returns a transport (WSCTransport / TunnelTransport) or
    raises :class:`EnvBridgeError`; it runs on a worker thread so a slow or
    firewalled child never stalls the tab's socket loop.  ``send_text(text)``
    pushes one serialized frame to the browser and must be thread-safe.
    ``on_channel_closed(channel, error)`` is called when a channel's child link
    is gone -- ``error`` set means unreachable, ``None`` a clean EOF -- so the
    socket loop can retire its own bookkeeping for that channel.
    """

    def __init__(self, dial: Callable[[str], object],
                 send_text: Callable[[str], None],
                 on_channel_closed: Optional[Callable[[str, Optional[str]], None]] = None) -> None:
        self._dial = dial
        self._send_text = send_text
        self._on_channel_closed = on_channel_closed
        self._lock = threading.Lock()
        self._links: dict[str, _Link] = {}
        self._route: dict[str, str] = {}
        self._closed = False

    # -- channel lifecycle ------------------------------------------------

    def open(self, env_id: str, channel: str, *, cols: int, rows: int,
             workspace: str = "") -> None:
        """Attach ``channel`` to ``env_id`` and send its ``open`` frame there."""
        payload = {"op": "open", "c": channel, "cols": cols, "rows": rows}
        if workspace:
            payload["workspace"] = workspace
        text = json.dumps(payload, ensure_ascii=False)
        work: list[tuple] = []

        with self._lock:
            if self._closed:
                return
            previous = self._route.get(channel)
            if previous is not None and previous != env_id:
                # Same terminal id retargeted (the session switched
                # environments): release it on the old child first.
                self._drop_channel_locked(self._links.get(previous), channel, work)
            self._route[channel] = env_id
            link = self._links.get(env_id)
            fresh = link is None
            if fresh:
                link = _Link(env_id)
                self._links[env_id] = link
            link.channels.add(channel)
            if link.state == "open":
                work.append(("send", link, text))
            elif link.state == "connecting" and len(link.pending) < PENDING_MAX:
                link.pending.append(text)
        # Written after ``self._lock`` is released: a child that blocks a write
        # (full PTY pipe, stalled tunnel) must not stall the whole bridge.
        self._flush(work)
        if fresh:
            threading.Thread(target=self._run_link, args=(link,),
                             name=f"term-env-{env_id[:24]}", daemon=True).start()

    def forward(self, channel: str, payload: dict) -> bool:
        """Relay an ``input``/``resize`` frame to the child.

        Returns False when the channel has no live env (detached, or its child
        link died), so the caller can retire it on its side too.
        """
        text = json.dumps(payload, ensure_ascii=False)
        send = None
        with self._lock:
            env_id = self._route.get(channel)
            link = self._links.get(env_id) if env_id else None
            if link is None or link.state == "dead":
                return False
            if link.state == "connecting":
                if len(link.pending) < PENDING_MAX:
                    link.pending.append(text)
                return True
            send = (link, text)
        self._send_to_child(*send)
        return True

    def close(self, channel: str) -> None:
        """Detach one channel; tear the child link down with its last channel."""
        work: list[tuple] = []
        with self._lock:
            env_id = self._route.pop(channel, None)
            if env_id is None:
                return
            self._drop_channel_locked(self._links.get(env_id), channel, work)
        self._flush(work)

    def shutdown(self) -> None:
        """Drop every child link (the browser socket went away)."""
        with self._lock:
            self._closed = True
            links, self._links = list(self._links.values()), {}
            self._route.clear()
            for link in links:
                link.state = "dead"
                link.channels.clear()
        for link in links:
            self._hard_close(link)

    def env_ids(self) -> list[str]:
        """Envs this socket currently holds a child connection for (tests)."""
        with self._lock:
            return sorted(self._links)

    # -- internals --------------------------------------------------------

    def _drop_channel_locked(self, link: Optional[_Link], channel: str, work: list) -> None:
        """Caller holds ``self._lock``.  Close one channel on its child env.

        Nothing is written here -- the work goes on ``work`` and runs once the
        lock is released, so a child that blocks a write cannot hold up every
        other environment of the tab.
        """
        if link is None:
            return
        link.channels.discard(channel)
        close_frame = json.dumps({"op": "close", "c": channel})
        if link.state == "open":
            work.append(("send", link, close_frame))
        elif link.state == "connecting" and len(link.pending) < PENDING_MAX:
            # Kept in order behind the buffered open, so the child sees a
            # balanced open/close pair rather than a stray close.
            link.pending.append(close_frame)
        if not link.channels and self._links.get(link.env_id) is link:
            # Refcount hit zero: the child keeps each PTY alive for a later
            # re-attach, but holding the connection is not worth it.
            del self._links[link.env_id]
            link.state = "dead"
            work.append(("close", link, None))

    def _flush(self, work: list) -> None:
        for kind, link, text in work:
            if kind == "send":
                self._send_to_child(link, text)
            else:
                self._hard_close(link)

    def _send_to_child(self, link: _Link, text: str) -> None:
        """Write one frame, serialized per link (never reorders the stream)."""
        with link.send_lock:
            transport = link.transport
            if transport is None:
                return
            try:
                transport.send_text(text)
                return
            except Exception as exc:
                link.state = "dead"
                reason = str(exc) or exc.__class__.__name__
        # A write that fails means the child link is gone.  Don't wait for the
        # pump to notice: a quiet terminal prints nothing, so the tab's channels
        # would stay attached to a dead link until someone typed.
        self._retire(link, error=f"child link failed: {reason}")

    def _hard_close(self, link: _Link) -> None:
        transport, link.transport = link.transport, None
        if transport is not None:
            transport.close()

    def _run_link(self, link: _Link) -> None:
        try:
            transport = self._dial(link.env_id)
        except Exception as exc:
            self._retire(link, error=str(exc) or exc.__class__.__name__)
            return

        with self._lock:
            if self._closed or self._links.get(link.env_id) is not link:
                transport.close()
                return
            link.transport = transport
            queued, link.pending = link.pending, []
            link.state = "open"
        # Buffered opens (and any input typed while the handshake was running)
        # go out first, holding the send lock for the batch so a frame from the
        # socket loop cannot overtake them.
        failure = ""
        with link.send_lock:
            for text in queued:
                try:
                    transport.send_text(text)
                except Exception as exc:
                    link.state = "dead"
                    failure = str(exc) or exc.__class__.__name__
                    break
        if link.state == "dead":
            self._retire(link, error=f"child link failed: {failure}")
            return

        try:
            while link.state == "open":
                text = transport.recv_text(timeout=POLL_SECONDS)
                if text is None:
                    break
                self._send_text(text)
        except Exception:
            pass
        self._retire(link, error=None)

    def _retire(self, link: _Link, *, error: Optional[str]) -> None:
        """Report a dead child link on its channels -- and only those."""
        with self._lock:
            if self._links.get(link.env_id) is link:
                del self._links[link.env_id]
            channels, link.channels = sorted(link.channels), set()
            for channel in channels:
                if self._route.get(channel) == link.env_id:
                    del self._route[channel]
            link.state = "dead"
        self._hard_close(link)
        for channel in channels:
            if self._on_channel_closed is not None:
                try:
                    self._on_channel_closed(channel, error)
                except Exception:
                    pass


__all__ = [
    "DIAL_TIMEOUT",
    "EnvBridgeError",
    "EnvMuxBridge",
    "LOCAL_ENV_ID",
    "POLL_SECONDS",
    "TunnelTransport",
    "WSCTransport",
]
