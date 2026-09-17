"""ServiceClient — urllib-based HTTP + SSE client for the local Agent Service.

Implements the frozen contract ``docs/tui-contract.md`` §4.1:

- GET  requests carry the token as a ``?token=`` query parameter;
- POST/PUT/DELETE requests carry ``Authorization: Bearer <token>``;
- a :class:`http.cookiejar.CookieJar` holds the login session cookie
  (``POST /v1/auth/login`` Set-Cookie) and is attached to every request;
- ``infer_stream`` opens ``POST /v1/infer/stream`` in a background daemon
  thread and parses the SSE byte stream, invoking
  ``on_event(event, data)`` with ``event`` in
  ``{"init", "message", "usage", "done", "error"}``.

The SSE parser tolerates (per contract §3.3, verified against
``runtime/handler_infer.py``):

- ``id: <n>`` lines (ignored; v1 has no resume support);
- ``:`` heartbeat comment lines;
- ``event: <name>`` lines (``init`` / ``usage`` / anything else);
- multi-line ``data:`` payloads (joined with ``\\n`` per the SSE spec);
- UTF-8 content split across read chunks (incremental decoder);
- the ``data: [DONE]`` terminator;
- non-2xx JSON error bodies (delivered as an ``error`` event);
- abrupt connection close before ``[DONE]`` (delivered as an ``error`` event).

Standard library only.
"""

from __future__ import annotations

import codecs
import json
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

__all__ = ["ApiError", "InferHandle", "ServiceClient"]

# Default timeout (seconds) for ordinary JSON requests.
_DEFAULT_TIMEOUT = 30.0


class ApiError(Exception):
    """Raised for non-2xx responses on the ordinary JSON API.

    Attributes:
        status: HTTP status code (e.g. 401, 404).
        message: human-readable detail (backend ``error``/``message`` field
            when the body is JSON, otherwise a raw body snippet).
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = int(status)
        self.message = str(message)


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------

class _SSEParser:
    """Incremental Server-Sent Events line parser.

    ``feed(text)`` consumes decoded text; complete frames (terminated by a
    blank line) are dispatched to ``on_frame(event, data)`` where *event* is
    the ``event:`` field value or ``None`` and *data* is the joined
    multi-line ``data:`` payload.
    """

    def __init__(self, on_frame) -> None:
        self._on_frame = on_frame
        self._event: str | None = None
        self._data: str | None = None
        self._buf = ""

    def feed(self, text: str) -> None:
        if not text:
            return
        self._buf += text
        while True:
            nl = self._buf.find("\n")
            if nl < 0:
                break
            line = self._buf[:nl]
            self._buf = self._buf[nl + 1:]
            self._handle_line(line.rstrip("\r"))

    def close(self) -> None:
        """Flush a trailing line (no final newline) and dispatch pending data."""
        if self._buf:
            self._handle_line(self._buf.rstrip("\r"))
            self._buf = ""
        self._dispatch()

    def _handle_line(self, line: str) -> None:
        if line.startswith(":"):
            return  # comment / heartbeat (e.g. ": keepalive")
        if line == "":
            self._dispatch()
            return
        if ":" in line:
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]  # strip exactly one leading space (SSE spec)
        else:
            field, value = line, ""
        if field == "data":
            self._data = value if self._data is None else self._data + "\n" + value
        elif field == "event":
            self._event = value
        # "id" / "retry" / unknown fields: tolerated and ignored (v1 has no
        # Last-Event-ID resumption).

    def _dispatch(self) -> None:
        event, data = self._event, self._data
        self._event = None
        self._data = None
        if data is None:
            return  # blank line without data: nothing to dispatch
        self._on_frame(event, data)


def _error_detail_from_http_error(exc: urllib.error.HTTPError) -> str:
    """Extract a readable error message from a non-2xx HTTPError body."""
    try:
        raw = exc.read()
    except Exception:
        raw = b""
    if not raw:
        return exc.reason or f"HTTP {exc.code}"
    text = raw.decode("utf-8", "replace")
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            for key in ("error", "message", "detail"):
                val = obj.get(key)
                if val:
                    return str(val)
    except (ValueError, UnicodeDecodeError):
        pass
    return text.strip()[:200] or f"HTTP {exc.code}"


# ---------------------------------------------------------------------------
# InferHandle
# ---------------------------------------------------------------------------

class InferHandle:
    """Handle for one ``POST /v1/infer/stream`` request.

    A daemon thread reads the SSE stream and invokes ``on_event``; the UI
    main loop polls :meth:`done` / :meth:`wait`.  ``abort`` posts
    ``/v1/infer/abort`` once the ``init`` frame has revealed the session id.
    """

    def __init__(self, client: "ServiceClient", body: dict, on_event) -> None:
        self._client = client
        self._on_event = on_event
        self._finished = threading.Event()
        self._session_id: str | None = None
        self._done = False
        self._abort_sent = False
        self._thread = threading.Thread(
            target=self._run, args=(body,), name="tui-infer-stream", daemon=True
        )
        self._thread.start()

    # -- public API (contract §4.1) ----------------------------------------

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the stream ends (done or error).

        Returns True when finished, False on timeout.
        """
        return self._finished.wait(timeout)

    def done(self) -> bool:
        return self._finished.is_set()

    def abort(self) -> None:
        """POST /v1/infer/abort {"session_id": ..., "forced": false}.

        No-op while the session id is still unknown (init frame not seen).
        Raises :class:`ApiError` / :class:`urllib.error.URLError` on failure;
        the caller decides how to surface it.
        """
        if self._abort_sent:
            return
        sid = self._session_id
        if not sid:
            return  # cannot target an abort without the session id
        self._abort_sent = True
        try:
            self._client.post(
                "/v1/infer/abort", {"session_id": sid, "forced": False}
            )
        except Exception:
            # A second abort attempt (e.g. the user presses Ctrl+C again)
            # must not be blocked by the first failure.
            self._abort_sent = False
            raise

    @property
    def session_id(self) -> str | None:
        return self._session_id

    # -- background worker --------------------------------------------------

    def _safe_event(self, event: str, data: dict) -> None:
        try:
            self._on_event(event, data)
        except Exception:
            # Callbacks only append data (contract §4.1); a UI-side exception
            # must never kill the stream thread.
            pass

    def _run(self, body: dict) -> None:
        req = self._client._build_request(
            "POST", "/v1/infer/stream", body=body
        )
        # No socket timeout for the stream: connection setup on 127.0.0.1
        # fails instantly when refused, while the body may legitimately stay
        # silent for minutes (a single-agent tool call such as exec_cli
        # produces no SSE frames until it finishes — the single-agent path
        # sends no heartbeats).
        try:
            resp = self._client._opener.open(req, timeout=None)
        except urllib.error.HTTPError as exc:
            # Non-2xx with a JSON error body (bad model, 409 update in
            # progress, ...): surface it as an error event.
            self._safe_event(
                "error",
                {"message": f"HTTP {exc.code}: {_error_detail_from_http_error(exc)}"},
            )
            self._finished.set()
            return
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", None) or exc
            self._safe_event("error", {"message": f"connection failed: {reason}"})
            self._finished.set()
            return

        parser = _SSEParser(self._dispatch_frame)
        decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            with resp:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    parser.feed(decoder.decode(chunk))
                parser.feed(decoder.decode(b"", final=True))
            parser.close()
            if not self._done:
                self._safe_event(
                    "error",
                    {"message": "stream closed before [DONE]"},
                )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self._safe_event("error", {"message": f"stream interrupted: {exc}"})
        finally:
            self._finished.set()

    def _dispatch_frame(self, event_name: str | None, data: str) -> None:
        if data == "[DONE]":
            self._done = True
            self._safe_event("done", {})
            return
        try:
            obj = json.loads(data)
        except (ValueError, TypeError):
            obj = None
        if event_name == "init":
            if isinstance(obj, dict):
                sid = obj.get("session_id")
                if sid:
                    self._session_id = str(sid)
                self._safe_event("init", obj)
            else:
                self._safe_event(
                    "error", {"message": f"init frame is not JSON: {data[:200]}"}
                )
        elif event_name == "usage":
            if isinstance(obj, dict):
                self._safe_event("usage", obj)
            else:
                self._safe_event(
                    "error", {"message": f"usage frame is not JSON: {data[:200]}"}
                )
        elif event_name == "error":
            if isinstance(obj, dict):
                msg = obj.get("message") or obj.get("error") or data[:200]
            else:
                msg = str(obj)
            self._safe_event("error", {"message": msg})
        else:
            # Bare ``data:`` frame (no event name) = Message dict; unknown
            # event names are treated as message payloads when they parse.
            if isinstance(obj, dict):
                self._safe_event("message", obj)
            else:
                self._safe_event(
                    "error",
                    {
                        "message": (
                            f"invalid SSE data"
                            + (f" in event '{event_name}'" if event_name else "")
                            + f": {data[:200]}"
                        )
                    },
                )


# ---------------------------------------------------------------------------
# ServiceClient
# ---------------------------------------------------------------------------

class ServiceClient:
    """Thin HTTP client for the local Agent Service (contract §4.1).

    Authentication assembly:
      - GET: the token is appended as ``?token=<token>``;
      - POST/PUT/DELETE: ``Authorization: Bearer <token>``;
      - the cookie jar always rides along (login session cookie).
    """

    def __init__(self, base_url: str, token: str = "",
                 ssl_context: "ssl.SSLContext | None" = None) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.token = token or ""
        self.ssl_context = ssl_context
        self._jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )

    # -- low level ----------------------------------------------------------

    def _build_url(self, path: str, params: dict | None,
                   add_token: bool) -> str:
        query = {}
        if params:
            for key, value in params.items():
                if value is None:
                    continue
                if isinstance(value, bool):
                    value = "true" if value else "false"
                elif not isinstance(value, str):
                    value = str(value)
                query[key] = value
        if add_token and self.token and "token" not in query:
            query["token"] = self.token
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def _build_request(self, method: str, path: str,
                       params: dict | None = None,
                       body: dict | None = None) -> urllib.request.Request:
        # Contract §4.1: GET carries the token as ?token=; POST/PUT/DELETE
        # carry Authorization: Bearer.  Never both.
        url = self._build_url(path, params, add_token=(method == "GET"))
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if method in ("POST", "PUT", "DELETE") and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return urllib.request.Request(url, data=data, method=method,
                                      headers=headers)

    def _request(self, method: str, path: str, params: dict | None = None,
                 body: dict | None = None) -> dict:
        req = self._build_request(method, path, params=params, body=body)
        try:
            resp = self._opener.open(req, timeout=_DEFAULT_TIMEOUT)
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, _error_detail_from_http_error(exc)) from exc
        with resp:
            raw = resp.read()
        if not raw:
            return {}
        text = raw.decode("utf-8", "replace")
        try:
            obj = json.loads(text)
        except ValueError:
            return {"raw": text}
        return obj if isinstance(obj, dict) else {"data": obj}

    # -- generic verbs (contract §4.1) ---------------------------------------

    def get(self, path: str, **params) -> dict:
        return self._request("GET", path, params=params or None)

    def post(self, path: str, body: dict | None = None) -> dict:
        return self._request("POST", path, body=body)

    def put(self, path: str, body: dict | None = None) -> dict:
        return self._request("PUT", path, body=body)

    def delete(self, path: str) -> dict:
        return self._request("DELETE", path)

    # -- convenience methods (contract §4.1) ---------------------------------

    def auth_status(self) -> dict | None:
        """GET /v1/auth/config.

        Returns the status dict when the service answered 200, or ``None``
        when authentication is enabled and unauthenticated (401).
        """
        try:
            return self.get("/v1/auth/config")
        except ApiError as exc:
            if exc.status == 401:
                return None
            raise

    def login(self, password: str) -> bool:
        """POST /v1/auth/login {"password": ...}; stores the session cookie.

        Returns True on 200 (including auth-disabled 200), False on 401.
        Other HTTP errors raise :class:`ApiError`.
        """
        try:
            data = self.post("/v1/auth/login", {"password": password})
        except ApiError as exc:
            if exc.status == 401:
                return False
            raise
        return bool(data.get("ok"))

    def env(self) -> dict:
        """GET /v1/env → {"env": {...}}."""
        return self.get("/v1/env")

    def agents(self) -> list[dict]:
        data = self.get("/v1/agents")
        if isinstance(data, dict):
            items = data.get("agents")
            if isinstance(items, list):
                return items
        if isinstance(data, list):
            return data
        return []

    def models(self) -> list[dict]:
        data = self.get("/v1/models")
        if isinstance(data, dict):
            items = data.get("models")
            if isinstance(items, list):
                return items
        if isinstance(data, list):
            return data
        return []

    def tools(self) -> list[dict]:
        data = self.get("/v1/tools")
        if isinstance(data, dict):
            items = data.get("tools")
            if isinstance(items, list):
                return items
        if isinstance(data, list):
            return data
        return []

    def sessions(self, limit: int = 20) -> list[dict]:
        """GET /v1/sessions, first page, up to *limit* entries.

        The backend paginates with ``page`` / ``page_size`` (verified in
        ``runtime/handler_api.py::_paginate_session_results``; the contract's
        ``?limit=`` wording is not honored by the server) and wraps the list
        in ``{"sessions": [...], "page", "page_size", "total", "has_more"}``.
        """
        data = self.get("/v1/sessions", page=1, page_size=int(limit or 20))
        if isinstance(data, dict):
            items = data.get("sessions")
            if isinstance(items, list):
                return items
        if isinstance(data, list):
            return data
        return []

    def conversation(self, session_id: str) -> dict:
        """GET /v1/sessions/{id} → conversation.json envelope dict."""
        return self.get("/v1/sessions/" + urllib.parse.quote(str(session_id)))

    def tunnel_status(self) -> dict:
        return self.get("/v1/tunnel/parent/status")

    def tunnel_register(self, parent_url: str) -> dict:
        """POST /v1/tunnel/parent/register {"parent": url}.

        Raises :class:`ApiError` on the backend's 400 failure response
        (its body carries ``{"ok": false, "error": ...}``).
        """
        return self.post("/v1/tunnel/parent/register", {"parent": parent_url})

    def tunnel_unregister(self) -> dict:
        return self.post("/v1/tunnel/parent/unregister", None)

    def infer_stream(self, body: dict, on_event) -> InferHandle:
        """POST /v1/infer/stream in a background daemon thread.

        ``on_event(event, data)`` is called from that thread with
        ``event`` in {"init", "message", "usage", "done", "error"}.
        """
        return InferHandle(self, body, on_event)
