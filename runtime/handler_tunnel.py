"""Tunnel (WS 反向隧道) endpoints.

Parent side (server-to-server, setup-token/API-key authorization):
    POST   /v1/tunnel/register        child registers itself (upsert record)
    DELETE /v1/tunnel/register?tunnel_id=...   child removes itself
    GET    /v1/tunnel/ws?token=...    tunnel data channel (WebSocket)

Child side (browser UI, normal session authorization; meaningful only when
env.json SETUP_SOURCE points at a parent):
    GET    /v1/tunnel/parent/status
    POST   /v1/tunnel/parent/register
    POST   /v1/tunnel/parent/unregister

The parent/child distinction is contextual: every agent runs both sides of
the code; the child side idles unless TUNNEL_ENABLED is set, and the parent
side only acts on tunnel ids that have remote-env records.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.parse

from runtime.remote_env_manager import snapshot_from_hello
from runtime.tunnel_protocol import (
    OP_HELLO,
    OP_PING,
    OP_PONG,
    OP_REQ,
    OP_REJECT,
    OP_RESP,
    OP_STREAM_CLOSE,
    OP_STREAM_DATA,
    OP_STREAM_ERROR,
    OP_STREAM_READY,
    REJECT_NOT_REGISTERED,
    decode_frame,
    encode_frame,
    tunnel_env_id,
    tunnel_id_from_env_id,
)
from runtime import wsutil

logger = logging.getLogger("runtime.tunnel")

_TUNNEL_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def _valid_tunnel_id(value: str) -> bool:
    return bool(_TUNNEL_ID_RE.match(str(value or "").strip().lower()))


class HandlerTunnelMixin:
    # ------------------------------------------------------------------
    # Parent side: registration
    # ------------------------------------------------------------------

    def _handle_tunnel_register(self) -> None:
        """POST /v1/tunnel/register — a child registers itself to this parent.

        Body: {"tunnel_id": "<16hex>", "snapshot": {...}, "url_hint": "..."}
        Upserts the ``tunnel:<id>`` remote-env record so it shows up in the
        remote environment list immediately (before the WS connects).
        """
        body = self._read_json_body()
        if body is None:
            return
        tunnel_id = str(body.get("tunnel_id", "")).strip().lower()
        if not _valid_tunnel_id(tunnel_id):
            self._send_json_error(400, "tunnel_id must be 16 hex chars")
            return
        snapshot = body.get("snapshot")
        if not isinstance(snapshot, dict):
            snapshot = None
        url_hint = str(body.get("url_hint", "") or "").strip()
        manager = self.server.remote_env_manager  # type: ignore[attr-defined]
        try:
            envs = manager.upsert_tunnel(tunnel_id, snapshot_from_hello(snapshot), url_hint)
        except OSError as exc:
            self._send_json_error(500, f"Failed to write remote_envs.json: {exc}")
            return
        self._send_json_response(200, {
            "ok": True,
            "env_id": tunnel_env_id(tunnel_id),
            "envs": envs,
        })

    def _handle_tunnel_unregister(self) -> None:
        """DELETE /v1/tunnel/register?tunnel_id=... — a child removes itself.

        The child has already (or is about to) stop dialing; this just drops
        the record so the list stays clean.
        """
        tunnel_id = str(self._get_query_param("tunnel_id", "")).strip().lower()
        if not _valid_tunnel_id(tunnel_id):
            self._send_json_error(400, "tunnel_id must be 16 hex chars")
            return
        env_id = tunnel_env_id(tunnel_id)
        manager = self.server.remote_env_manager  # type: ignore[attr-defined]
        try:
            envs = manager.remove(env_id)
        except KeyError:
            self._send_json_response(200, {"ok": True, "removed": False, "envs": manager.read()})
            return
        self._send_json_response(200, {"ok": True, "removed": True, "envs": envs})

    # ------------------------------------------------------------------
    # Parent side: tunnel data channel (WebSocket)
    # ------------------------------------------------------------------

    def _handle_tunnel_ws(self) -> None:
        """GET /v1/tunnel/ws — tunnel data channel upgrade.

        The first frame must be a hello carrying the child's tunnel id; the
        manager binds (welcome), replaces a duplicate, or rejects ids with no
        remote-env record (a child can never resurrect a removed record).
        """
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send_json_error(400, "Missing Sec-WebSocket-Key")
            return
        self.close_connection = True
        sock = self.connection
        # Idle tunnel connections must survive indefinitely (the sweeper
        # pings for liveness), so clear the HTTP handler's 30s socket timeout.
        try:
            sock.settimeout(None)
        except OSError:
            pass
        try:
            sock.sendall(wsutil.ws_handshake_response(key).encode("utf-8"))
        except OSError:
            return

        manager = self.server.tunnel_manager  # type: ignore[attr-defined]
        frame = wsutil.ws_recv_frame(sock, expect_masked=True)
        if frame is None or frame[0] != wsutil.OP_TEXT:
            self._tunnel_ws_close(sock)
            return
        try:
            hello = decode_frame(frame[1])
        except ValueError:
            self._tunnel_ws_send_json(sock, {"op": OP_REJECT, "reason": "bad_hello"})
            self._tunnel_ws_close(sock)
            return
        if hello.get("op") != OP_HELLO:
            self._tunnel_ws_send_json(sock, {"op": OP_REJECT, "reason": "expected_hello"})
            self._tunnel_ws_close(sock)
            return
        tunnel_id = str(hello.get("tunnel_id", "")).strip().lower()
        if not _valid_tunnel_id(tunnel_id):
            self._tunnel_ws_send_json(sock, {"op": OP_REJECT, "reason": "bad_tunnel_id"})
            self._tunnel_ws_close(sock)
            return

        action, env_id = manager.attach(sock, tunnel_id)
        if action == "reject":
            logger.info("Tunnel: rejected hello from unregistered tunnel_id=%s", tunnel_id)
            self._tunnel_ws_send_json(sock, {"op": OP_REJECT, "reason": REJECT_NOT_REGISTERED})
            self._tunnel_ws_close(sock)
            return
        conn = manager.conn_for_env(env_id)
        if conn is None:
            return
        logger.info("Tunnel: %s online (env %s)", tunnel_id, env_id)
        try:
            self._tunnel_ws_reader_loop(sock, conn, tunnel_id)
        finally:
            manager.detach(tunnel_id, conn)
            try:
                sock.close()
            except OSError:
                pass

    def _tunnel_ws_reader_loop(self, sock, conn, tunnel_id: str) -> None:
        """Dispatch inbound frames until the connection dies."""
        while True:
            frame = wsutil.ws_recv_frame(sock, expect_masked=True)
            if frame is None:
                return
            opcode, data = frame
            conn.last_frame_at = time.monotonic()
            if opcode == wsutil.OP_PING:
                wsutil.ws_send_pong(sock, data)
                continue
            if opcode == wsutil.OP_PONG:
                continue
            if opcode == wsutil.OP_TEXT:
                try:
                    obj = decode_frame(data)
                except ValueError:
                    continue
                op = obj.get("op")
                if op == OP_RESP:
                    conn.complete_resp(obj)
                    conn.active_body_id = str(obj.get("id", ""))
                elif op in (OP_STREAM_READY, OP_STREAM_ERROR):
                    conn.complete_stream_open(obj)
                elif op == OP_STREAM_DATA:
                    conn.feed_stream(obj)
                elif op == OP_STREAM_CLOSE:
                    conn.close_stream_remote(str(obj.get("sid", "")))
                # hello/pong from the child are not expected after welcome
                continue
            if opcode == wsutil.OP_BINARY:
                if conn.active_body_id is None:
                    continue  # stray binary frame — ignore
                if data:
                    conn.feed_body(conn.active_body_id, data)
                else:
                    conn.finalize_body(conn.active_body_id)
                    conn.active_body_id = None

    @staticmethod
    def _tunnel_ws_send_json(sock, obj: dict) -> None:
        try:
            wsutil.ws_send_frame(sock, encode_frame(obj), wsutil.OP_TEXT)
        except OSError:
            pass

    @staticmethod
    def _tunnel_ws_close(sock) -> None:
        try:
            wsutil.ws_send_close(sock)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Parent side: browser bridge (tunnel-mode workspace / terminal)
    # ------------------------------------------------------------------

    # Response headers forwarded to the browser (everything else belongs to
    # the child's local response and would confuse keep-alive framing here).
    _TUNNEL_PROXY_FORWARD_RESP_HEADERS = {
        "content-type", "content-disposition", "content-length",
        "content-encoding",
        "cache-control", "content-range", "accept-ranges",
        "etag", "last-modified",
    }
    # Browser request headers forwarded to the child (the browser cookie is
    # not forwarded — it is meaningless to the child; auth is handled by the
    # child itself, which self-authorizes requests arriving over the tunnel).
    _TUNNEL_PROXY_FORWARD_REQ_HEADERS = {
        "content-type", "x-upload-offset", "x-upload-size", "x-file-size",
        "range",
    }

    def _tunnel_proxy_env(self, env_id: str):
        """Validate env_id as a tunnel env with a record; returns (record, tunnel_manager)."""
        from runtime.tunnel_protocol import is_tunnel_env_id
        if not is_tunnel_env_id(env_id):
            self._send_json_error(400, "Not a tunnel environment")
            return None, None
        manager = self.server.remote_env_manager  # type: ignore[attr-defined]
        try:
            record = manager.get(env_id)
        except KeyError:
            self._send_json_error(404, f"Remote environment not found: {env_id}")
            return None, None
        return record, getattr(self.server, "tunnel_manager", None)

    def _tunnel_proxy_child_path(self, env_id: str) -> str:
        """Rebuild the child path (+query) from the incoming request.

        ``env_id`` arrives already unquoted (the router decodes it), but the
        raw request path may still carry percent-encoding: the browser builds
        the bridge URL with ``encodeURIComponent`` (``tunnel%3A...``) while
        other clients may send the bare id.  Strip whichever form is present.
        """
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        for prefix in (
            "/v1/tunnel-proxy/" + urllib.parse.quote(env_id, safe=""),
            f"/v1/tunnel-proxy/{env_id}",
        ):
            if path.startswith(prefix):
                path = path[len(prefix):] or "/"
                break
        else:
            path = "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return path

    def _handle_tunnel_proxy(self, env_id: str) -> None:
        """ANY /v1/tunnel-proxy/{env_id}/{child path...} — browser bridge.

        The browser talks to the parent (same origin: cookies, no CORS, no
        child token juggling); the parent forwards the request over the
        child's tunnel and streams the response back.  Covers workspace
        list/search/content/download/create/move/delete and chunk uploads.
        """
        record, tunnel_manager = self._tunnel_proxy_env(env_id)
        if record is None:
            return
        if tunnel_manager is None or not tunnel_manager.is_online(env_id):
            self._send_json_response(502, {
                "error": "child_unreachable",
                "message": "Child tunnel is offline.",
            })
            return

        child_path = self._tunnel_proxy_child_path(env_id)
        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() in self._TUNNEL_PROXY_FORWARD_REQ_HEADERS
        }
        body = None
        if self.command not in {"GET", "HEAD"}:
            # Only buffer a body when the client actually sent one.  Many
            # bridged methods carry no body at all (e.g. DELETE
            # /v1/terminals/{id}); requiring Content-Length > 0 here would
            # 400 them before they ever reach the child, leaving the child
            # side effect (a destroyed terminal, a deleted file, ...) undone.
            try:
                content_length = int(self.headers.get("Content-Length", 0) or 0)
            except (TypeError, ValueError):
                content_length = 0
            if content_length > 0:
                from runtime.handler_base import _MAX_PUSH_BODY_BYTES
                body = self._read_raw_body(_MAX_PUSH_BODY_BYTES)
                if body is None:
                    return

        try:
            status, resp_headers, chunks = tunnel_manager.call_env_stream(
                env_id, self.command, child_path, headers, body, timeout=600,
            )
        except Exception as exc:
            self._send_json_response(502, {
                "error": "child_unreachable",
                "message": f"Cannot reach child environment over tunnel: {exc}",
            })
            return

        fwd_headers = {
            k: v for k, v in resp_headers.items()
            if k.lower() in self._TUNNEL_PROXY_FORWARD_RESP_HEADERS
        }
        content_length = fwd_headers.get("Content-Length")
        self.send_response(status)
        for key, value in fwd_headers.items():
            self.send_header(key, value)
        if content_length:
            self.end_headers()
            remaining = int(content_length)
        else:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            remaining = -1
        try:
            while True:
                chunk = chunks.get(timeout=300)
                if chunk is None:
                    break
                if content_length:
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
                else:
                    self.wfile.write(("%x" % len(chunk)).encode("ascii") + b"\r\n")
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
            if not content_length:
                self.wfile.write(b"0\r\n\r\n")
        except OSError:
            self.close_connection = True

    def _handle_tunnel_proxy_ws(self, env_id: str) -> None:
        """GET /v1/tunnel-proxy/{env_id}/v1/terminals/ws — terminal bridge.

        Browser WebSocket ↔ parent tunnel stream ↔ child terminal WebSocket.
        Frame types are preserved (the terminal protocol speaks text).
        """
        record, tunnel_manager = self._tunnel_proxy_env(env_id)
        if record is None:
            return
        if tunnel_manager is None or not tunnel_manager.is_online(env_id):
            self._send_json_response(502, {
                "error": "child_unreachable",
                "message": "Child tunnel is offline.",
            })
            return

        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send_json_error(400, "Missing Sec-WebSocket-Key")
            return
        self.close_connection = True
        sock = self.connection
        try:
            sock.settimeout(None)
        except OSError:
            pass
        try:
            sock.sendall(wsutil.ws_handshake_response(key).encode("utf-8"))
        except OSError:
            return

        # Child terminal path (the child self-authorizes the local handshake
        # when its authorization is enabled).
        child_path = self._tunnel_proxy_child_path(env_id)
        try:
            stream = tunnel_manager.open_stream_env(
                env_id, child_path, timeout=15,
            )
        except Exception as exc:
            self._tunnel_ws_close(sock)
            return

        def _pump_to_browser() -> None:
            try:
                while True:
                    item = stream.recv(timeout=5.0)
                    if item is None:
                        break
                    frame_type, data = item
                    if frame_type == "text":
                        wsutil.ws_send_text(sock, data.decode("utf-8", errors="replace"))
                    else:
                        wsutil.ws_send_binary(sock, data)
            except OSError:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        pump = threading.Thread(target=_pump_to_browser, name="tunnel-proxy-ws", daemon=True)
        pump.start()
        try:
            while True:
                frame = wsutil.ws_recv_frame(sock, expect_masked=True)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == wsutil.OP_PING:
                    wsutil.ws_send_pong(sock, payload)
                    continue
                if opcode not in (wsutil.OP_TEXT, wsutil.OP_BINARY):
                    continue
                try:
                    stream.send(payload)
                except Exception:
                    break
        finally:
            try:
                stream.close()
            except Exception:
                pass
            self._tunnel_ws_close(sock)
            pump.join(timeout=5)

    # ------------------------------------------------------------------
    # Child side: browser UI endpoints
    # ------------------------------------------------------------------

    def _handle_tunnel_parent_status(self) -> None:
        """GET /v1/tunnel/parent/status — tunnel registration state for the UI."""
        client = self.server.tunnel_client  # type: ignore[attr-defined]
        self._send_json_response(200, client.status())

    def _handle_tunnel_parent_register(self) -> None:
        """POST /v1/tunnel/parent/register — register this env to its parent.

        Optional JSON body: ``{"parent": "<url>"}``.  When the address is
        non-empty it is validated and persisted as the ``SETUP_SOURCE`` env
        entry (the dial loop keeps using it); an absent/empty body falls back
        to the currently configured ``SETUP_SOURCE``.
        """
        parent = ""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            content_length = 0
        if content_length > 0:
            body = self._read_json_body()
            if body is None:
                return  # error response already sent
            if isinstance(body, dict):
                parent = str(body.get("parent", "") or "")
        client = self.server.tunnel_client  # type: ignore[attr-defined]
        ok, message, data = client.request_register(parent)
        payload = dict(data or {})
        payload["ok"] = ok
        if message:
            payload["error"] = message
        self._send_json_response(200 if ok else 400, payload)

    def _handle_tunnel_parent_unregister(self) -> None:
        """POST /v1/tunnel/parent/unregister — remove this env from its parent."""
        self._drain_request_body()
        client = self.server.tunnel_client  # type: ignore[attr-defined]
        ok, message, data = client.request_unregister()
        payload = dict(data or {})
        payload["ok"] = ok
        if message:
            payload["error"] = message
        self._send_json_response(200 if ok else 400, payload)
