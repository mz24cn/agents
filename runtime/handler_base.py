"""Base handler mixin: shared helpers, routing, static files, WebSocket/PTY, auth.

Part of the ``_RuntimeRequestHandler`` decomposition in ``runtime.server``.
This mixin provides the building blocks shared by every endpoint: CORS,
JSON helpers, authorization, the do_* dispatchers, static file serving,
the WebSocket/PTY terminal support and the auth handlers.

Zero third-party dependencies — only Python standard library.
"""

from __future__ import annotations

import datetime
import gzip
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
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
    import fcntl
    import termios

from runtime import wsutil
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
from runtime.terminal_env_bridge import LOCAL_ENV_ID, EnvMuxBridge

logger = logging.getLogger("runtime.server")

# Maximum accepted JSON request body (bytes).  Larger bodies are rejected with
# 413 and the connection is closed, so a public-facing instance cannot be
# pushed to buffer multi-gigabyte payloads (memory exhaustion / DoS).
_MAX_JSON_BODY_BYTES = 64 * 1024 * 1024

# Pushed update deltas (POST /v1/setup?op=push) are raw tar.gz bodies. A full
# delta (first update of a child) can carry the whole deployed project plus
# web/dist, so this limit is far above any realistic update while still
# bounding memory.
_MAX_PUSH_BODY_BYTES = 512 * 1024 * 1024

# Terminals a single multiplexed terminal socket (/v1/terminals/ws) may
# carry.  Far above what a browser tab realistically opens (one per session
# with a terminal ever shown) while still bounding the fds one connection can
# hold open.
_MAX_MUX_CHANNELS = 32


def _mux_dim(value, default: int) -> int:
    """Coerce a terminal dimension from a mux frame, clamped to something sane."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(n, 10000))


# ---------------------------------------------------------------------------
# Streaming request-body readers
# ---------------------------------------------------------------------------
# The tunnel browser bridge forwards a request body to the child as it is
# read off the browser socket, instead of buffering the whole thing first.
# These module-level generators yield the body in bounded blocks (the caller
# frames each block into one tunnel binary frame).  They raise instead of
# sending a response so the caller can decide what to do (the tunnel proxy
# has already put the request header on the wire by the time the body is read).

class _BodyReaderError(Exception):
    """The request body could not be read (malformed / truncated / closed)."""


class _BodyTooLargeError(_BodyReaderError):
    """The request body exceeded the configured size cap."""


def _read_block(rfile, n: int) -> bytes:
    """``rfile.read(n)``, mapping a socket error to ``_BodyReaderError``."""
    try:
        block = rfile.read(n)
    except OSError as exc:
        raise _BodyReaderError(str(exc)) from exc
    if not block:
        raise _BodyReaderError("truncated request body")
    return block


def _read_line(rfile, limit: int) -> bytes:
    """``rfile.readline(limit)``, mapping a socket error to ``_BodyReaderError``."""
    try:
        return rfile.readline(limit)
    except OSError as exc:
        raise _BodyReaderError(str(exc)) from exc


def iter_raw_body(rfile, content_length: int, max_bytes: int,
                  block_size: int = 65536):
    """Yield a fixed-length request body in ``block_size`` blocks.

    Raises ``_BodyTooLargeError`` when the declared length exceeds
    *max_bytes* (before reading anything) and ``_BodyReaderError`` when the
    stream ends before *content_length* bytes arrived (client hung up).
    """
    if content_length > max_bytes:
        raise _BodyTooLargeError()
    remaining = content_length
    while remaining > 0:
        block = _read_block(rfile, min(block_size, remaining))
        remaining -= len(block)
        yield block


def iter_chunked_body(rfile, max_bytes: int, block_size: int = 65536):
    """Yield a ``Transfer-Encoding: chunked`` request body chunk by chunk.

    Decodes the chunk framing on the fly (some mobile browsers send Blob PUT
    bodies chunked, without a Content-Length).  Raises ``_BodyTooLargeError``
    once the running total exceeds *max_bytes* and ``_BodyReaderError`` on any
    malformed / truncated framing.
    """
    total = 0
    while True:
        size_line = _read_line(rfile, 65537)
        if not size_line or b"\n" not in size_line:
            raise _BodyReaderError("bad chunk size line")
        size_token = size_line.strip().split(b";", 1)[0]
        try:
            size = int(size_token, 16)
        except ValueError:
            raise _BodyReaderError("bad chunk size") from None
        if size == 0:
            # Consume trailer headers up to the blank line.
            while True:
                line = _read_line(rfile, 65537)
                if line in (b"\r\n", b"\n", b""):
                    break
            return
        if total + size > max_bytes:
            raise _BodyTooLargeError()
        remaining = size
        while remaining > 0:
            block = _read_block(rfile, min(block_size, remaining))
            total += len(block)
            remaining -= len(block)
            yield block
        # RFC 7230: each chunk is followed by CRLF.
        if _read_block(rfile, 2) != b"\r\n":
            raise _BodyReaderError("missing chunk CRLF")


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
        # Probes an upstream endpoint's own model list.  Listed before the
        # generic /v1/models entry so the more specific path wins on review;
        # both patterns are anchored, so order is not load-bearing here.
        (re.compile(r"^/v1/models/probe$"), "_handle_probe_models", ()),
        (re.compile(r"^/v1/models$"), "_handle_list_models", ()),
        (re.compile(r"^/v1/tools$"), "_handle_list_tools", ()),
        (re.compile(r"^/v1/tools/skill/([^/]+)$"), "_handle_get_skill_body", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/mcp-servers$"), "_handle_list_mcp_servers", ()),
        (re.compile(r"^/v1/prompt-templates$"), "_handle_list_prompt_templates", ()),
        (re.compile(r"^/v1/env$"), "_handle_get_env", ()),
        (re.compile(r"^/v1/remote-envs$"), "_handle_remote_envs_list", ()),
        (re.compile(r"^/v1/auth/config$"), "_handle_auth_config_get", ()),
        (re.compile(r"^/v1/setup$"), "_handle_setup_script", ()),
        (re.compile(r"^/v1/tunnel/parent/status$"), "_handle_tunnel_parent_status", ()),
        (re.compile(r"^/v1/sessions/tree$"), "_handle_session_category_tree", ()),
        (re.compile(r"^/v1/sessions$"), "_handle_list_sessions", ()),
        (re.compile(r"^/v1/sessions/search$"), "_handle_search_sessions", ()),
        (re.compile(r"^/v1/sessions/events$"), "_handle_sessions_events", ()),
        # Multiplexed frame channel: one SSE per browser tab. Must precede the
        # generic /v1/sessions/{id} route, which would otherwise match "frames".
        (re.compile(r"^/v1/sessions/frames$"), "_handle_session_frames", ()),
        (re.compile(r"^/v1/sessions/([^/]+)/stream$"), "_handle_session_stream", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/sessions/([^/]+)/log-dir$"), "_handle_session_log_dir", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/sessions/([^/]+)/execution-analysis$"), "_handle_session_execution_analysis", (urllib.parse.unquote,)),
        # meta only (no conversation): cheap enough for the browser to resolve a
        # *background* session's bindings before it opens that session's terminal.
        (re.compile(r"^/v1/sessions/([^/]+)/meta$"), "_handle_session_meta", (urllib.parse.unquote,)),
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
        # Voice input: is the ASR accessory service (accessories/ASR) up?
        (re.compile(r"^/v1/asr/health$"), "_handle_asr_health", ()),
        # Parent-side browser bridge: env_id in path, rest forwarded to child.
        (re.compile(r"^/v1/env-proxy/([^/]+)(?:/.*)?$"), "_handle_env_proxy", (urllib.parse.unquote,)),
    ],
    "POST": [
        (re.compile(r"^/v1/auth/login$"), "_handle_auth_login", ()),
        (re.compile(r"^/v1/auth/logout$"), "_handle_auth_logout", ()),
        (re.compile(r"^/v1/auth/config$"), "_handle_auth_config_post", ()),
        # op=push: the parent pushes a delta tar to this environment; the
        # handler dispatches on ?op= just like the GET route.
        (re.compile(r"^/v1/setup$"), "_handle_setup_script", ()),
        (re.compile(r"^/v1/infer$"), "_handle_infer", ()),
        (re.compile(r"^/v1/infer/stream$"), "_handle_infer_stream", ()),
        (re.compile(r"^/v1/infer/abort$"), "_handle_infer_abort", ()),
        (re.compile(r"^/v1/infer/interject$"), "_handle_infer_interject", ()),
        (re.compile(r"^/v1/tools/call$"), "_handle_tool_call", ()),
        (re.compile(r"^/v1/tools/mcp$"), "_handle_register_mcp_servers", ()),
        (re.compile(r"^/v1/tools/test$"), "_handle_test_tool", ()),
        (re.compile(r"^/v1/tools/skill$"), "_handle_register_skill", ()),
        (re.compile(r"^/v1/models$"), "_handle_register_model", ()),
        (re.compile(r"^/v1/tools$"), "_handle_register_tool", ()),
        (re.compile(r"^/v1/prompt-templates$"), "_handle_create_prompt_template", ()),
        (re.compile(r"^/v1/env$"), "_handle_set_env", ()),
        (re.compile(r"^/v1/remote-envs$"), "_handle_remote_envs_add", ()),
        (re.compile(r"^/v1/remote-envs/(.+)/push-update$"), "_handle_remote_envs_push_update", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/remote-envs/([^/]+)/hello$"), "_handle_remote_envs_hello", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/tunnel/register$"), "_handle_tunnel_register", ()),
        (re.compile(r"^/v1/tunnel/parent/register$"), "_handle_tunnel_parent_register", ()),
        (re.compile(r"^/v1/tunnel/parent/unregister$"), "_handle_tunnel_parent_unregister", ()),
        (re.compile(r"^/v1/env/detect$"), "_handle_detect_env", ()),
        # Multiplexed frame channel commands (see GET /v1/sessions/frames).
        (re.compile(r"^/v1/sessions/frames/subscribe$"), "_handle_session_frames_subscribe", ()),
        (re.compile(r"^/v1/sessions/frames/unsubscribe$"), "_handle_session_frames_unsubscribe", ()),
        (re.compile(r"^/v1/sessions/frames/close$"), "_handle_session_frames_close", ()),
        (re.compile(r"^/v1/sessions/([^/]+)/generate-title$"), "_handle_generate_session_title", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/sessions/([^/]+)/regenerate-summary$"), "_handle_regenerate_session_summary", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/sessions/([^/]+)/read$"), "_handle_mark_session_read", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/sessions/([^/]+)/flight$"), "_handle_session_flight", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/sessions/([^/]+)/revoke$"), "_handle_revoke_session", (urllib.parse.unquote,)),
        # Derive a new session from one assistant message (conversation copy).
        (re.compile(r"^/v1/sessions/([^/]+)/fork$"), "_handle_fork_session", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/agents$"), "_handle_create_agent", ()),
        (re.compile(r"^/v1/workspace/rename$"), "_handle_workspace_rename", ()),
        (re.compile(r"^/v1/workspace/mkdir$"), "_handle_workspace_mkdir", ()),
        (re.compile(r"^/v1/workspace/duplicate$"), "_handle_workspace_duplicate", ()),
        (re.compile(r"^/v1/workspace/move$"), "_handle_workspace_move", ()),
        (re.compile(r"^/v1/workspace/copy$"), "_handle_workspace_copy", ()),
        (re.compile(r"^/v1/workspace/upload/init$"), "_handle_workspace_upload_init", ()),
        (re.compile(r"^/v1/workspace/upload/([^/]+)/complete$"), "_handle_workspace_upload_complete", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/env-proxy/([^/]+)(?:/.*)?$"), "_handle_env_proxy", (urllib.parse.unquote,)),
    ],
    "PUT": [
        (re.compile(r"^/v1/models/([^/]+)$"), "_handle_update_model", ()),
        (re.compile(r"^/v1/tools/([^/]+)$"), "_handle_update_tool", ()),
        (re.compile(r"^/v1/mcp-servers/([^/]+)$"), "_handle_restore_mcp_server_config", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/prompt-templates/([^/]+)$"), "_handle_update_prompt_template", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/agents/([^/]+)$"), "_handle_update_agent", ()),
        (re.compile(r"^/v1/remote-envs/(.+)$"), "_handle_remote_envs_update", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/workspace/upload/([^/]+)/chunk/(\d+)$"), "_handle_workspace_upload_chunk", (urllib.parse.unquote, int)),
        (re.compile(r"^/v1/env-proxy/([^/]+)(?:/.*)?$"), "_handle_env_proxy", (urllib.parse.unquote,)),
    ],
    "DELETE": [
        (re.compile(r"^/v1/models/([^/]+)$"), "_handle_delete_model", ()),
        (re.compile(r"^/v1/tools/batch$"), "_handle_batch_delete_tools", ()),
        (re.compile(r"^/v1/mcp-servers/([^/]+)$"), "_handle_delete_mcp_server", ()),
        (re.compile(r"^/v1/tools/([^/]+)$"), "_handle_delete_tool", ()),
        (re.compile(r"^/v1/prompt-templates/([^/]+)$"), "_handle_delete_prompt_template", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/env/([^/]+)$"), "_handle_delete_env", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/remote-envs/(.+)$"), "_handle_remote_envs_delete", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/tunnel/register$"), "_handle_tunnel_unregister", ()),
        (re.compile(r"^/v1/sessions/([^/]+)$"), "_handle_delete_session", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/agents/([^/]+)$"), "_handle_delete_agent", ()),
        (re.compile(r"^/v1/workspace/delete$"), "_handle_workspace_delete", ()),
        (re.compile(r"^/v1/workspace/upload/([^/]+)$"), "_handle_workspace_upload_cancel", (urllib.parse.unquote,)),
        (re.compile(r"^/v1/terminals/([^/]+)$"), "_handle_delete_terminal", ()),
        (re.compile(r"^/v1/env-proxy/([^/]+)(?:/.*)?$"), "_handle_env_proxy", (urllib.parse.unquote,)),
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

# Session conversation.json files are rewritten by the incremental persister
# while a session is active, so every write orphans the previous cache entry.
# Use a small dedicated cache (oldest-first eviction) so that re-downloading
# an unchanged session (e.g. switching away and back to it) reuses the
# compressed bytes while the memory footprint stays bounded.
_SESSION_GZIP_CACHE: dict = {}
_SESSION_GZIP_CACHE_LOCK = threading.Lock()
_SESSION_GZIP_CACHE_MAX = 8

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
        if content_length > _MAX_JSON_BODY_BYTES:
            # Reject without buffering the payload; closing the connection
            # keeps the unread bytes from corrupting keep-alive framing.
            self.close_connection = True
            self._send_json_error(413, "Request body too large")
            return None
        raw = self.rfile.read(content_length)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_json_error(400, f"Invalid JSON: {exc}")
            return None

    def _read_raw_body(self, max_bytes: int) -> Optional[bytes]:
        """Read the request body as raw bytes (non-JSON payloads, e.g. op=push).

        Returns:
            Body bytes, or None if the request was rejected (an error
            response has already been sent).
        """
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._send_json_error(400, "Invalid Content-Length")
            return None
        if content_length <= 0:
            self._send_json_error(400, "Empty request body")
            return None
        if content_length > max_bytes:
            # Reject without buffering the payload; closing the connection
            # keeps the unread bytes from corrupting keep-alive framing.
            self.close_connection = True
            self._send_json_error(413, "Request body too large")
            return None
        try:
            raw = self.rfile.read(content_length)
        except OSError:
            self.close_connection = True
            self._send_json_error(400, "Failed to read request body")
            return None
        if len(raw) != content_length:
            # The client closed the connection before the body was
            # complete.  The remaining framing on this connection is
            # untrustworthy, so reject and drop it instead of forwarding a
            # truncated body (on the far side it would silently become a
            # short request with a mismatched Content-Length).
            self.close_connection = True
            self._send_json_error(400, "Truncated request body")
            return None
        return raw

    def _read_chunked_body(self, max_bytes: int) -> Optional[bytes]:
        """Read a request body framed with ``Transfer-Encoding: chunked``.

        Some mobile browsers send Blob PUT bodies chunked, without a
        Content-Length.  The stdlib request handler cannot frame such a
        body itself (it would parse the first chunk-size line as the next
        request line and desync the keep-alive connection), so the body is
        consumed here.  Returns ``None`` when the framing is malformed,
        oversized, or the connection dies mid-body -- the caller sends the
        error response.
        """
        parts: list = []
        try:
            for block in iter_chunked_body(self.rfile, max_bytes):
                parts.append(block)
        except _BodyReaderError:
            return None
        return b"".join(parts)

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
        # Everything except the /v1/ JSON API (including the /v1/terminals/ws
        # terminal endpoint, which spawns a real shell) is public (static files, etc.).
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

        # /v1/setup additionally accepts a setup token (st_...) or an API key
        # (as_...) via the ``token`` query parameter (the setup link carries
        # it), for both GET ops and POST op=push (the parent pushing a delta
        # to a child). The token already carries update rights, so this adds
        # no new privilege. The long-lived API key is accepted as well so a
        # setup link can be persistent: st_ setup tokens expire after one
        # hour, which would break URLs stored for remote env management and
        # as SETUP_SOURCE for online updates.
        # op=hello is NOT exempt: it follows the same authorization as any
        # other /v1/ endpoint (public only when the environment has no auth
        # enabled).
        if method in {"GET", "POST"} and path == "/v1/setup":
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            token = params.get("token", [""])[0]
            if token and (
                auth_manager.verify_setup_token(token)
                or auth_manager.verify_api_key(token)
            ):
                return True

        # GET /v1/* additionally accepts the ``token`` query parameter (setup
        # token st_... or API key as_...). Browsers cannot attach an
        # Authorization header to <img>/<audio>/<video>/iframe/PDF resources
        # or to WebSocket handshakes, so remote execution (file-manager
        # resources, xterm terminal WS, file-journal reads) authenticates via
        # the query parameter. The token is the same credential the setup
        # link already carries, so this adds no new privilege; it does show
        # up in access logs, which is acceptable for LAN deployments.
        if method == "GET":
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            token = params.get("token", [""])[0]
            if token and (
                auth_manager.verify_setup_token(token)
                or auth_manager.verify_api_key(token)
            ):
                return True

        session_token = self._request_cookie(COOKIE_NAME)
        if session_token and auth_manager.verify_session_token(session_token):
            return True

        bearer = self._bearer_token()
        # Bearer accepts the long-lived API key and the setup token: the
        # parent's remote tool proxy and the browser's cross-origin
        # POST/PUT/DELETE (uploads, workspace ops) present the same token
        # from the registered env URL via the Authorization header.
        if bearer and (
            auth_manager.verify_api_key(bearer)
            or auth_manager.verify_setup_token(bearer)
        ):
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
# Secure is set for TLS connections: either the request arrived over this
        # server's own TLS listener (ssl_context set) or a reverse proxy
        # marked it https via X-Forwarded-Proto.
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https" or bool(
            getattr(self.server, "ssl_context", None)
        ):
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
        if path.startswith("/v1/"):
            if not self._require_authorized("GET", path):
                return
            # One multiplexed terminal socket per browser tab: every terminal
            # the tab has opened shares it -- terminals of *child environments*
            # included, which the frame's ``env_id`` routes to the right child
            # (see _handle_terminal_mux_ws).  So N terminals cost one of the
            # browser's ~6 per-origin connections instead of N.  It spawns real
            # shells, so it stays behind the /v1/ auth gate above; the upgrade
            # must be handled before the JSON route table (which would 404 it).
            if path == "/v1/terminals/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
                self._handle_terminal_mux_ws()
                return
            # Tunnel data channel (child → parent reverse tunnel). Like the
            # terminal endpoint it is a plain GET with an Upgrade header and
            # must be handled before the JSON route table; credentials are
            # enforced by the /v1/ gate above (GET ?token= or session cookie).
            # Trust boundary: with parent auth DISABLED there are no
            # credentials to check, so /v1/tunnel/* and /v1/env-proxy/*
            # are open to anyone who can reach this port (a connected
            # tunnel can execute the child's local tools). Keep the port
            # off untrusted networks when the parent has no password set.
            if path == "/v1/tunnel/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
                self._handle_tunnel_ws()
                return
            # Voice input: browser WebSocket bridged to the local ASR
            # accessory service.  Same "Upgrade before the JSON route table"
            # rule as the terminal and tunnel sockets; the /v1/ gate above
            # already checked the session cookie / token.
            if path == "/v1/asr/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
                self._handle_asr_ws()
                return
            if not self._dispatch_route("GET", path):
                self._send_json_error(404, f"Not found: {self.path}")
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

    def _gzip_cached(
        self,
        data: bytes,
        file_path: str,
        stat_info: os.stat_result,
        cache: dict,
        cache_lock: threading.Lock,
        cache_max: int,
    ) -> tuple[bytes, bool]:
        """Gzip-compress *data* and cache the result keyed by file identity.

        The cache key ``(file_path, mtime_ns, size)`` means any rewrite of
        the file (new mtime/size) automatically invalidates the entry, so
        repeat requests for an unchanged file reuse the compressed bytes
        without re-compressing. Returns ``(compressed, True)`` when gzip
        shrank the payload, otherwise ``(data, False)`` so the caller
        serves the raw bytes.
        """
        cache_key = (file_path, stat_info.st_mtime_ns, stat_info.st_size)
        with cache_lock:
            compressed = cache.get(cache_key)
        if compressed is None:
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb", mtime=stat_info.st_mtime) as gz:
                gz.write(data)
            compressed = buf.getvalue()
            if len(compressed) >= len(data):
                # Not worth compressing — serve raw bytes (and do not cache).
                return data, False
            with cache_lock:
                if len(cache) >= cache_max:
                    # Evict the oldest entry (dicts keep insertion order) so
                    # a steady stream of rewrites cannot flush live entries.
                    cache.pop(next(iter(cache)), None)
                cache[cache_key] = compressed
        return compressed, True

    def _serve_file_response(
        self,
        file_path: str,
        mime_type: str,
        *,
        cache_control: Optional[str] = None,
        disposition: Optional[str] = None,
        gzip_cache: Optional[tuple] = None,
    ) -> None:
        """Serve a local file with the full static-file treatment.

        Shared by the static-file endpoint and the session download endpoint
        (``GET /v1/sessions/{id}`` serves the conversation.json bytes
        directly). Provides:

        - conditional requests via ``If-None-Match`` (ETag) and
          ``If-Modified-Since`` → 304 Not Modified (cache reuse);
        - byte-range requests (RFC 7233) → 206 Partial Content, enabling
          e.g. pdf.js chunked PDF loading and video seeking;
        - gzip compression for text-based MIME types (when client
          advertises ``Accept-Encoding: gzip``), with the compressed bytes
          cached per (path, mtime, size);
        - zero-copy ``sendfile`` streaming for large files.

        ``gzip_cache`` selects the compressed-bytes cache as
        ``(dict, lock, max_entries)``; it defaults to the static-file cache.
        """
        try:
            stat_info = os.stat(file_path)
        except OSError:
            self._send_json_error(500, "Failed to stat file")
            return

        last_modified = datetime.datetime.fromtimestamp(
            stat_info.st_mtime, tz=datetime.timezone.utc
        )
        etag = self._file_etag(file_path, stat_info)
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
            disposition=disposition,
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
            if gzip_cache is None:
                gzip_cache = (
                    _STATIC_GZIP_CACHE,
                    _STATIC_GZIP_CACHE_LOCK,
                    _STATIC_GZIP_CACHE_MAX,
                )
            cache, cache_lock, cache_max = gzip_cache
            data, do_gzip = self._gzip_cached(
                data, file_path, stat_info, cache, cache_lock, cache_max
            )

        # ---------- response ----------
        self._send_full_file(
            file_path,
            stat_info,
            mime_type,
            etag=etag,
            last_modified=last_modified_str,
            cache_control=cache_control,
            disposition=disposition,
            body=data,
            gzip=do_gzip,
        )

    def _handle_static_file(self) -> None:
        """Serve static files from the web/dist directory.

        Path resolution (exact file → SPA index.html fallback, traversal
        guard) and the Cache-Control policy live here; the shared transport
        (304 revalidation, 206 ranges, gzip + compressed-bytes cache,
        sendfile) is ``_serve_file_response``.
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

        mime_type, _ = mimetypes.guess_type(file_path)
        mime_type = mime_type or "application/octet-stream"

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

        self._serve_file_response(file_path, mime_type, cache_control=cache_control)

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

    @staticmethod
    def _ws_send_frame(sock, text: str) -> None:
        """Encode and send a WebSocket text frame."""
        try:
            wsutil.ws_send_text(sock, text)
        except OSError:
            pass

    @staticmethod
    def _ws_recv_frame(sock) -> Optional[str]:
        """Receive and decode the next text frame from a WebSocket client.

        Built on ``wsutil.ws_recv_frame`` (robust against short reads).
        Ping frames are answered with pong, pong/continuation frames are
        dropped, and close/EOF yield None.  Binary frames are decoded as
        UTF-8 text so a client choosing binary still drives the PTY.
        """
        while True:
            frame = wsutil.ws_recv_frame(sock, expect_masked=True)
            if frame is None:
                return None
            opcode, payload = frame
            if opcode == wsutil.OP_PING:
                wsutil.ws_send_pong(sock, payload)
                continue
            if opcode in (wsutil.OP_PONG, wsutil.OP_CONT):
                continue
            return payload.decode("utf-8", errors="ignore")

    # ------------------------------------------------------------------
    # Multiplexed terminal WebSocket (one socket per browser tab)
    # ------------------------------------------------------------------

    def _handle_terminal_mux_ws(self) -> None:
        """Handle ``WS /v1/terminals/ws``: every terminal of one tab on one socket.

        The browser's per-origin HTTP/1.1 pool is only ~6 connections wide and a
        WebSocket counts against it, so one socket per terminal lets a handful of
        open terminals starve ordinary requests.  Here the tab keeps a single
        socket and every frame carries its channel id:

        - client → server ``{"op": "open"|"input"|"resize"|"close", "c": id, ...}``
        - server → client ``{"op": "opened"|"closed"|"error", "c": id}`` for
          control, ``{"c": id, "d": chunk}`` for PTY output.

        ``open`` also carries ``env_id``, which is what makes the single socket
        cover **child environments** too (runtime/terminal_env_bridge.py):

        - ``"local"`` (default) -- the PTY is spawned here, as before;
        - any other id          -- a registered environment; the parent dials
          that child's own ``/v1/terminals/ws`` once per tab and every channel
          of that env rides that one connection.

        One browser socket per tab therefore holds no matter how many
        environments the user opens terminals in -- an extra environment costs
        the browser nothing, and the child credential never leaves this process.
        A child that cannot be reached fails *its* channels with an ``error``
        frame; it must never take the tab's other terminals down.

        Each channel drives a persistent PTY session: closing a channel (or
        losing the whole socket) detaches but never kills it, and output produced
        while detached is not replayed — there is no browser-facing scrollback on
        the server.  That is exactly why the frontend keeps the channels of
        temporarily hidden terminals attached: the browser-side xterm scrollback
        is the only history there is.
        """
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send_json_error(400, "Missing Sec-WebSocket-Key")
            return

        # The socket is hijacked below — do not let the HTTP/1.1 keep-alive loop
        # try to parse further requests on this connection afterwards.
        self.close_connection = True
        sock = self.connection
        try:
            sock.settimeout(None)
        except OSError:
            pass
        sock.sendall(wsutil.ws_handshake_response(key).encode("utf-8"))

        # PTY output arrives from each terminal's own reader thread while this
        # thread writes input echoes and control frames, so frame writes are
        # serialized (a half-written header/payload pair would corrupt the
        # stream).  The env bridge pumps child frames through the same lock.
        send_lock = threading.Lock()
        channels: dict[str, dict] = {}

        def send_raw(text: str) -> None:
            with send_lock:
                self._ws_send_frame(sock, text)

        def send_msg(payload: dict) -> None:
            send_raw(json.dumps(payload, ensure_ascii=False))

        def env_channel_closed(term_id: str, error: Optional[str]) -> None:
            """A child environment's link died: retire just its channels."""
            info = channels.get(term_id)
            if info is None or not info.get("env_id"):
                return  # already detached (or a local channel, which never fires)
            channels.pop(term_id, None)
            if error:
                send_msg({"op": "error", "c": term_id,
                          "error": f"environment unreachable: {error}"})
            else:
                send_msg({"op": "closed", "c": term_id})

        # Remote channels (``env_id`` != local) are dialed on worker threads, so
        # a firewalled child cannot stall this socket's read loop.
        bridge = EnvMuxBridge(dial=self._mux_env_transport,
                              send_text=send_raw,
                              on_channel_closed=env_channel_closed)

        def attach(term_id: str, cols: int, rows: int, workspace,
                   env_id: str = LOCAL_ENV_ID) -> None:
            if env_id != LOCAL_ENV_ID:
                # Nothing is spawned locally: the PTY lives on the child and
                # ``workspace`` is a path over there.  The first channel of an
                # env opens the parent -> child connection; later ones share it.
                channels[term_id] = {"env_id": env_id}
                bridge.open(env_id, term_id, cols=cols, rows=rows,
                            workspace=workspace if isinstance(workspace, str) else "")
                return
            if workspace:
                from runtime.common import set_request_context as _set_ctx
                _set_ctx(workspace=workspace)
            if sys.platform == "win32" and PtyProcess is None:
                send_msg({"op": "error", "c": term_id, "error": "winpty not available"})
                return

            def emit(data: str, _term_id: str = term_id) -> None:
                send_msg({"c": _term_id, "d": data})

            # Marks which socket owns the callback, so detaching this socket
            # never clears a channel another tab (or a reconnect) took over.
            emit._mux_sock = sock
            session_id = term_id.split(":")[0] or term_id
            info = get_or_create_terminal(session_id, cols=cols, rows=rows, send_output=emit)
            if not info:
                send_msg({"op": "error", "c": term_id, "error": "Failed to create terminal"})
                return
            with _terminal_sessions_lock:
                info["sock"] = sock
                info["disconnected_at"] = None
                info["send_output"] = emit
            channels[term_id] = info
            self._terminal_set_winsize(info, cols, rows)
            send_msg({"op": "opened", "c": term_id})

        def detach(term_id: str, *, notify: bool) -> None:
            info = channels.pop(term_id, None)
            if info is None:
                return
            env_id = info.get("env_id")
            if env_id:
                # Remote channel: release it on the child, and let the
                # parent -> child connection go with the last channel this tab
                # had in that environment (the child keeps each PTY detached).
                bridge.close(term_id)
            else:
                with _terminal_sessions_lock:
                    if info.get("sock") is sock:
                        info["sock"] = None
                        info["disconnected_at"] = time.monotonic()
                    if getattr(info.get("send_output"), "_mux_sock", None) is sock:
                        info["send_output"] = None
            if notify:
                send_msg({"op": "closed", "c": term_id})

        def write_input(term_id: str, data: str) -> None:
            info = channels.get(term_id)
            if info is None:
                return
            env_id = info.get("env_id")
            if env_id:
                if not bridge.forward(term_id, {"op": "input", "c": term_id, "d": data}):
                    # The child link died; the bridge reports those channels,
                    # so stop routing to it here rather than repeating the drop.
                    channels.pop(term_id, None)
                return
            try:
                if sys.platform == "win32":
                    proc = info.get("proc")
                    if proc:
                        proc.write(data)
                else:
                    master_fd = info.get("master_fd")
                    if master_fd is not None:
                        os.write(master_fd, data.encode("utf-8"))
            except OSError:
                pass

        logger.info("Multiplexed terminal channel established")
        try:
            while True:
                raw = self._ws_recv_frame(sock)
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    send_msg({"op": "error", "error": "malformed frame"})
                    continue
                if not isinstance(msg, dict):
                    continue
                term_id = msg.get("c")
                if not isinstance(term_id, str) or not term_id or len(term_id) > 256:
                    send_msg({"op": "error", "error": "missing channel id"})
                    continue
                op = msg.get("op")
                if op == "open":
                    # ``env_id`` is the whole routing story: "local" (default,
                    # and what every pre-existing client sends) runs the PTY
                    # here, anything else is a registered child environment.
                    env_id = msg.get("env_id") or LOCAL_ENV_ID
                    if not isinstance(env_id, str) or len(env_id) > 256:
                        send_msg({"op": "error", "c": term_id, "error": "invalid env_id"})
                        continue
                    if term_id not in channels and len(channels) >= _MAX_MUX_CHANNELS:
                        send_msg({"op": "error", "c": term_id,
                                  "error": f"too many terminals on one channel (max {_MAX_MUX_CHANNELS})"})
                        continue
                    attach(term_id, _mux_dim(msg.get("cols"), 80), _mux_dim(msg.get("rows"), 24),
                           msg.get("workspace"), env_id)
                elif op == "input":
                    data = msg.get("d")
                    if isinstance(data, str):
                        write_input(term_id, data)
                elif op == "resize":
                    info = channels.get(term_id)
                    if info is None:
                        continue
                    cols, rows = _mux_dim(msg.get("cols"), 0), _mux_dim(msg.get("rows"), 0)
                    env_id = info.get("env_id")
                    if env_id:
                        bridge.forward(term_id, {"op": "resize", "c": term_id,
                                                 "cols": cols, "rows": rows})
                    else:
                        self._terminal_set_winsize(info, cols, rows)
                elif op == "close":
                    detach(term_id, notify=True)
                else:
                    send_msg({"op": "error", "c": term_id, "error": f"unknown op: {op}"})
        finally:
            # Losing the socket detaches every channel of this tab but keeps the
            # PTYs alive for reconnection (and for the agent's terminal tools).
            for term_id in list(channels):
                detach(term_id, notify=False)
            # ...and the same for the children: drop their connections, their
            # PTYs stay detached on the child until the next tab asks for them.
            bridge.shutdown()
            sock.close()
            logger.info("Multiplexed terminal channel closed")

    @staticmethod
    def _terminal_set_winsize(info: dict, cols: int, rows: int) -> None:
        """Apply the client's geometry to an attached terminal (PTY or winpty)."""
        if not cols or not rows:
            return
        try:
            if sys.platform == "win32":
                proc = info.get("proc")
                if proc:
                    proc.setwinsize(rows, cols)
                return
            master_fd = info.get("master_fd")
            if master_fd is not None:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass

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
