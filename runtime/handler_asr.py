"""ASR (voice input) bridge to the accessories/ASR service.

The ASR accessory runs as its own WebSocket + HTTP service (default
``127.0.0.1:7996``, see ``accessories/ASR/README.md``).  The chat page cannot
speak to it directly:

* an https page is not allowed to open a plain ``ws://`` socket (mixed
  content), and the microphone itself already requires https or localhost —
  so the page's origin is usually https while the ASR service is plain ws;
* the service port would have to be reachable from the browser, and its
  shared token would have to travel inside a page-visible URL;
* the browser would need to know a host/port that only makes sense on the
  machine running this process.

So the page opens a **same-origin** WebSocket and this process relays frames
to the local service, injecting the service token on the server side.
Authorization is the ordinary ``/v1/`` gate (session cookie, API key or
``?token=``) — the same one the terminal WebSocket handshake passes through.

    GET /v1/asr/health   probe for the UI (always 200; ``available`` carries
                         the verdict, so the mic button can explain itself)
    GET /v1/asr/ws       WebSocket bridge (Upgrade), PCM16LE up / JSON down

Configuration (environment):

    AGENTS_ASR_URL    ASR service base URL.  **Empty by default**, which means
                      voice input is off: the endpoints answer "not
                      configured" and the chat page hides the mic button.
                      A path part is used as the WebSocket path (the service
                      default is /asr/ws).
    AGENTS_ASR_TOKEN  forwarded to the service when it runs with --token.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.parse
import urllib.request
from typing import Tuple

from runtime import wsutil
from runtime.wsclient import WSClient, WSClientError

logger = logging.getLogger(__name__)

# WebSocket path of the ASR service when AGENTS_ASR_URL carries no path.
DEFAULT_ASR_WS_PATH = "/asr/ws"

# Browser-facing endpoints.
ASR_HEALTH_PATH = "/v1/asr/health"
ASR_WS_PATH = "/v1/asr/ws"

# Query parameters the browser may set on the bridge.  Everything else is
# dropped — in particular a browser-supplied ``token`` never reaches the
# service; that one is server-side configuration only.
_FORWARD_QUERY = ("sample_rate", "session_id", "last_seg_id")

_HEALTH_TIMEOUT = 3.0
_DIAL_TIMEOUT = 5.0


def asr_base_url() -> str:
    """Configured ASR service base URL; empty when voice input is not enabled."""
    raw = (os.environ.get("AGENTS_ASR_URL") or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    return raw.rstrip("/")


def asr_endpoints() -> Tuple[str, str]:
    """``(health_url, ws_url)`` for the configured service, or ``("", "")``.

    The HTTP probes always live at the root of the configured host; the path
    part of ``AGENTS_ASR_URL`` (if any) is taken as the service's WebSocket
    path, which lets a reverse proxy in front of the ASR service be described
    with one variable.
    """
    base = asr_base_url()
    if not base:
        return "", ""
    scheme, _, rest = base.partition("://")
    host, _, path = rest.partition("/")
    ws_path = ("/" + path) if path else DEFAULT_ASR_WS_PATH
    ws_scheme = "wss" if scheme == "https" else "ws"
    return f"{scheme}://{host}/health", f"{ws_scheme}://{host}{ws_path}"


def asr_token() -> str:
    return (os.environ.get("AGENTS_ASR_TOKEN") or "").strip()


class HandlerAsrMixin:
    """Voice-input endpoints (see module docstring)."""

    # ------------------------------------------------------------------
    # GET /v1/asr/health
    # ------------------------------------------------------------------

    def _handle_asr_health(self) -> None:
        """Report whether the ASR service answers, for the mic button.

        Always 200: the caller is a UI probe that wants to know *whether*
        voice input can start and why not, not an HTTP status to branch on.
        ``configured`` separates "this deployment has no ASR" (hide the
        button) from "it has one that is not answering" (show it, disabled).
        """
        health_url, ws_url = asr_endpoints()
        payload = {
            "available": False,
            "configured": bool(health_url),
            "asr_url": asr_base_url(),
            "ws_path": ASR_WS_PATH,
            "secure_context_required": True,
        }
        if not health_url:
            payload["error"] = "not_configured"
            payload["message"] = "未启用语音输入：设置 AGENTS_ASR_URL 指向 ASR 服务（如 http://127.0.0.1:7996）"
            self._send_json_response(200, payload)
            return
        headers = {"Accept": "application/json"}
        token = asr_token()
        if token:
            headers["X-ASR-Token"] = token
        try:
            req = urllib.request.Request(health_url, headers=headers)
            with urllib.request.urlopen(req, timeout=_HEALTH_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
            payload["available"] = True
            if isinstance(body, dict):
                payload["engine"] = body.get("engine", "")
                payload["engines"] = body.get("engines", [])
                payload["sessions"] = body.get("sessions")
        except Exception as exc:
            # Not started / wrong port / bad token all land here; the message
            # is what the UI shows, so keep it short and non-technical-ish.
            payload["error"] = "asr_unreachable"
            payload["message"] = f"{health_url}: {exc}"
        self._send_json_response(200, payload)

    # ------------------------------------------------------------------
    # GET /v1/asr/ws  (WebSocket upgrade)
    # ------------------------------------------------------------------

    @staticmethod
    def _asr_error_frame(sock, code: str, message: str) -> None:
        """Report a bridge-level failure over the socket we just opened.

        A bare HTTP error during an upgrade leaves the browser with an opaque
        onerror; an error frame lets the UI name the fix.
        """
        try:
            wsutil.ws_send_text(sock, json.dumps(
                {"type": "error", "code": code, "message": message}, ensure_ascii=False))
            wsutil.ws_send_close(sock, 4404)
        except OSError:
            pass

    def _asr_target_url(self) -> str:
        """Service URL for this bridge connection (filtered query + token).

        Empty when voice input is not configured.
        """
        _health, ws_url = asr_endpoints()
        if not ws_url:
            return ""
        query = urllib.parse.urlparse(self.path).query
        params = {
            key: values[0]
            for key, values in urllib.parse.parse_qs(query).items()
            if key in _FORWARD_QUERY and values
        }
        token = asr_token()
        if token:
            params["token"] = token
        encoded = urllib.parse.urlencode(params)
        return f"{ws_url}?{encoded}" if encoded else ws_url

    def _handle_asr_ws(self) -> None:
        """Bridge one browser WebSocket to one ASR service session.

        Frames are relayed verbatim in both directions (binary = PCM16LE
        audio up, JSON text events down); ping/pong are answered locally on
        each side so an idle recording is not dropped by an intermediary.
        """
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

        target = self._asr_target_url()
        if not target:
            # Voice input is off in this deployment: the button is normally
            # hidden by the health probe, so this is the "stale page" path.
            self._asr_error_frame(sock, "not_configured",
                                  "未启用语音输入：设置 AGENTS_ASR_URL 指向 ASR 服务")
            return
        try:
            upstream = WSClient.connect(target, timeout=_DIAL_TIMEOUT)
        except WSClientError as exc:
            logger.info("ASR service unreachable at %s: %s", target, exc)
            # Answer over the socket we just opened: a bare HTTP error during
            # an upgrade gives the browser an opaque onerror, whereas an error
            # frame tells the UI "start the ASR service".
            self._asr_error_frame(sock, "asr_unreachable",
                                  f"ASR 服务不可达（{asr_base_url()}）：{exc}")
            return

        # Idle recordings are silent but not dead: the service streams events
        # only while speech is detected, so the bridge must not time out.
        upstream.set_timeout(None)
        logger.info("ASR bridge open -> %s", target)

        def _pump_to_browser() -> None:
            try:
                while True:
                    frame = upstream.recv()
                    if frame is None:
                        break
                    opcode, payload = frame
                    if opcode == wsutil.OP_TEXT:
                        wsutil.ws_send_text(sock, payload.decode("utf-8", errors="replace"))
                    elif opcode == wsutil.OP_BINARY:
                        wsutil.ws_send_binary(sock, payload)
                    elif opcode == wsutil.OP_PING:
                        upstream.send_pong(payload)
            except OSError:
                pass

        pump = threading.Thread(target=_pump_to_browser, name="asr-ws-bridge", daemon=True)
        pump.start()
        try:
            while True:
                # reply_close=False: the teardown below answers the close
                # handshake, and two close frames look like a failed recording.
                frame = wsutil.ws_recv_frame(sock, expect_masked=True,
                                             reply_close=False)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == wsutil.OP_PING:
                    wsutil.ws_send_pong(sock, payload)
                    continue
                if opcode not in (wsutil.OP_TEXT, wsutil.OP_BINARY):
                    continue
                try:
                    if opcode == wsutil.OP_TEXT:
                        upstream.send_text(payload.decode("utf-8", errors="replace"))
                    else:
                        upstream.send_binary(payload)
                except OSError:
                    break
        finally:
            upstream.close()
            pump.join(timeout=3)
            try:
                wsutil.ws_send_close(sock)
            except OSError:
                pass
            logger.info("ASR bridge closed")
