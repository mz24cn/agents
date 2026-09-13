"""TunnelClient — child-side dialer of the parent tunnel.

A child environment registers itself to its parent (explicit user action via
the UI: ``POST /v1/tunnel/parent/register``), then dials the parent's
``/v1/tunnel/ws`` data channel and serves frames:

* ``req`` frames are executed against this environment's own HTTP service
  over loopback (the normal handlers + authorization apply unchanged) and
  answered with ``resp`` frames,
* ``stream-open`` frames bridge one of the local WebSocket endpoints (e.g.
  the terminal) by dialing it with the same WS client,
* ``deregister`` (parent removed this environment) and a rejected hello
  (no record on the parent) clear the persisted enabled flag, so a later
  restart never resurrects a removed registration.

Parent address + token are taken from the existing ``SETUP_SOURCE`` entry in
env.json (the same field the build-version UI already shows); the register
endpoint may supply a new address, which is then persisted to ``SETUP_SOURCE``.
The enabled flag is the ``TUNNEL_ENABLED`` env.json key; the child identity
(``tunnel_id``) is a 16-hex value persisted in ``DATA_DIR/tunnel_id``.

Zero third-party dependencies — only Python standard library.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from runtime.auth_manager import COOKIE_NAME
from runtime.common import atomic_write_text, get_workspace
from runtime.remote_env_manager import normalize_setup_url
from runtime.tunnel_protocol import (
    OP_DEREGISTER,
    OP_PING,
    OP_PONG,
    OP_REQ,
    OP_REJECT,
    OP_REPLACED,
    OP_RESP,
    OP_STREAM_CLOSE,
    OP_STREAM_DATA,
    OP_STREAM_ERROR,
    OP_STREAM_OPEN,
    OP_STREAM_READY,
    OP_WELCOME,
    REJECT_NOT_REGISTERED,
    RECONNECT_MAX,
    RECONNECT_MIN,
    SMALL_BODY_INLINE,
    STALE_AFTER,
    CHUNK_SIZE,
    decode_frame,
    encode_frame,
    new_tunnel_id,
    tunnel_env_id,
)
from runtime import wsutil
from runtime.wsclient import WSClient, WSClientError

logger = logging.getLogger("runtime.tunnel_client")

_TUNNEL_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}


class TunnelClient:
    """Child-side tunnel dialer (one instance per server)."""

    # Concurrent local-call workers (the parent's own fan-out is 8:
    # talk_to max_workers / group-chat sub-agents).
    _WORKERS = 8

    def __init__(self, server, data_dir: str) -> None:
        self._server = server
        self._data_dir = data_dir
        self._env_manager = server._env_manager  # type: ignore[attr-defined]
        self._wake = threading.Event()
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tunnel_id: Optional[str] = None
        self._live_ws: Optional[WSClient] = None
        self._streams: dict[str, "_LocalStream"] = {}
        self._streams_lock = threading.Lock()
        # Tool-call execution: reqs are served by a small worker pool on
        # dedicated threads so a long-running local call (e.g. a remote
        # delegate) never blocks the frame reader (which must keep answering
        # parent pings) nor other concurrent tunnel requests.  Responses are
        # still emitted atomically per request (one _send_lock) so the parent
        # reader's single active-body cursor stays in sync.
        import queue as _queue
        self._req_queue: "_queue.Queue" = _queue.Queue()
        self._worker_pool: list[threading.Thread] = []
        # Serializes every child->parent frame SEQUENCE: a response's header +
        # body + terminator must not interleave with pongs or stream-data
        # frames (the parent routes response bodies with one active-body id).
        self._send_lock = threading.Lock()
        self._state = {"state": "idle", "detail": "", "env_id": ""}
        self._state_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        with self._state_lock:
            if self._thread is not None:
                return
            # Re-arm after a stop() so the loop runs again.
            self._stop_evt.clear()
            self._wake.clear()
            self._thread = threading.Thread(target=self._loop, name="tunnel-client", daemon=True)
            self._thread.start()
            if not any(t.is_alive() for t in self._worker_pool):
                self._worker_pool = [
                    threading.Thread(
                        target=self._req_worker,
                        name=f"tunnel-req-worker-{i}",
                        daemon=True,
                    )
                    for i in range(self._WORKERS)
                ]
                for t in self._worker_pool:
                    t.start()

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake.set()
        for _ in self._worker_pool:
            self._req_queue.put(None)  # stop sentinel
        # Close the live tunnel so a blocked recv() returns immediately and
        # the parent stops seeing this child as online.
        ws = self._live_ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3)
            self._thread = None

    # ------------------------------------------------------------------
    # UI-facing API (browser → this environment)
    # ------------------------------------------------------------------

    def status(self) -> dict:
        enabled, base, token, _err = self._read_config()
        parent = base
        if token:
            masked = token[:3] + "…" + token[-2:] if len(token) > 8 else "****"
            parent = f"{base} (token {masked})"
        with self._state_lock:
            state = dict(self._state)
        return {
            "configured": bool(base),
            "parent": parent,
            "tunnel_id": self._tunnel_id or "",
            "enabled": enabled,
            "state": state["state"],
            "env_id": state["env_id"],
            "detail": state["detail"],
        }

    def request_register(self, parent: str = "") -> tuple:
        """Explicitly register this environment to its parent and start
        dialing.

        When *parent* is a non-empty address it is validated and persisted
        as the ``SETUP_SOURCE`` env entry (the single source of truth the
        dial loop and the UI keep reading); otherwise the currently
        configured ``SETUP_SOURCE`` from env.json is used.
        Returns (ok, message, data)."""
        raw = str(parent or "").strip()
        if raw:
            try:
                norm = normalize_setup_url(raw)
            except ValueError as exc:
                return (False, f"母环境地址无效: {exc}", {})
            try:
                self._env_manager.set("SETUP_SOURCE", raw)
            except Exception as exc:
                return (False, f"Cannot persist SETUP_SOURCE: {exc}", {})
            base = f"{norm['scheme']}://{norm['netloc']}"
            token = next((v for k, v in norm["query"] if k == "token"), "")
        else:
            _enabled, base, token, err = self._read_config()
            if not base:
                return (False, err or "SETUP_SOURCE is not configured", {})
        tunnel_id = self._ensure_tunnel_id()
        snapshot = self._hello_snapshot()
        url = base + "/v1/tunnel/register"
        body = json.dumps({"tunnel_id": tunnel_id, "snapshot": snapshot}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, data=body, headers=headers, method="POST"),
                timeout=20,
            ) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = _json_body_or_empty(exc.read())
            message = detail.get("error") or detail.get("message") or f"HTTP {exc.code}"
            return (False, f"Parent rejected registration: {message}", {})
        except Exception as exc:
            return (False, f"Cannot reach parent environment: {exc}", {})
        self._env_manager.set("TUNNEL_ENABLED", "1")
        self._set_state("registering", env_id=str(data.get("env_id", "")))
        self._wake.set()
        return (True, "", data)

    def request_unregister(self) -> tuple:
        """Remove this environment from its parent and stop dialing."""
        _enabled, base, token, _err = self._read_config()
        tunnel_id = self._tunnel_id
        if base and tunnel_id:
            url = base + "/v1/tunnel/register?tunnel_id=" + urllib.parse.quote(tunnel_id)
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                urllib.request.urlopen(
                    urllib.request.Request(url, data=b"", headers=headers, method="DELETE"),
                    timeout=10,
                )
            except Exception as exc:
                logger.warning("Tunnel: parent unregister failed (best effort): %s", exc)
        self._env_manager.delete("TUNNEL_ENABLED")
        ws = self._live_ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        self._wake.set()
        return (True, "", {})

    # ------------------------------------------------------------------
    # Config / identity
    # ------------------------------------------------------------------

    def _read_config(self) -> tuple:
        """Return (enabled, parent_base_url, token, error_message)."""
        try:
            env = self._env_manager.read()
        except Exception:
            env = {}
        enabled = str(env.get("TUNNEL_ENABLED", "")) == "1"
        raw = str(env.get("SETUP_SOURCE", "") or "").strip()
        if not raw:
            return (enabled, "", "", "SETUP_SOURCE 未配置（母环境地址）")
        try:
            norm = normalize_setup_url(raw)
        except ValueError as exc:
            return (enabled, "", "", f"SETUP_SOURCE 无效: {exc}")
        base = f"{norm['scheme']}://{norm['netloc']}"
        token = next((v for k, v in norm["query"] if k == "token"), "")
        return (enabled, base, token, "")

    def _parent_ws_url(self, base: str, token: str) -> str:
        ws_base = base.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        url = ws_base + "/v1/tunnel/ws"
        if token:
            url += "?token=" + urllib.parse.quote(token, safe="")
        return url

    def _ensure_tunnel_id(self) -> str:
        if self._tunnel_id:
            return self._tunnel_id
        path = os.path.join(self._data_dir, "tunnel_id")
        tunnel_id = ""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                tunnel_id = fh.read().strip()
        except OSError:
            tunnel_id = ""
        if not _TUNNEL_ID_RE.match(tunnel_id):
            tunnel_id = new_tunnel_id()
            try:
                os.makedirs(self._data_dir, exist_ok=True)
                atomic_write_text(path, tunnel_id)
            except OSError as exc:
                logger.warning("Tunnel: cannot persist tunnel_id (%s); using in-memory value", exc)
        self._tunnel_id = tunnel_id
        return tunnel_id

    def _hello_snapshot(self) -> dict:
        """Assemble the same fields ``op=hello`` advertises, in-process."""
        from runtime.env_manager import compute_setup_versions
        from runtime.handler_api import _SERVER_INSTANCE_ID, _platform_arch, _platform_os

        try:
            env_map = self._env_manager.read()
        except Exception:
            env_map = {}
        server_sock = getattr(self._server, "_server", None)
        return {
            **compute_setup_versions(self._env_manager, self._data_dir),
            "inference_active": bool(getattr(server_sock, "active_streams", {})) or bool(
                int(getattr(server_sock, "active_inference_count", 0) or 0)
            ),
            "api_inference_active": bool(
                int(getattr(server_sock, "active_api_inference_count", 0) or 0)
            ),
            "session_inference_active": bool(getattr(server_sock, "active_streams", {})),
            "server_instance_id": _SERVER_INSTANCE_ID,
            "app_title": str(env_map.get("APP_TITLE", "") or ""),
            "app_logo": str(env_map.get("APP_LOGO", "") or ""),
            "arch": _platform_arch(),
            "os": _platform_os(),
            "workspace": get_workspace(),
        }

    def _set_state(self, state: str, detail: str = "", env_id: str = "") -> None:
        with self._state_lock:
            self._state = {
                "state": state,
                "detail": detail,
                "env_id": env_id or self._state.get("env_id", ""),
            }

    def _clear_enabled(self) -> None:
        try:
            self._env_manager.delete("TUNNEL_ENABLED")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Dial loop
    # ------------------------------------------------------------------

    def _wait_wake(self, timeout: float) -> bool:
        """Wait up to *timeout* seconds for a state-change wake (or stop).

        Returns True when woken early (re-evaluate config immediately).
        """
        self._wake.wait(timeout)
        if self._wake.is_set():
            self._wake.clear()
            return True
        return False

    def _loop(self) -> None:
        backoff = RECONNECT_MIN
        while not self._stop_evt.is_set():
            enabled, base, token, err = self._read_config()
            if not enabled:
                # Keep "unregistered" stable (the parent removed this
                # environment) until the user explicitly registers again;
                # otherwise plain idle.
                with self._state_lock:
                    keep = self._state.get("state") == "unregistered"
                if not keep:
                    self._set_state("idle")
                self._wait_wake(1.0)
                continue
            if not base:
                self._set_state("error", err)
                self._wait_wake(5.0)
                continue
            tunnel_id = self._ensure_tunnel_id()
            try:
                self._set_state("connecting")
                outcome = self._dial_once(base, token, tunnel_id)
            except WSClientError as exc:
                self._set_state("error", f"connect: {exc}")
                self._wait_wake(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)
                continue
            except Exception as exc:
                logger.exception("Tunnel: dial loop error")
                self._set_state("error", str(exc))
                self._wait_wake(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)
                continue
            backoff = RECONNECT_MIN
            if outcome == "not-registered":
                self._clear_enabled()
                self._set_state("unregistered", "parent has no record for this environment")
                continue
            if outcome == "deregistered":
                self._set_state("idle", "removed from parent")
                continue
            if outcome == "replaced":
                self._wait_wake(1.0)
                continue
            # "closed" — transient disconnect; redial after a short pause.
            self._wait_wake(backoff)

    def _dial_once(self, base: str, token: str, tunnel_id: str) -> str:
        """One connect+serve cycle.

        Returns a terminal outcome: "closed", "not-registered",
        "deregistered", or "replaced".
        """
        url = self._parent_ws_url(base, token)
        ws = WSClient.connect(url, timeout=15.0)
        self._live_ws = ws
        ws.set_timeout(STALE_AFTER + 5.0)
        try:
            self._send_frame_obj(ws, {
                "op": "hello",
                "tunnel_id": tunnel_id,
                "snapshot": self._hello_snapshot(),
            })
            frame = ws.recv()
            if frame is None:
                return "closed"
            opcode, data = frame
            if opcode != wsutil.OP_TEXT:
                return "closed"
            obj = decode_frame(data)
            op = obj.get("op")
            if op == OP_WELCOME:
                self._set_state("online", env_id=str(obj.get("env_id", "")))
            elif op == OP_REJECT:
                if obj.get("reason") == REJECT_NOT_REGISTERED:
                    return "not-registered"
                return "closed"
            elif op == OP_REPLACED:
                return "replaced"
            else:
                return "closed"
            try:
                return self._serve(ws)
            except (OSError, socket.timeout):
                return "closed"
        finally:
            if self._live_ws is ws:
                self._live_ws = None
            try:
                ws.close()
            except Exception:
                pass

    def _serve(self, ws: WSClient) -> str:
        """Serve tunnel frames until the connection dies or is deregistered."""
        while not self._stop_evt.is_set():
            frame = ws.recv()
            if frame is None:
                return "closed"
            opcode, data = frame
            if opcode == wsutil.OP_PING:
                with self._send_lock:
                    ws.send_pong(data)
                continue
            if opcode == wsutil.OP_PONG:
                continue
            if opcode != wsutil.OP_TEXT:
                continue  # stray binary outside a req body — ignore
            try:
                obj = decode_frame(data)
            except ValueError:
                continue
            op = obj.get("op")
            if op == OP_REQ:
                # Read the (already-terminated) body on the reader thread,
                # then hand the whole job to the worker pool; the reader
                # must not stall on long local calls or pings would time out.
                try:
                    body = self._read_req_body(ws)
                except _BodyReadFailed:
                    self._send_resp(ws, str(obj.get("id", "")), 400,
                                    {"Content-Type": "application/json"},
                                    b'{"error": "failed to read request body over tunnel"}')
                    continue
                self._req_queue.put((ws, {
                    "id": str(obj.get("id", "")),
                    "method": str(obj.get("method", "GET")).upper(),
                    "path": str(obj.get("path", "/")),
                    "headers": obj.get("headers") or {},
                    "body": body,
                }))
            elif op == OP_STREAM_OPEN:
                self._handle_stream_open(ws, obj)
            elif op == OP_STREAM_DATA:
                try:
                    data = base64.b64decode(str(obj.get("data", "")))
                except (ValueError, TypeError):
                    continue
                with self._streams_lock:
                    stream = self._streams.get(str(obj.get("sid", "")))
                if stream is not None:
                    stream.to_local_put(str(obj.get("t", "bin")), data)
            elif op == OP_STREAM_CLOSE:
                with self._streams_lock:
                    stream = self._streams.pop(str(obj.get("sid", "")), None)
                if stream is not None:
                    stream.close_from_parent()
            elif op == OP_DEREGISTER:
                self._clear_enabled()
                return "deregistered"
            elif op == OP_REPLACED:
                return "replaced"
        return "closed"

    # ------------------------------------------------------------------
    # HTTP request dispatch (serial — one local call in flight at a time)
    # ------------------------------------------------------------------

    def _req_worker(self) -> None:
        """Worker thread: executes queued tunnel reqs against the local HTTP
        service and streams the response back over the tunnel.  A None
        sentinel (from stop()) ends the loop."""
        while True:
            item = self._req_queue.get()
            if item is None:
                return
            ws, job = item
            try:
                self._execute_req(ws, job)
            except Exception:
                logger.exception("Tunnel: local request execution failed")
            finally:
                _release_body(job.get("body"))

    def _execute_req(self, ws: WSClient, job: dict) -> None:
        rid = job["id"]
        try:
            status, headers_out, resp = self._local_call(
                job["method"], job["path"], job["headers"], job["body"]
            )
        except OSError as exc:
            self._send_resp(ws, rid, 502, {"Content-Type": "application/json"},
                            json.dumps({"error": f"local call failed: {exc}"}).encode())
            return
        self._send_response(ws, rid, status, headers_out, resp)

    def _send_response(self, ws: WSClient, rid: str, status: int,
                       headers_out: dict, resp) -> None:
        """Emit one complete response frame sequence (header + body +
        terminator) atomically.

        Holding _send_lock across the whole sequence keeps pongs and
        stream-data frames (other threads on the same tunnel) out of the
        body: the parent reader routes response bodies with a single
        active-body cursor, so any interleaved frame corrupts the body.
        """
        try:
            with self._send_lock:
                ws.send_text(encode_frame({
                    "op": OP_RESP, "id": rid, "status": status, "headers": headers_out,
                }).decode("utf-8"))
                while True:
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    ws.send_binary(chunk)
                ws.send_binary(b"")  # body terminator
        except (OSError, WSClientError):
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _send_frame_obj(self, ws: WSClient, obj: dict) -> None:
        # Single-frame control sends share the sequence lock (they may
        # interleave BETWEEN atomic response sequences, never inside one).
        with self._send_lock:
            ws.send_text(encode_frame(obj).decode("utf-8"))

    def _read_req_body(self, ws: WSClient):
        """Consume binary frames after a req header until the empty
        terminator.  Small bodies stay in memory; larger ones spill to a
        temp file (so multi-hundred-MB pushes do not double memory)."""
        parts: list[bytes] = []
        total = 0
        tmp = None
        while True:
            frame = ws.recv()
            if frame is None:
                raise _BodyReadFailed()
            opcode, data = frame
            if opcode == wsutil.OP_PING:
                # A parent liveness ping arrived mid-body: answer it and
                # keep reading.  The parent sends request frame sequences
                # atomically, so this is defensive (e.g. a future control
                # frame must not kill a multi-minute body transfer).
                with self._send_lock:
                    ws.send_pong(data)
                continue
            if opcode == wsutil.OP_PONG:
                continue
            if opcode != wsutil.OP_BINARY:
                raise _BodyReadFailed()
            if not data:
                break
            if total <= SMALL_BODY_INLINE and tmp is None:
                parts.append(data)
                total += len(data)
            else:
                if tmp is None:
                    import tempfile
                    tmp = tempfile.TemporaryFile("w+b")
                    tmp.write(b"".join(parts))
                    parts = []
                tmp.write(data)
                total += len(data)
        if tmp is not None:
            tmp.flush()
            tmp.seek(0)
            return tmp
        return b"".join(parts)

    def _auth_manager(self):
        """This environment's AuthManager.

        The manager instance is attached to the HTTP server object
        (``auth_manager``) and mirrored on the server wrapper
        (``_auth_manager``); the client holds a reference to the wrapper.
        """
        auth = getattr(self._server, "auth_manager", None)
        if auth is None:
            auth = getattr(self._server, "_auth_manager", None)
        return auth

    def _self_auth_cookie(self) -> str:
        """Self-authorization for requests arriving over the tunnel.

        The tunnel itself is authenticated (this environment dials the parent
        with the parent's credential), so a request arriving over it is the
        parent's. When this environment has authorization enabled, present
        its own session cookie on the loopback call so it passes auth; the
        parent never needs to store a child-side token.
        """
        auth = self._auth_manager()
        if auth is None or not auth.is_enabled():
            return ""
        try:
            token, _ttl = auth.create_session_token()
        except Exception:
            return ""
        return f"{COOKIE_NAME}={token}"

    def _self_auth_token(self) -> str:
        """Short-lived setup token for tunnel-bridged WebSocket handshakes.

        The child's GET auth accepts a ``token=`` query parameter (setup
        token / API key — not session cookies), so a fresh setup token is
        attached at dial time; it is only needed for the handshake itself.
        """
        auth = self._auth_manager()
        if auth is None or not auth.is_enabled():
            return ""
        try:
            token, _exp = auth.create_setup_token()
        except Exception:
            return ""
        return token

    def _local_call(self, method: str, path: str, headers_in: dict, body):
        """Execute the request against this environment's own HTTP service."""
        port = self._server.port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=None)
        headers = {
            str(k): str(v)
            for k, v in (headers_in or {}).items()
            if str(k).lower() not in _HOP_BY_HOP
        }
        cookie = self._self_auth_cookie()
        if cookie and not any(str(k).lower() == "cookie" for k in headers):
            headers["Cookie"] = cookie
        if isinstance(body, (bytes, bytearray)):
            conn.request(method, path, body=bytes(body) or None, headers=headers)
        else:
            # seekable temp file → http.client sets Content-Length and
            # streams the body
            conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        out_headers = {
            k: v for k, v in resp.getheaders() if k.lower() not in _HOP_BY_HOP
        }
        return resp.status, out_headers, resp

    def _send_resp(self, ws: WSClient, rid: str, status: int, headers: dict, body: bytes) -> None:
        try:
            with self._send_lock:
                ws.send_text(encode_frame({
                    "op": OP_RESP, "id": rid, "status": status, "headers": headers,
                }).decode("utf-8"))
                if body:
                    for i in range(0, len(body), CHUNK_SIZE):
                        ws.send_binary(body[i:i + CHUNK_SIZE])
                ws.send_binary(b"")
        except (OSError, WSClientError):
            pass

    # ------------------------------------------------------------------
    # Stream bridging (local WebSocket endpoints, e.g. terminals)
    # ------------------------------------------------------------------

    def _handle_stream_open(self, ws: WSClient, obj: dict) -> None:
        sid = str(obj.get("sid", ""))
        path = str(obj.get("path", "/"))
        if not path.startswith("/"):
            self._send_stream_error(ws, sid, 400)
            return
        token = self._self_auth_token()
        if token:
            sep = "&" if "?" in path else "?"
            path = f"{path}{sep}token={urllib.parse.quote(token, safe='')}"
        local_url = f"ws://127.0.0.1:{self._server.port}{path}"
        try:
            local = WSClient.connect(local_url, timeout=10.0)
            # The local endpoint (e.g. an idle terminal) may be silent for a
            # long time; the tunnel-side liveness logic owns reaping, so the
            # local read must not time out.
            local.set_timeout(None)
        except (WSClientError, OSError) as exc:
            logger.warning("Tunnel: local WS bridge to %s failed: %s", path, exc)
            self._send_stream_error(ws, sid, 502)
            return
        stream = _LocalStream(self, ws, sid, local)
        with self._streams_lock:
            self._streams[sid] = stream
        try:
            self._send_frame_obj(ws, {"op": OP_STREAM_READY, "sid": sid})
        except (OSError, WSClientError):
            with self._streams_lock:
                self._streams.pop(sid, None)
            local.close()
            return
        tag = f"tunnel-stream-{sid[:8]}"
        threading.Thread(target=stream.pump, name=f"{tag}-pump", daemon=True).start()
        threading.Thread(target=stream.write_loop, name=f"{tag}-writer", daemon=True).start()

    def _send_stream_error(self, ws: WSClient, sid: str, status: int) -> None:
        try:
            self._send_frame_obj(ws, {"op": OP_STREAM_ERROR, "sid": sid, "status": status})
        except (OSError, WSClientError):
            pass


class _BodyReadFailed(Exception):
    pass


def _release_body(body) -> None:
    if hasattr(body, "close"):
        try:
            body.close()
        except Exception:
            pass


def _json_body_or_empty(raw) -> dict:
    try:
        data = json.loads(raw if isinstance(raw, (bytes, bytearray)) else b"")
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


class _LocalStream:
    """One bridged local WebSocket endpoint on the child side."""

    def __init__(self, owner: TunnelClient, parent_ws: WSClient, sid: str, local: WSClient) -> None:
        self.owner = owner
        self.parent_ws = parent_ws
        self.sid = sid
        self.local = local
        # parent→local chunks (bytes); None is the EOF sentinel.
        self.to_local = _make_queue()
        self.closed = False

    def to_local_put(self, frame_type: str, data: bytes) -> None:
        if not self.closed and data:
            self.to_local.put((frame_type, data))

    def write_loop(self) -> None:
        """parent→local direction: write queued chunks to the local WS,
        preserving the frame type (text stays text)."""
        while True:
            item = self.to_local.get()
            if item is None:
                break
            frame_type, data = item
            try:
                if frame_type == "text":
                    self.local.send_text(data.decode("utf-8", errors="replace"))
                else:
                    self.local.send_binary(data)
            except (OSError, WSClientError):
                break

    def close_from_parent(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.local.close()
        except Exception:
            pass
        self.to_local.put(None)

    def pump(self) -> None:
        """local → parent direction: relay local WS frames to the parent."""
        try:
            while not self.closed:
                frame = self.local.recv()
                if frame is None:
                    break
                opcode, data = frame
                if opcode == wsutil.OP_PING:
                    self.local.send_pong(data)
                    continue
                if opcode not in (wsutil.OP_TEXT, wsutil.OP_BINARY):
                    continue
                self._send_to_parent({
                    "op": OP_STREAM_DATA, "sid": self.sid,
                    "t": "text" if opcode == wsutil.OP_TEXT else "bin",
                    "data": base64.b64encode(data).decode("ascii"),
                })
        except (OSError, WSClientError):
            pass
        finally:
            self.closed = True
            with self.owner._streams_lock:
                self.owner._streams.pop(self.sid, None)
            try:
                self._send_to_parent({"op": OP_STREAM_CLOSE, "sid": self.sid})
            except Exception:
                pass
            try:
                self.local.close()
            except Exception:
                pass

    def _send_to_parent(self, obj: dict) -> None:
        with self.owner._send_lock:
            self.parent_ws.send_text(encode_frame(obj).decode("utf-8"))


def _make_queue():
    import queue
    return queue.Queue()
