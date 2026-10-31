"""Unit tests for runtime/terminal_env_bridge.py — the env fan-out of the
terminal mux.

A browser tab keeps ONE ``WS /v1/terminals/ws`` (docs/plan-connection-budget.md)
and every channel says which environment its PTY lives in.  These tests pin the
two invariants that make that safe:

* one parent -> child connection per (tab, environment), however many channels
  that tab has in the environment, and it goes away with the last one;
* a child that cannot be reached damages *its* channels only.

The transports are faked, so nothing here depends on a network; the end-to-end
paths (direct child, reverse tunnel) are covered in tests/test_remote_envs.py
and tests/test_tunnel.py.
"""

import json
import queue
import threading
import time

from runtime import wsutil
from runtime.terminal_env_bridge import (
    EnvBridgeError,
    EnvMuxBridge,
    POLL_SECONDS,
    TunnelTransport,
    WSCTransport,
)

_EOF = object()


class FakeTransport:
    """Stand-in for a parent -> child mux connection."""

    def __init__(self, env_id, log):
        self.env_id = env_id
        self.sent = []
        self.inbox: "queue.Queue" = queue.Queue()
        self.closed = False
        self.send_fails = False
        log.append(self)

    # -- bridge contract ------------------------------------------------

    def send_text(self, text):
        if self.send_fails:
            raise OSError("child link is gone")
        self.sent.append(json.loads(text))

    def recv_text(self, timeout=POLL_SECONDS):
        # Faithful to the real transports: silence is NOT EOF. Only a pushed
        # _EOF (child closed / link closed) ends the pump.
        while True:
            try:
                item = self.inbox.get(timeout=0.02)
            except queue.Empty:
                continue
            return None if item is _EOF else item

    def close(self):
        self.closed = True
        self.inbox.put(_EOF)

    # -- test helpers ---------------------------------------------------

    def ops(self):
        return [m.get("op") for m in self.sent]

    def frames(self):
        return self.sent


def _wait_until(pred, timeout=3.0, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {message}")


class Harness:
    """Bridge + recorded dials + frames sent to the browser."""

    def __init__(self, failing=()):
        self.dials = []            # every transport ever dialed
        self.links = {}            # env_id -> current transport
        self.to_browser = []
        self.closed = []           # (channel, error) reported by the bridge
        self.failing = set(failing)
        self.bridge = EnvMuxBridge(self._dial, self._send, self._on_closed)

    def _dial(self, env_id):
        if env_id in self.failing:
            raise EnvBridgeError(f"cannot reach child environment: {env_id}")
        transport = FakeTransport(env_id, self.dials)
        self.links[env_id] = transport
        return transport

    def _send(self, text):
        self.to_browser.append(json.loads(text))

    def _on_closed(self, channel, error):
        self.closed.append((channel, error))

    def link(self, env_id):
        return self.links.get(env_id)

    def dial_count(self, env_id=None):
        if env_id is None:
            return len(self.dials)
        return sum(1 for t in self.dials if t.env_id == env_id)

    def wait_dialed(self, env_id, count=1):
        _wait_until(lambda: self.dial_count(env_id) >= count,
                    message=f"env {env_id} dialed {count}x")

    def browser_ops_for(self, channel):
        return [m for m in self.to_browser if m.get("c") == channel]

    def shutdown(self):
        self.bridge.shutdown()
        for t in self.dials:
            t.inbox.put(_EOF)


# ---------------------------------------------------------------------------
# Connection ledger (the reason this module exists)
# ---------------------------------------------------------------------------


def test_several_channels_one_environment_one_connection():
    h = Harness()
    try:
        h.bridge.open("env-a", "sess-1", cols=80, rows=24)
        h.bridge.open("env-a", "sess-2", cols=100, rows=30)
        h.wait_dialed("env-a")
        _wait_until(lambda: len(h.link("env-a").ops()) == 2, message="both opens forwarded")

        assert h.dial_count("env-a") == 1, "one environment must cost one connection"
        assert h.bridge.env_ids() == ["env-a"]
        opens = h.link("env-a").frames()
        assert [f["c"] for f in opens] == ["sess-1", "sess-2"]
        assert opens[0]["cols"] == 80 and opens[1]["rows"] == 30
    finally:
        h.shutdown()


def test_each_environment_gets_its_own_connection_but_never_a_second_one():
    h = Harness()
    try:
        h.bridge.open("env-a", "sess-a1", cols=80, rows=24)
        h.bridge.open("env-a", "sess-a2", cols=80, rows=24)
        h.bridge.open("env-b", "sess-b", cols=80, rows=24)
        h.wait_dialed("env-a")
        h.wait_dialed("env-b")

        assert h.dial_count() == 2, f"expected one dial per env, got {h.dial_count()}"
        assert sorted(h.bridge.env_ids()) == ["env-a", "env-b"]
        # The browser still holds ONE socket: the bridge never opens one per env
        # towards it -- the env is a frame field, not a connection.
        assert h.link("env-a") is not h.link("env-b")
    finally:
        h.shutdown()


def test_frames_travel_verbatim_and_are_routed_by_channel():
    h = Harness()
    try:
        h.bridge.open("env-a", "sess-1", cols=80, rows=24)
        h.bridge.open("env-a", "sess-2", cols=80, rows=24)
        h.wait_dialed("env-a")
        _wait_until(lambda: len(h.link("env-a").ops()) == 2)

        # child -> browser: the bridge is a router, the channel id already
        # says where the frame belongs.
        h.link("env-a").inbox.put(json.dumps({"c": "sess-2", "d": "beta out"}))  # text frame
        _wait_until(lambda: any(m.get("d") == "beta out" for m in h.to_browser))

        # browser -> child: only the channel id and the payload, no env_id
        # (the child's own mux speaks the same protocol, untouched).
        assert h.bridge.forward("sess-2", {"op": "input", "c": "sess-2", "d": "ls\r"})
        _wait_until(lambda: any(
            f.get("op") == "input" and f.get("d") == "ls\r" for f in h.link("env-a").frames()
        ))
        sent = h.link("env-a").frames()
        assert all("env_id" not in f for f in sent), sent
    finally:
        h.shutdown()


def test_last_channel_close_releases_the_connection_and_reopen_redials():
    h = Harness()
    try:
        h.bridge.open("env-a", "sess-1", cols=80, rows=24)
        h.bridge.open("env-a", "sess-2", cols=80, rows=24)
        h.wait_dialed("env-a")
        first = h.link("env-a")

        h.bridge.close("sess-1")
        _wait_until(lambda: any(f.get("op") == "close" for f in first.frames()))
        assert not first.closed, "connection stays while a channel remains"

        h.bridge.close("sess-2")
        _wait_until(lambda: first.closed, message="connection torn down with last channel")
        assert h.bridge.env_ids() == []

        # A later terminal in the same env re-dials rather than reusing the
        # torn-down link.
        h.bridge.open("env-a", "sess-3", cols=80, rows=24)
        h.wait_dialed("env-a", count=2)
        assert h.link("env-a") is not first
    finally:
        h.shutdown()


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


def test_unreachable_environment_answers_on_its_channels_only():
    h = Harness(failing={"env-dead"})
    try:
        h.bridge.open("env-alive", "sess-alive", cols=80, rows=24)
        h.bridge.open("env-dead", "sess-dead", cols=80, rows=24)
        h.wait_dialed("env-alive")

        # The bridge's contract is the callback: the mux handler turns it into
        # an ``{"op":"error","c":...}`` frame (asserted end-to-end in
        # tests/test_remote_envs.py).
        _wait_until(lambda: any(c == "sess-dead" for c, _ in h.closed),
                    message="dead channel reported")
        channel, error = h.closed[0]
        assert channel == "sess-dead" and "cannot reach" in (error or "")
        assert not any(c == "sess-alive" for c, _ in h.closed), \
            "the healthy environment of the same tab must stay untouched"
        assert h.bridge.env_ids() == ["env-alive"]

        # And it is retryable: the record may gain a token in the meantime.
        h.failing.discard("env-dead")
        h.bridge.open("env-dead", "sess-dead", cols=80, rows=24)
        h.wait_dialed("env-dead")
    finally:
        h.shutdown()


def test_a_link_dying_mid_session_notifies_its_channels():
    h = Harness()
    try:
        h.bridge.open("env-a", "sess-1", cols=80, rows=24)
        h.wait_dialed("env-a")
        h.link("env-a").inbox.put(_EOF)  # child hung up

        _wait_until(lambda: any(c == "sess-1" and e is None for c, e in h.closed),
                    message="channel closed notification")
        assert h.bridge.env_ids() == []
        # Input on the dead channel is refused rather than silently queued.
        assert h.bridge.forward("sess-1", {"op": "input", "c": "sess-1", "d": "x"}) is False
    finally:
        h.shutdown()


def test_send_failure_ends_the_link():
    h = Harness()
    try:
        h.bridge.open("env-a", "sess-1", cols=80, rows=24)
        h.wait_dialed("env-a")
        h.link("env-a").send_fails = True
        h.bridge.forward("sess-1", {"op": "input", "c": "sess-1", "d": "x"})
        # A dead write is reported at once: waiting for the pump to notice would
        # mean waiting for the terminal to print something (it may never do so).
        _wait_until(lambda: any(c == "sess-1" and e for c, e in h.closed),
                    message="failed write to surface as a closed channel")
        assert h.bridge.env_ids() == []
    finally:
        h.shutdown()


def test_retargeting_a_channel_releases_it_on_the_old_environment():
    """A session switching environments must not leave a ghost channel behind."""
    h = Harness()
    try:
        h.bridge.open("env-a", "sess", cols=80, rows=24)
        h.bridge.open("env-b", "sess2", cols=80, rows=24)
        h.wait_dialed("env-a")
        h.wait_dialed("env-b")

        h.bridge.open("env-b", "sess", cols=80, rows=24)
        h.wait_dialed("env-b", count=1)
        _wait_until(lambda: any(
            f.get("op") == "close" and f.get("c") == "sess" for f in h.link("env-a").frames()
        ), message="close on the old env")
        _wait_until(lambda: any(
            f.get("op") == "open" and f.get("c") == "sess" for f in h.link("env-b").frames()
        ), message="open on the new env")

        # Only the new environment now receives keystrokes.
        h.bridge.forward("sess", {"op": "input", "c": "sess", "d": "pwd\r"})
        _wait_until(lambda: any(
            f.get("d") == "pwd\r" for f in h.link("env-b").frames()
        ))
        assert not any(f.get("d") == "pwd\r" for f in h.link("env-a").frames())
    finally:
        h.shutdown()


# ---------------------------------------------------------------------------
# Framing details
# ---------------------------------------------------------------------------


def test_frames_typed_while_the_child_is_still_being_dialed_keep_their_order():
    """The user may type before the handshake lands; the child must still see
    the open before the input."""
    release = threading.Event()
    h = Harness()

    def slow_dial(env_id):
        release.wait(timeout=5)
        return Harness._dial(h, env_id)

    h.bridge._dial = slow_dial
    try:
        h.bridge.open("env-a", "sess", cols=80, rows=24)
        h.bridge.forward("sess", {"op": "input", "c": "sess", "d": "ls\r"})
        release.set()
        h.wait_dialed("env-a")
        transport = h.link("env-a")
        _wait_until(lambda: len(transport.frames()) == 2, message="buffered frames flushed")
        assert [f["op"] for f in transport.frames()] == ["open", "input"]
    finally:
        h.shutdown()


def test_pending_buffer_is_bounded():
    """Keystrokes typed into a child that never answers must not pile up."""
    from runtime.terminal_env_bridge import PENDING_MAX
    h = Harness()
    release = threading.Event()
    h.bridge._dial = lambda env_id: (release.wait(timeout=5), Harness._dial(h, env_id))[1]
    try:
        h.bridge.open("env-a", "sess", cols=80, rows=24)
        for _ in range(PENDING_MAX + 50):
            h.bridge.forward("sess", {"op": "input", "c": "sess", "d": "x"})
        # Still accepting frames (True) but holding at most the cap + the open.
        assert h.bridge._links["env-a"].state == "connecting"
        assert len(h.bridge._links["env-a"].pending) <= PENDING_MAX + 1
    finally:
        release.set()
        h.shutdown()


def test_shutdown_drops_every_child_link():
    h = Harness()
    h.bridge.open("env-a", "sess-a", cols=80, rows=24)
    h.bridge.open("env-b", "sess-b", cols=80, rows=24)
    h.wait_dialed("env-a")
    h.wait_dialed("env-b")
    links = [h.link("env-a"), h.link("env-b")]

    h.bridge.shutdown()
    _wait_until(lambda: all(t.closed for t in links), message="child links closed")
    assert h.bridge.env_ids() == []
    # A channel opened after shutdown is a no-op (the socket is going away).
    h.bridge.open("env-a", "sess-late", cols=80, rows=24)
    assert h.bridge.env_ids() == []


# ---------------------------------------------------------------------------
# Transports: an idle terminal is not a dead one
# ---------------------------------------------------------------------------


class FakeTunnelStream:
    """Stand-in for ``_TunnelStream``: ``recv`` answers None for "nothing yet"
    *and* for EOF, which is exactly the trap this transport has to avoid."""

    def __init__(self, items=()):
        self._items = list(items)
        self.closed = False
        self.polls = 0
        self.sent = []

    def recv(self, timeout=None):
        self.polls += 1
        if self._items:
            return self._items.pop(0)
        return None

    def send(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


def test_tunnel_transport_treats_a_recv_timeout_as_silence_not_eof():
    """Only ``closed`` may end the link.

    Getting this wrong kills a quiet remote terminal after one poll interval --
    the user sees the shell freeze with no error anywhere.
    """
    stream = FakeTunnelStream([("text", b'{"c":"s","d":"hi"}'), ("bin", b"bytes")])
    transport = TunnelTransport(stream)
    assert transport.recv_text(timeout=0.01) == '{"c":"s","d":"hi"}'
    assert transport.recv_text(timeout=0.01) == "bytes"
    transport.send_text("{\"op\":\"input\"}")
    assert stream.sent == [b'{"op":"input"}']
    # silence, then a real close
    stream.closed = True
    assert transport.recv_text(timeout=0.01) is None


def test_tunnel_transport_idle_stream_keeps_polling():
    """A merely quiet stream must not end the pump (that would take the
    channel down on the first idle interval)."""
    stream = FakeTunnelStream()
    transport = TunnelTransport(stream)
    out: list = []
    worker = threading.Thread(target=lambda: out.append(transport.recv_text(timeout=0.01)),
                              daemon=True)
    worker.start()
    time.sleep(0.2)
    assert out == [], "silence must not end the link"
    assert stream.polls > 5, "it keeps polling rather than blocking forever"
    stream.closed = True  # a real close ends the poll loop
    worker.join(timeout=3)
    assert out == [None]


def test_wsc_transport_answers_pings_and_skips_them():
    class Client:
        def __init__(self, frames):
            self._frames = list(frames)
            self.pongs = []

        def recv(self):
            if not self._frames:
                return None
            return self._frames.pop(0)

        def send_pong(self, data):
            self.pongs.append(data)

        def send_text(self, text):
            self.sent = text

        def close(self):
            pass

    client = Client([
        (wsutil.OP_PING, b"keepalive"),
        (wsutil.OP_TEXT, b'{"c":"s","d":"out"}'),
    ])
    transport = WSCTransport(client)
    assert transport.recv_text() == '{"c":"s","d":"out"}'
    assert client.pongs == [b"keepalive"]
    assert transport.recv_text() is None
