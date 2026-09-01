"""Base handler mixin: shared helpers, routing, static files, WebSocket/PTY, auth.

Part of the ``_RuntimeRequestHandler`` decomposition in ``runtime.server``.
This mixin provides the building blocks shared by every endpoint: CORS,
JSON helpers, authorization, the do_* dispatchers, static file serving,
the WebSocket/PTY terminal support and the auth handlers.

Zero third-party dependencies — only Python standard library.
"""

import base64
import datetime
import gzip
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import select
import struct
import sys
import threading
import time
import urllib.parse
from http import cookies
from typing import Optional

# PTY support for WebSocket terminal
if sys.platform == 'win32':
    try:
        from winpty import PtyProcess
    except ImportError:
        PtyProcess = None
else:
    import pty
    import fcntl
    import termios

from runtime.auth_manager import AuthManager, COOKIE_NAME
from runtime.runtime import Runtime
from runtime.server_state import (
    get_or_create_terminal,
    get_terminal_for_session,
    get_terminal_session,
    unregister_terminal_session,
    _terminal_sessions,
    _terminal_sessions_lock,
)

logger = logging.getLogger("runtime.server")


# ---------------------------------------------------------------------------
# Declarative route tables (precompiled regexes)
# ---------------------------------------------------------------------------
# Each entry is (compiled_pattern, handler_name, converters):
#   - compiled_pattern: precompiled regex matched against the URL path
#   - handler_name: method on the handler instance to invoke
#   - converters: tuple of callables applied to each regex capture group
#     (e.g. urllib.parse.unquote, int); empty tuple passes groups through.
# Order matters: more specific routes must appear before generic ones.
# ---------------------------------------------------------------------------

_ROUTES: dict[str, list] = {
    "GET": [
        (re.compile(r"^/v1/models$"), "_handle_list_models", ()),
        (re.compile(r"^/v1/tools$"), "_handle_list_tools", ()),
        (re.compile(r"^/v1/mcp-servers$"), "_handle_list_mcp_servers", ()),
        (re.compile(r"^/v1/prompt-templates$"), "_handle_list_prompt_templates", ()),
        (re.compile(r"^/v1/env$"), "_handle_get_env", ()),
        (re.compile(r"^/v1/auth/config$"), "_handle_auth_config_get", ()),
        (re.compile(r"^/v1/setup$"), "_handle_setup_script", ()),
        (re.compile(r"^/v1/sessions/tree$"), "_handle_session_category_tree", ()),
        (re.compile(r"^/v1/sessions$"), "_handle_list_sessions", ()),
        (re.compile(r"^/v1/sessions/search$"), "_handle_search_sessions", ()),
        (re.compile(r"^/v1/sessions/events$"), "_handle_sessions_events", ()),
        (re.compile(r"^/v1/sessions/([^/]+)/stream$"), "_handle_session_stream", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/sessions/([^/]+)/log-dir$"), "_handle_session_log_dir", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/sessions/([^/]+)/execution-analysis$"), "_handle_session_execution_analysis", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/sessions/([^/]+)$"), "_handle_get_session", ()),
        (re.compile(r"^/v1/agents$"), "_handle_list_agents", ()),
        (re.compile(r"^/v1/agents/([^/]+)$"), "_handle_get_agent", ()),
        (re.compile(r"^/v1/workspace/list$"), "_handle_workspace_list", ()),
        (re.compile(r"^/v1/workspace/children$"), "_handle_workspace_children", ()),
        (re.compile(r"^/v1/workspace/search$"), "_handle_workspace_search", ()),
        (re.compile(r"^/v1/workspace/content$"), "_handle_workspace_content", ()),
        (re.compile(r"^/v1/workspace/download$"), "_handle_workspace_download", ()),
        (re.compile(r"^/v1/workspace/thumbnail$"), "_handle_workspace_thumbnail", ()),
        (re.compile(r"^/v1/workspace/paste-dir$"), "_handle_workspace_paste_dir", ()),
        (re.compile(r"^/v1/sessions/([^/]+)/file-journals/([^/]+)$"), "_handle_get_file_journal_diff", (urllib.parse.unquote, urllib.parse.unquote)),
        (re.compile(r"^/v1/sessions/([^/]+)/file-journals$"), "_handle_get_file_journals", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/terminals$"), "_handle_list_terminals", ()),
    ],
    "POST": [
        (re.compile(r"^/v1/auth/login$"), "_handle_auth_login", ()),
        (re.compile(r"^/v1/auth/logout$"), "_handle_auth_logout", ()),
        (re.compile(r"^/v1/auth/config$"), "_handle_auth_config_post", ()),
        (re.compile(r"^/v1/infer$"), "_handle_infer", ()),
        (re.compile(r"^/v1/infer/stream$"), "_handle_infer_stream", ()),
        (re.compile(r"^/v1/infer/abort$"), "_handle_infer_abort", ()),
        (re.compile(r"^/v1/tools/call$"), "_handle_tool_call", ()),
        (re.compile(r"^/v1/tools/mcp$"), "_handle_register_mcp_servers", ()),
        (re.compile(r"^/v1/tools/test$"), "_handle_test_tool", ()),
        (re.compile(r"^/v1/tools/skill$"), "_handle_register_skill", ()),
        (re.compile(r"^/v1/models$"), "_handle_register_model", ()),
        (re.compile(r"^/v1/tools$"), "_handle_register_tool", ()),
        (re.compile(r"^/v1/prompt-templates$"), "_handle_create_prompt_template", ()),
        (re.compile(r"^/v1/env$"), "_handle_set_env", ()),
        (re.compile(r"^/v1/env/detect$"), "_handle_detect_env", ()),
        (re.compile(r"^/v1/sessions/([^/]+)/generate-title$"), "_handle_generate_session_title", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/sessions/([^/]+)/regenerate-summary$"), "_handle_regenerate_session_summary", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/sessions/([^/]+)/read$"), "_handle_mark_session_read", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/sessions/([^/]+)/flight$"), "_handle_session_flight", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/sessions/([^/]+)/revoke$"), "_handle_revoke_session", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/agents$"), "_handle_create_agent", ()),
        (re.compile(r"^/v1/workspace/rename$"), "_handle_workspace_rename", ()),
        (re.compile(r"^/v1/workspace/mkdir$"), "_handle_workspace_mkdir", ()),
        (re.compile(r"^/v1/workspace/duplicate$"), "_handle_workspace_duplicate", ()),
        (re.compile(r"^/v1/workspace/move$"), "_handle_workspace_move", ()),
        (re.compile(r"^/v1/workspace/copy$"), "_handle_workspace_copy", ()),
        (re.compile(r"^/v1/workspace/upload/init$"), "_handle_workspace_upload_init", ()),
        (re.compile(r"^/v1/workspace/upload/([^/]+)/complete$"), "_handle_workspace_upload_complete", (urllib.parse.unquote,)),
    ],
    "PUT": [
        (re.compile(r"^/v1/models/([^/]+)$"), "_handle_update_model", ()),
        (re.compile(r"^/v1/tools/([^/]+)$"), "_handle_update_tool", ()),
        (re.compile(r"^/v1/mcp-servers/([^/]+)$"), "_handle_restore_mcp_server_config", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/prompt-templates/([^/]+)$"), "_handle_update_prompt_template", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/agents/([^/]+)$"), "_handle_update_agent", ()),
        (re.compile(r"^/v1/workspace/upload/([^/]+)/chunk/(\d+)$"), "_handle_workspace_upload_chunk", (urllib.parse.unquote, int)),
    ],
    "DELETE": [
        (re.compile(r"^/v1/models/([^/]+)$"), "_handle_delete_model", ()),
        (re.compile(r"^/v1/tools/batch$"), "_handle_batch_delete_tools", ()),
        (re.compile(r"^/v1/mcp-servers/([^/]+)$"), "_handle_delete_mcp_server", ()),
        (re.compile(r"^/v1/tools/([^/]+)$"), "_handle_delete_tool", ()),
        (re.compile(r"^/v1/prompt-templates/([^/]+)$"), "_handle_delete_prompt_template", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/env/([^/]+)$"), "_handle_delete_env", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/sessions/([^/]+)$"), "_handle_delete_session", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/agents/([^/]+)$"), "_handle_delete_agent", ()),
        (re.compile(r"^/v1/workspace/delete$"), "_handle_workspace_delete", ()),
        (re.compile(r"^/v1/workspace/upload/([^/]+)$"), "_handle_workspace_upload_cancel", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/terminals/([^/]+)$"), "_handle_delete_terminal", ()),
    ],
}


# ---------------------------------------------------------------------------
# Static file gzip cache
# ---------------------------------------------------------------------------
# Re-gzipping every text asset on every request is wasteful for an SPA with
# many JS/CSS files. Cache the compressed bytes keyed by (realpath, mtime_ns,
# size) so repeat requests (and 304 revalidations that miss) hit the cache.
# Content-hashed assets are immutable, so the cache is very stable.
_STATIC_GZIP_CACHE: dict = {}
_STATIC_GZIP_CACHE_LOCK = threading.Lock()
_STATIC_GZIP_CACHE_MAX = 64

# Files at or above this size are streamed with zero-copy sendfile instead of
# being read into memory (and never gzip-compressed in memory).
_SENDFILE_THRESHOLD = 256 * 1024  # 256 KB
# Do not gzip files larger than this in memory (protects the sendfile path
# from accidentally buffering multi-hundred-MB text files).
_GZIP_MAX_SIZE = 8 * 1024 * 1024  # 8 MB


class HandlerBaseMixin:
    def log_message(self, format: str, *args: object) -> None:
        """Request logging.

        Default is suppressed to keep stderr quiet; set logging level to DEBUG
        to see every request line (\"GET /v1/... HTTP/1.1\" 200).
        """
        logger.debug("HTTP request: " + format, *args)

    def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
        """Log any error response (esp. 501 from unsupported methods) with the
        offending method + path before delegating to the stdlib handler."""
        logger.warning(
            "HTTP %s %s -> %s %s",
            self.command if hasattr(self, "command") else "?",
            self.path if hasattr(self, "path") else "?",
            code,
            message or "",
        )
        super().send_error(code, message, explain)

    # ------------------------------------------------------------------
    # CORS
    # ------------------------------------------------------------------

    def end_headers(self) -> None:
        """Override to inject CORS headers on every response."""
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight requests."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Upload-Offset, X-Upload-Size, X-File-Size")
        # Explicit zero-length body so HTTP/1.1 keep-alive framing stays correct.
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _content_disposition(self, disposition: str, filename: str) -> str:
        """Build a Content-Disposition header that supports non-ASCII filenames.

        ``BaseHTTPRequestHandler.send_header`` encodes header values as latin-1.
        Raw Chinese (or other non-latin-1) characters in ``filename=`` therefore
        raise UnicodeEncodeError. RFC 5987/6266 allows UTF-8 names through the
        ASCII-only ``filename*`` parameter, while ``filename`` remains a safe
        fallback for older clients.
        """
        safe_disposition = re.sub(r"[^A-Za-z]", "", disposition) or "attachment"
        fallback = filename.encode("ascii", "ignore").decode("ascii").strip() or "download"
        fallback = fallback.replace('\\', '_').replace('"', '_').replace('\r', '_').replace('\n', '_')
        encoded = urllib.parse.quote(filename, safe="")
        return f'{safe_disposition}; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'

    def _drain_request_body(self) -> None:
        """Consume any unread request body so keep-alive streams stay aligned.

        Handlers that never read their body (e.g. ``upload/complete``, which the
        client calls with a JSON ``{}``) must drain it. Otherwise the leftover
        bytes sit in the socket buffer and get read as the *start of the next
        request line* on the same connection, corrupting its method/path —
        seen as intermittent 501 ``Unsupported method ('{}POST')`` on the
        following ``upload/init`` request.
        """
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 0:
                self.rfile.read(content_length)
        except (ValueError, OSError):
            pass

    def _read_json_body(self) -> Optional[dict]:
        """Read and parse JSON from the request body.

        Returns:
            Parsed dict, or None if parsing fails (error response is sent).
        """
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_json_error(400, "Empty request body")
            return None
        raw = self.rfile.read(content_length)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_json_error(400, f"Invalid JSON: {exc}")
            return None

    def _send_json_response(self, status: int, data: object) -> None:
        """Send a JSON response with the given status code."""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json_error(self, status: int, message: str) -> None:
        """Send a JSON error response."""
        self._send_json_response(status, {"error": message})

    def _get_auth_manager(self) -> AuthManager:
        return self.server.auth_manager  # type: ignore[attr-defined]

    def _request_cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return ""
        try:
            jar = cookies.SimpleCookie()
            jar.load(raw)
            morsel = jar.get(name)
            return morsel.value if morsel else ""
        except Exception:
            return ""

    def _bearer_token(self) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return ""

    def _is_authorized(self, method: str, path: str) -> bool:
        if not path.startswith("/v1/"):
            return True
        if method == "OPTIONS":
            return True
        # Login/logout endpoints must remain reachable so the browser can
        # establish/clear its HttpOnly session cookie.
        if method == "POST" and path in {"/v1/auth/login", "/v1/auth/logout"}:
            return True

        auth_manager = self._get_auth_manager()
        # Missing auth_token.json or missing password/api-key keeps the whole
        # authorization system invisible and preserves existing behavior.
        if not auth_manager.is_enabled():
            return True

        # /v1/setup is public only for GET op=hello. All other setup
        # operations follow normal authorization and may use a setup token.
        if method == "GET" and path == "/v1/setup":
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if method == "GET" and params.get("op", [""])[0] == "hello":
                return True
            token = params.get("token", [""])[0]
            if token and auth_manager.verify_setup_token(token):
                return True

        session_token = self._request_cookie(COOKIE_NAME)
        if session_token and auth_manager.verify_session_token(session_token):
            return True

        bearer = self._bearer_token()
        if bearer and auth_manager.verify_api_key(bearer):
            return True

        return False

    def _require_authorized(self, method: str, path: str) -> bool:
        if self._is_authorized(method, path):
            return True
        self._send_json_response(401, {"error": "unauthorized", "message": "Authentication required"})
        return False

    def _send_session_cookie(self, token: str, max_age: int) -> None:
        parts = [
            f"{COOKIE_NAME}={token}",
            f"Max-Age={int(max_age)}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            parts.append("Secure")
        self.send_header("Set-Cookie", "; ".join(parts))

    def _clear_session_cookie(self) -> None:
        self.send_header("Set-Cookie", f"{COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict")

    def _get_runtime(self) -> Runtime:
        """Get the Runtime instance from the server."""
        return self.server.runtime  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _dispatch_route(self, method: str, path: str) -> bool:
        """Match *path* against the route table for *method* and invoke its handler.

        Returns True if a route matched, False otherwise. Regex capture groups are
        passed to the handler after applying the per-group converters declared in
        the route table (e.g. URL-decoding, int conversion).
        """
        table = _ROUTES.get(method, ())
        for pattern, handler_name, converters in table:
            m = pattern.match(path)
            if m:
                groups = m.groups()
                if converters:
                    groups = tuple(
                        conv(g) if conv is not None else g
                        for g, conv in zip(groups, converters)
                    )
                getattr(self, handler_name)(*groups)
                return True
        return False

    def do_GET(self) -> None:
        """Handle GET requests."""
        # Strip query string before routing so GET endpoints can accept query params.
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path.startswith("/v1/") and not self._require_authorized("GET", path):
            return
        if path.startswith("/v1/"):
            if not self._dispatch_route("GET", path):
                self._send_json_error(404, f"Not found: {self.path}")
            return
        if path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            self._handle_websocket()
            return
        self._handle_static_file()

    def _handle_list_terminals(self) -> None:
        """GET /v1/terminals — list active terminal sessions."""
        with _terminal_sessions_lock:
            terminals = []
            for tid, info in _terminal_sessions.items():
                terminals.append({
                    "terminal_id": tid,
                    "session_id": info["session_id"],
                })
        self._send_json_response(200, {"terminals": terminals})

    def _handle_delete_terminal(self, terminal_id: str) -> None:
        """DELETE /v1/terminals/{terminal_id} — destroy a terminal session."""
        terminal_id = urllib.parse.unquote(terminal_id)
        session = get_terminal_session(terminal_id)
        if not session:
            # Frontend may send the bare session_id (without ":auto" suffix).
            # Try lookup by session_id as well.
            session = get_terminal_for_session(terminal_id)
            if session:
                # Find the actual terminal_id key for this session
                with _terminal_sessions_lock:
                    for tid, info in _terminal_sessions.items():
                        if info is session:
                            terminal_id = tid
                            break
        if not session:
            self._send_json_error(404, f"Terminal not found: {terminal_id}")
            return
        unregister_terminal_session(terminal_id)
        logger.info("Terminal session deleted via API: %s", terminal_id)
        self._send_json_response(200, {"status": "deleted", "terminal_id": terminal_id})

    # MIME types that benefit from gzip compression (text-based).
    _COMPRESSIBLE_MIME_PREFIXES = (
        "text/",
        "application/javascript",
        "application/json",
        "application/xml",
        "application/xhtml+xml",
        "application/rss+xml",
        "application/atom+xml",
        "image/svg+xml",
        "application/wasm",
    )

    @staticmethod
    def _is_compressible(mime_type: str) -> bool:
        """Return True if the given MIME type is a candidate for gzip."""
        for prefix in HandlerBaseMixin._COMPRESSIBLE_MIME_PREFIXES:
            if mime_type.startswith(prefix):
                return True
        return False

    @staticmethod
    def _file_etag(file_path: str, stat_info: os.stat_result) -> str:
        """Build a weak ETag from the file's mtime, size, and inode."""
        raw = f"{stat_info.st_mtime:.6f}-{stat_info.st_size}-{stat_info.st_ino}"
        return f'W/"{hashlib.md5(raw.encode()).hexdigest()}"'

    @staticmethod
    def _is_hashed_asset(filename: str) -> bool:
        """Detect assets with content-hash in filename (e.g. app.abc123.js).

        These can be cached aggressively (immutable / 1 year).
        """
        # Common patterns: name.[8+ hex chars].ext  or  name-[8+ hex chars].ext
        base, _ = os.path.splitext(filename)
        # Check for dot-separated hash suffix:  chunk.2a4d8f.js
        if "." in base:
            candidate = base.rsplit(".", 1)[-1]
            if len(candidate) >= 8 and re.match(r"^[0-9a-fA-F]+$", candidate):
                return True
        # Check for dash-separated hash suffix:  chunk-2a4d8f.js
        if "-" in base:
            candidate = base.rsplit("-", 1)[-1]
            if len(candidate) >= 8 and re.match(r"^[0-9a-fA-F]+$", candidate):
                return True
        return False

    # ------------------------------------------------------------------
    # Byte-range requests (RFC 7233) + zero-copy file streaming
    # ------------------------------------------------------------------
    # Range and sendfile are two different layers that compose into one
    # internal API:
    #   - Range is HTTP protocol semantics: the client asks for
    #     ``bytes=start-end`` and the server replies 206 + Content-Range.
    #   - sendfile is a kernel zero-copy transport (offset/count), used
    #     to ship both full 200 bodies and 206 range slices.
    # ``_stream_file(file, offset, length)`` is the single transport for
    # both; ``_serve_file_range`` implements the protocol on top of it.

    @staticmethod
    def _parse_range_header(range_header: str, size: int) -> Optional[tuple[int, int]]:
        """Parse a single-range ``Range: bytes=...`` header.

        Returns an inclusive ``(start, end)`` tuple, ``None`` when the
        request should be served as a full 200 (multi-range requests, or a
        suffix range covering the whole file), or raises ``ValueError`` for
        an invalid / unsatisfiable range (→ 416).
        """
        unit, sep, spec = range_header.partition("=")
        if not sep or unit.strip().lower() != "bytes":
            raise ValueError("unsupported range unit")
        spec = spec.strip()
        if "," in spec:
            # Multi-range: RFC 7233 §3.1 lets the server ignore it.
            return None
        dash = spec.find("-")
        if dash < 0:
            raise ValueError("malformed range")
        start_s = spec[:dash].strip()
        end_s = spec[dash + 1:].strip()
        if start_s == "":
            # Suffix range: last N bytes (bytes=-N)
            if not end_s.isdigit() or int(end_s) == 0:
                raise ValueError("invalid suffix range")
            suffix_len = int(end_s)
            if suffix_len >= size:
                return None  # covers the whole file → full 200
            return size - suffix_len, size - 1
        if not start_s.isdigit():
            raise ValueError("invalid range start")
        start = int(start_s)
        if end_s == "":
            # Open-ended range: bytes=N-  (to EOF)
            if start >= size:
                raise ValueError("range start beyond EOF")
            return start, size - 1
        if not end_s.isdigit():
            raise ValueError("invalid range end")
        end = int(end_s)
        if start > end:
            raise ValueError("range start after end")
        if start >= size:
            raise ValueError("range start beyond EOF")
        return start, min(end, size - 1)

    def _send_416(self, size: int) -> None:
        """Send 416 Range Not Satisfiable (RFC 7233 §4.4)."""
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def _stream_file(self, file_path: str, offset: int, length: int) -> None:
        """Stream ``length`` bytes of ``file_path`` starting at ``offset``.

        Uses ``os.sendfile`` (kernel zero-copy) where available, falling back
        to a portable buffered read/write loop (e.g. Windows). The caller
        must have written response headers first; this flushes the buffered
        writer so headers always hit the wire before the body.
        """
        if length <= 0:
            return
        try:
            with open(file_path, "rb") as f:
                self.wfile.flush()  # headers must reach the wire first
                try:
                    self.connection.sendfile(f, offset=offset, count=length)
                except (AttributeError, OSError, ValueError):
                    # Fallback: buffered copy (Windows / non-regular files).
                    f.seek(offset)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                    self.wfile.flush()
        except OSError:
            # File vanished or the connection broke mid-transfer; the
            # handler will close the connection.
            pass

    def _serve_file_range(
        self,
        file_path: str,
        stat_info: os.stat_result,
        mime_type: str,
        *,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        cache_control: Optional[str] = None,
        disposition: Optional[str] = None,
    ) -> bool:
        """Serve an RFC 7233 byte-range request (206) or reject it (416).

        Returns ``True`` when a response was sent; ``False`` when the caller
        should fall through to a full 200 (no Range header, an ignored
        multi-range, a suffix covering the whole file, or an ``If-Range``
        precondition mismatch).
        """
        range_header = self.headers.get("Range")
        if not range_header:
            return False
        if_range = self.headers.get("If-Range")
        if etag and if_range and if_range != etag:
            # If-Range precondition failed → ignore Range (RFC 7233 §3.2)
            return False
        size = stat_info.st_size
        try:
            parsed = self._parse_range_header(range_header, size)
        except ValueError:
            self._send_416(size)
            return True
        if parsed is None:
            return False
        start, end = parsed
        self.send_response(206)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        if etag:
            self.send_header("ETag", etag)
        if last_modified:
            self.send_header("Last-Modified", last_modified)
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self._stream_file(file_path, start, end - start + 1)
        return True

    def _send_full_file(
        self,
        file_path: str,
        stat_info: os.stat_result,
        mime_type: str,
        *,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        cache_control: Optional[str] = None,
        disposition: Optional[str] = None,
        body: Optional[bytes] = None,
        gzip: bool = False,
    ) -> None:
        """Send a full 200 file response.

        ``body`` holds pre-read (possibly gzip-compressed) bytes; when
        ``None`` the file is streamed from disk, using zero-copy sendfile
        for files at or above ``_SENDFILE_THRESHOLD``.
        """
        size = stat_info.st_size
        self.send_response(200)
        self.send_header("Content-Type", mime_type)
        if disposition:
            self.send_header("Content-Disposition", disposition)
        if body is not None:
            self.send_header("Content-Length", str(len(body)))
        else:
            self.send_header("Content-Length", str(size))
        if last_modified:
            self.send_header("Last-Modified", last_modified)
        if etag:
            self.send_header("ETag", etag)
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        self.send_header("Accept-Ranges", "bytes")
        if gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        if body is not None:
            self.wfile.write(body)
        elif size >= _SENDFILE_THRESHOLD:
            self._stream_file(file_path, 0, size)
        else:
            try:
                with open(file_path, "rb") as f:
                    self.wfile.write(f.read())
            except OSError:
                pass  # connection likely broken; handler will close it

    def _handle_static_file(self) -> None:
        """Serve static files from the web/dist directory.

        Supports:
        - gzip compression for text-based MIME types (when client
          advertises ``Accept-Encoding: gzip``).
        - Conditional requests via ``If-None-Match`` (ETag) and
          ``If-Modified-Since`` → 304 Not Modified.
        - Byte-range requests (RFC 7233) → 206 Partial Content, enabling
          e.g. pdf.js chunked PDF loading and video seeking.
        - Zero-copy ``sendfile`` streaming for large files.
        - Cache-Control with a long max-age for content-hashed assets
          and a short (or ``no-cache``) lifetime for HTML / non-hashed.
        """
        static_dir = self.server.static_dir  # type: ignore[attr-defined]
        if static_dir is None:
            self._send_json_error(404, f"Not found: {self.path}")
            return

        # Strip query string
        url_path = self.path.split("?")[0]

        # Try to serve the exact file first
        if url_path == "/":
            file_path = os.path.join(static_dir, "index.html")
        else:
            file_path = os.path.join(static_dir, url_path.lstrip("/"))

        # Prevent path traversal
        file_path = os.path.realpath(file_path)
        if not file_path.startswith(os.path.realpath(static_dir)):
            self._send_json_error(403, "Forbidden")
            return

        if not os.path.isfile(file_path):
            # Fall back to index.html for SPA client-side routing
            file_path = os.path.join(static_dir, "index.html")

        if not os.path.isfile(file_path):
            self._send_json_error(404, f"Not found: {self.path}")
            return

        # ---------- file metadata ----------
        try:
            stat_info = os.stat(file_path)
        except OSError:
            self._send_json_error(500, "Failed to stat file")
            return

        mime_type, _ = mimetypes.guess_type(file_path)
        mime_type = mime_type or "application/octet-stream"
        last_modified = datetime.datetime.fromtimestamp(
            stat_info.st_mtime, tz=datetime.timezone.utc
        )
        etag = self._file_etag(file_path, stat_info)

        # ---------- cache-control ----------
        filename = os.path.basename(file_path)
        if self._is_hashed_asset(filename):
            # Content-hashed assets are immutable → 1 year
            cache_control = "public, max-age=31536000, immutable"
        elif filename == "index.html" or mime_type == "text/html":
            # HTML should always be revalidated (SPA entry point)
            cache_control = "no-cache"
        else:
            # Other assets: cache for 1 hour, allow stale for 1 day
            cache_control = "public, max-age=3600, stale-while-revalidate=86400"

        last_modified_str = last_modified.strftime("%a, %d %b %Y %H:%M:%S GMT")

        # ---------- conditional request ----------
        if_none_match = self.headers.get("If-None-Match", "")
        if if_none_match and if_none_match == etag:
            self._send_304(last_modified, etag, mime_type)
            return

        if_modified_since = self.headers.get("If-Modified-Since", "")
        if if_modified_since:
            try:
                # Parse IMF-fixdate (RFC 7231 §7.1.1.1)
                ims = datetime.datetime.strptime(
                    if_modified_since, "%a, %d %b %Y %H:%M:%S %Z"
                ).replace(tzinfo=datetime.timezone.utc)
                # Truncate both to seconds for comparison per HTTP spec
                if last_modified.replace(microsecond=0) <= ims:
                    self._send_304(last_modified, etag, mime_type)
                    return
            except ValueError:
                pass  # malformed header → serve full response

        # ---------- byte-range request (RFC 7233) ----------
        if self._serve_file_range(
            file_path,
            stat_info,
            mime_type,
            etag=etag,
            last_modified=last_modified_str,
            cache_control=cache_control,
        ):
            return

        # ---------- read / gzip ----------
        # Read into memory only when the response may be gzipped or the file
        # is small enough that buffering is cheaper than the sendfile path.
        size = stat_info.st_size
        accept_encoding = self.headers.get("Accept-Encoding", "")
        supports_gzip = "gzip" in accept_encoding.lower()
        gzip_candidate = (
            supports_gzip
            and self._is_compressible(mime_type)
            and size > 256
            and size <= _GZIP_MAX_SIZE
        )
        if gzip_candidate or size < _SENDFILE_THRESHOLD:
            try:
                with open(file_path, "rb") as f:
                    data = f.read()
            except OSError:
                self._send_json_error(500, "Failed to read file")
                return
        else:
            data = None  # streamed from disk via sendfile below

        do_gzip = False
        if data is not None and gzip_candidate:
            cache_key = (file_path, stat_info.st_mtime_ns, stat_info.st_size)
            with _STATIC_GZIP_CACHE_LOCK:
                compressed = _STATIC_GZIP_CACHE.get(cache_key)
            if compressed is None:
                buf = io.BytesIO()
                with gzip.GzipFile(fileobj=buf, mode="wb", mtime=stat_info.st_mtime) as gz:
                    gz.write(data)
                compressed = buf.getvalue()
                if len(compressed) < len(data):
                    with _STATIC_GZIP_CACHE_LOCK:
                        if len(_STATIC_GZIP_CACHE) >= _STATIC_GZIP_CACHE_MAX:
                            _STATIC_GZIP_CACHE.clear()
                        _STATIC_GZIP_CACHE[cache_key] = compressed
                else:
                    # Not worth compressing — fall through and serve raw bytes.
                    compressed = None
            if compressed is not None:
                data = compressed
                do_gzip = True

        # ---------- response ----------
        self._send_full_file(
            file_path,
            stat_info,
            mime_type,
            etag=etag,
            last_modified=last_modified_str,
            cache_control=cache_control,
            body=data,
            gzip=do_gzip,
        )

    def _send_304(
        self,
        last_modified: datetime.datetime,
        etag: str,
        mime_type: str,
    ) -> None:
        """Send a 304 Not Modified response with appropriate headers."""
        self.send_response(304)
        self.send_header("Content-Type", mime_type)
        self.send_header("Last-Modified", last_modified.strftime("%a, %d %b %Y %H:%M:%S GMT"))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    # ------------------------------------------------------------------
    # WebSocket support
    # ------------------------------------------------------------------

    def _handle_websocket(self) -> None:
        """Handle WebSocket upgrade request and start PTY session."""
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send_json_error(400, "Missing Sec-WebSocket-Key")
            return

        # The socket is hijacked below — do not let the HTTP/1.1 keep-alive
        # loop try to parse further requests on this connection afterwards.
        self.close_connection = True

        # Parse terminal_id and optional params from query string
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        terminal_id = params.get("terminal_id", [None])[0]
        initial_cols = int(params["cols"][0]) if params.get("cols") else None
        initial_rows = int(params["rows"][0]) if params.get("rows") else None
        workspace = params.get("workspace", [None])[0]
        if workspace:
            from runtime.common import set_request_context as _set_ctx
            _set_ctx(workspace=workspace)

        # Perform WebSocket handshake
        magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        accept_key = base64.b64encode(
            hashlib.sha1((key + magic).encode("utf-8")).digest()
        ).decode()

        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_key}\r\n\r\n"
        )
        sock = self.connection
        # The socket inherits the HTTP handler's idle timeout (self.timeout=30,
        # applied in BaseHTTPRequestHandler.setup for keep-alive connections).
        # A WebSocket/PTY terminal must stay alive while idle (e.g. the user is
        # just reading the shell prompt), so clear the timeout on the hijacked
        # socket.  Otherwise an idle terminal is force-closed after 30s and the
        # frontend falls into a reconnect loop.
        try:
            sock.settimeout(None)
        except OSError:
            pass
        sock.sendall(response.encode("utf-8"))

        logger.info("WebSocket connection established: terminal_id=%s, cols=%s, rows=%s", terminal_id, initial_cols, initial_rows)
        self._start_pty_session(sock, terminal_id, initial_cols, initial_rows)

    @staticmethod
    def _ws_send_frame(sock, text: str) -> None:
        """Encode and send a WebSocket text frame."""
        data = text.encode("utf-8", errors="replace")
        length = len(data)
        frame = bytearray([0x81])  # FIN + TEXT

        if length <= 125:
            frame.append(length)
        elif length <= 65535:
            frame.append(126)
            frame.extend(struct.pack("!H", length))
        else:
            frame.append(127)
            frame.extend(struct.pack("!Q", length))

        frame.extend(data)
        try:
            sock.sendall(frame)
        except OSError:
            pass

    @staticmethod
    def _ws_recv_frame(sock) -> Optional[str]:
        """Receive and decode a WebSocket frame."""
        try:
            header = sock.recv(2)
            if not header:
                return None
            b1, b2 = header[0], header[1]

            opcode = b1 & 0x0F
            if opcode == 8:  # Close frame
                return None

            payload_len = b2 & 0x7F
            if payload_len == 126:
                payload_len = struct.unpack("!H", sock.recv(2))[0]
            elif payload_len == 127:
                payload_len = struct.unpack("!Q", sock.recv(8))[0]

            masking_key = sock.recv(4)
            raw_data = sock.recv(payload_len)

            # Unmask data
            unmasked = bytearray(
                b ^ masking_key[i % 4] for i, b in enumerate(raw_data)
            )
            return unmasked.decode("utf-8", errors="ignore")
        except (OSError, struct.error):
            return None

    def _start_pty_session(self, sock, terminal_id: Optional[str] = None,
                            initial_cols: Optional[int] = None,
                            initial_rows: Optional[int] = None) -> None:
        """Start a PTY session over WebSocket connection.
        
        Args:
            sock: WebSocket connection socket
            terminal_id: Optional terminal session ID (format: session_id or session_id:assistant_id)
            initial_cols: Terminal columns from the client
            initial_rows: Terminal rows from the client
        """
        if sys.platform == "win32":
            self._start_pty_session_win32(sock, terminal_id, initial_cols, initial_rows)
        else:
            self._start_pty_session_unix(sock, terminal_id, initial_cols, initial_rows)

    def _start_pty_session_win32(self, sock, terminal_id: Optional[str] = None,
                                  initial_cols: Optional[int] = None,
                                  initial_rows: Optional[int] = None) -> None:
        """Windows PTY session using winpty.
        
        Supports session persistence: if a terminal session with the same ID already exists,
        reconnect to it instead of creating a new one.
        """
        if PtyProcess is None:
            self._ws_send_frame(sock, '{"error": "winpty not available"}')
            return

        # Get or create terminal
        session_id = terminal_id.split(":")[0] if terminal_id else None
        if session_id:
            terminal_info = get_or_create_terminal(
                session_id,
                cols=initial_cols or 80,
                rows=initial_rows or 24,
                send_output=lambda data: self._ws_send_frame(sock, data),
            )
        else:
            terminal_info = None
        
        if not terminal_info:
            self._ws_send_frame(sock, '{"error": "Failed to create terminal"}')
            return
        
        # Attach the browser to the terminal's persistent WinPTY reader.
        # Do not start/stop readers here: concurrent proc.read() calls split
        # PowerShell VT redraw sequences and produce duplicated/wrapped input.
        with _terminal_sessions_lock:
            terminal_info["sock"] = sock
            terminal_info["disconnected_at"] = None
            terminal_info["send_output"] = lambda data: self._ws_send_frame(sock, data)
        
        proc = terminal_info.get("proc")
        
        # Resize PTY to match the client's terminal dimensions on reconnect
        if proc and initial_cols and initial_rows:
            try:
                proc.setwinsize(initial_rows, initial_cols)
            except Exception:
                pass
        
        # Send terminal_id to client
        if terminal_id:
            self._ws_send_frame(sock, json.dumps({"__terminal_id": terminal_id}))
        
        try:
            while True:
                user_input = self._ws_recv_frame(sock)
                if user_input is None:
                    break

                # Handle resize command
                if user_input.startswith('{"__resize":'):
                    try:
                        msg = json.loads(user_input)
                        proc.setwinsize(msg["rows"], msg["cols"])
                        continue
                    except Exception:
                        pass

                proc.write(user_input)
        finally:
            # Mark session as disconnected but don't terminate
            if terminal_info:
                with _terminal_sessions_lock:
                    if terminal_info.get("sock") is sock:
                        terminal_info["disconnected_at"] = time.monotonic()
                        terminal_info["sock"] = None
                        terminal_info["send_output"] = None
            sock.close()

    def _start_pty_session_unix(self, sock, terminal_id: Optional[str] = None,
                                initial_cols: Optional[int] = None,
                                initial_rows: Optional[int] = None) -> None:
        """Unix PTY session using pty.fork().
        
        Supports session persistence: if a terminal session with the same ID already exists,
        reconnect to it instead of creating a new one.
        """
        # Get or create terminal
        session_id = terminal_id.split(":")[0] if terminal_id else None
        if session_id:
            terminal_info = get_or_create_terminal(
                session_id,
                cols=initial_cols or 80,
                rows=initial_rows or 24,
            )
        else:
            terminal_info = None
        
        if not terminal_info:
            self._ws_send_frame(sock, '{"error": "Failed to create terminal"}')
            return
        
        master_fd = terminal_info.get("master_fd")
        
        # Attach socket to terminal
        with _terminal_sessions_lock:
            terminal_info["active"] = False  # Signal old read_pty to stop
            terminal_info["sock"] = sock
            terminal_info["disconnected_at"] = None
        
        time.sleep(0.1)
        
        with _terminal_sessions_lock:
            terminal_info["active"] = True
        
        # Resize PTY to match the client's terminal dimensions on reconnect
        if master_fd and initial_cols and initial_rows:
            try:
                winsize = struct.pack("HHHH", initial_rows, initial_cols, 0, 0)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
            except Exception:
                pass
        
        # Send terminal_id to client
        if terminal_id:
            self._ws_send_frame(sock, json.dumps({"__terminal_id": terminal_id}))
        
        def read_pty():
            """Read from PTY and send to WebSocket."""
            while terminal_info.get("active", True):
                try:
                    ready, _, _ = select.select([master_fd], [], [], 0.1)
                    if ready:
                        data = os.read(master_fd, 1024)
                        if not data:
                            break
                        data = data.decode("utf-8", errors="replace")
                        if data:
                            current_sock = terminal_info.get("sock")
                            if current_sock:
                                try:
                                    self._ws_send_frame(current_sock, data)
                                except Exception:
                                    pass
                            if "buffer_lock" in terminal_info:
                                with terminal_info["buffer_lock"]:
                                    terminal_info["output_buffer"].append(data)
                except OSError:
                    break

        threading.Thread(target=read_pty, daemon=True).start()

        def set_winsize(fd, rows, cols):
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

        try:
            while True:
                user_input = self._ws_recv_frame(sock)
                if user_input is None:
                    break

                # Handle resize command
                if user_input.startswith('{"__resize":'):
                    try:
                        msg = json.loads(user_input)
                        set_winsize(master_fd, msg["rows"], msg["cols"])
                        continue
                    except Exception:
                        pass

                # Regular keyboard input
                os.write(master_fd, user_input.encode("utf-8"))
        finally:
            # On disconnect: don't kill the PTY process, just mark as disconnected
            # The session will stay alive for potential reconnection
            if terminal_info:
                with _terminal_sessions_lock:
                    terminal_info["active"] = False  # Mark this connection as inactive
                    terminal_info["disconnected_at"] = time.monotonic()
                    terminal_info["sock"] = None
            
            sock.close()
            logger.info("Terminal session disconnected: %s (session stays alive for reconnection)", terminal_id)

    def do_POST(self) -> None:
        """Handle POST requests."""
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path.startswith("/v1/") and not self._require_authorized("POST", path):
            return

        is_inference = path in {"/v1/infer", "/v1/infer/stream"}
        inference_registered = False
        if is_inference:
            lock = getattr(self.server, "inference_update_lock", None)
            if lock is not None:
                with lock:
                    if getattr(self.server, "update_in_progress", False):
                        self._send_json_response(409, {
                            "error": "update_in_progress",
                            "message": "Inference is temporarily unavailable while an update is being applied",
                        })
                        return
                    self.server.active_inference_count = int(getattr(self.server, "active_inference_count", 0) or 0) + 1
                    inference_registered = True

        try:
            if not self._dispatch_route("POST", path):
                self._send_json_error(404, f"Not found: {self.path}")
        finally:
            if inference_registered:
                lock = getattr(self.server, "inference_update_lock", None)
                if lock is not None:
                    with lock:
                        self.server.active_inference_count = max(
                            0, int(getattr(self.server, "active_inference_count", 0) or 0) - 1
                        )

    def do_PUT(self) -> None:
        """Handle PUT requests."""
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path.startswith("/v1/") and not self._require_authorized("PUT", path):
            return
        if not self._dispatch_route("PUT", path):
            self._send_json_error(404, f"Not found: {self.path}")

    def do_DELETE(self) -> None:
        """Handle DELETE requests."""
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path.startswith("/v1/") and not self._require_authorized("DELETE", path):
            return
        if not self._dispatch_route("DELETE", path):
            self._send_json_error(404, f"Not found: {self.path}")

    # ------------------------------------------------------------------
    # Auth handlers
    # ------------------------------------------------------------------

    def _handle_auth_login(self) -> None:
        auth_manager = self._get_auth_manager()
        if not auth_manager.is_enabled():
            self._send_json_response(200, {"ok": True, "auth_enabled": False})
            return
        data = self._read_json_body()
        if data is None:
            return
        password = data.get("password", "")
        if not auth_manager.verify_password(str(password)):
            self._send_json_response(401, {"error": "invalid_password", "message": "Invalid password"})
            return
        token, max_age = auth_manager.create_session_token()
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_session_cookie(token, max_age)
        self.end_headers()
        self.wfile.write(body)

    def _handle_auth_logout(self) -> None:
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._clear_session_cookie()
        self.end_headers()
        self.wfile.write(body)

    def _handle_auth_config_get(self) -> None:
        auth_manager = self._get_auth_manager()
        self._send_json_response(200, auth_manager.status(include_setup_token=True))

    def _handle_auth_config_post(self) -> None:
        data = self._read_json_body()
        if data is None:
            return
        disable_auth = bool(data.get("disable_auth"))
        password = data.get("password", None)
        if password is not None:
            password = str(password)
        ttl = data.get("cookie_ttl_seconds", None)
        if ttl is not None:
            try:
                ttl = int(ttl)
            except (TypeError, ValueError):
                self._send_json_response(400, {"error": "invalid_cookie_ttl", "message": "Invalid cookie TTL"})
                return
        auth_manager = self._get_auth_manager()
        try:
            if disable_auth:
                result = auth_manager.disable_auth()
            else:
                result = auth_manager.update_config(password=password, cookie_ttl_seconds=ttl)
        except ValueError as exc:
            code = "invalid_password_format" if "Password" in str(exc) else "invalid_cookie_ttl"
            self._send_json_response(400, {"error": code, "message": str(exc)})
            return
        except RuntimeError as exc:
            self._send_json_response(500, {"error": "disable_auth_failed", "message": str(exc)})
            return
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if result.get("has_password"):
            token, max_age = auth_manager.create_session_token()
            self._send_session_cookie(token, max_age)
        else:
            self._clear_session_cookie()
        self.end_headers()
        self.wfile.write(body)
