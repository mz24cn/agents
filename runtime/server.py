"""HTTP API Server for the Agent Service.

Provides RuntimeHTTPServer, a lightweight HTTP server built on Python's
standard library http.server module. Exposes REST endpoints for inference,
tool calling, and registry management.

Zero third-party dependencies — only Python standard library.
"""

import datetime
import gzip
import hashlib
import base64
import struct
import importlib.util
import ipaddress
import io
import json
import logging
import mimetypes
import os
import re
import select
import signal
import shutil
import ssl
import sys
import tempfile
import threading
import time
import uuid
import urllib.parse
from dataclasses import asdict
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from http import cookies
from typing import Callable, Optional

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
    import signal

logger = logging.getLogger("runtime.server")


# ---------------------------------------------------------------------------
# Conversation formatting helpers
# ---------------------------------------------------------------------------


from runtime.auth_manager import AuthManager, COOKIE_NAME
from runtime.common import get_system_encoding, SYSTEM_ENCODING
from runtime.env_manager import EnvManager
from runtime.session_manager import SessionManager
from runtime.mcp_client import MCPClientManager
from runtime.skill_manager import SkillManager
from runtime.models import (
    InferenceRequest,
    InferenceResult,
    Message,
    ModelConfig,
    ToolConfig,
)
from runtime.prompt_template_manager import PromptTemplateManager
from runtime.agent_manager import AgentManager
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.context_manager import ContextManager, ConversationTurn, JournalConflictError


from runtime.common import DATA_DIR as _DATA_DIR, set_request_context, get_request_context, clear_request_context, now_iso as _now_iso, session_timestamp


_MODELS_PATH = os.path.join(_DATA_DIR, "models.json")
_TOOLS_PATH = os.path.join(_DATA_DIR, "tools.json")
_MCP_SERVERS_PATH = os.path.join(_DATA_DIR, "mcp_servers.json")
_PROMPT_TEMPLATES_PATH = os.path.join(_DATA_DIR, "prompt_templates.json")
_ENV_PATH = os.path.join(_DATA_DIR, "env.json")
_AUTH_PATH = os.path.join(_DATA_DIR, "auth_token.json")
_AGENTS_DIR = os.path.join(_DATA_DIR, "agents")
# https 多证书目录：证书 {domain}.pem + 密钥 {domain}.key（见 _build_ssl_context）
_CERTS_DIR = os.path.join(_DATA_DIR, "certs")

# ---------------------------------------------------------------------------
# Module-level state and shared helpers (moved to runtime/server_state.py)
# ---------------------------------------------------------------------------
# Re-exported so `from runtime.server import ...` keeps working for
# runtime/builtin_tools.py and the test-suite (which patches the module-level
# path constants defined above).
from runtime.server_state import (
    _broadcast_session_event,
    _broadcast_session_status,
    _cleanup_thread,
    _load_function_from_file,
    _session_event_subscribers,
    _session_state_lock,
    _session_statuses,
    _terminal_sessions,
    _terminal_sessions_lock,
    _unread_sessions,
    cleanup_expired_terminal_sessions,
    get_or_create_terminal,
    get_terminal_for_session,
    get_terminal_session,
    IncrementalConversationPersister,
    merge_stream_messages,
    persist_conversation,
    register_terminal_session,
    unregister_terminal_session,
    stream_batch_is_protocol_complete,
)


# ---------------------------------------------------------------------------
# Handler mixins (moved to runtime/handler_*.py)
# ---------------------------------------------------------------------------
from runtime.handler_api import HandlerApiMixin
from runtime.handler_base import HandlerBaseMixin
from runtime.handler_infer import HandlerInferMixin
from runtime.handler_workspace import HandlerWorkspaceMixin


class _RuntimeRequestHandler(
    HandlerBaseMixin,
    HandlerInferMixin,
    HandlerWorkspaceMixin,
    HandlerApiMixin,
    BaseHTTPRequestHandler,
):
    """HTTP request handler that routes requests to the Runtime instance.

    The Runtime instance is accessed via self.server.runtime, which is set
    by RuntimeHTTPServer. Request-handling methods are provided by the
    domain-specific handler mixins:

    - HandlerBaseMixin: shared helpers, routing, static files, WebSocket/PTY, auth
    - HandlerInferMixin: inference endpoints
    - HandlerWorkspaceMixin: workspace + upload endpoints
    - HandlerApiMixin: registries, env, sessions, agents
    """

    # HTTP/1.1 keep-alive: reuse TCP connections across requests instead of
    # opening a new connection (and spawning a new thread) per request.
    protocol_version = "HTTP/1.1"
    # Idle keep-alive connections are dropped after 30s so lingering client
    # sockets do not hold request threads forever.
    timeout = 30


# ---------------------------------------------------------------------------
# Bind config resolution and HTTPS / SNI certificate support
# ---------------------------------------------------------------------------

_DEFAULT_PROTOCOL = "http"
_DEFAULT_DOMAIN = "0.0.0.0"
_DEFAULT_PORT = 7988
# SNI 未命中证书后的磁盘重查间隔（秒）：既避免热路径重复读盘，
# 又允许运行中把新证书放入 certs 目录后无需重启即可生效。
_SNI_MISS_RECHECK_SECONDS = 5.0


def _resolve_bind_config(
    host: Optional[str] = None,
    port: Optional[int] = None,
    protocol: Optional[str] = None,
) -> dict:
    """Resolve the address and protocol the server should bind to.

    Priority (highest first):
      1. Explicit arguments (``host`` / ``port`` / ``protocol``) — final
         overrides.
      2. The ``AGENTS_URL`` environment variable, e.g.
         ``https://domain:7988/`` — parsed into protocol, domain and
         port.  This variable may be synced into the process environment
         from env.json (web UI env settings) by EnvManager, so changes made
         there take effect on the next start (restart).
     3. Built-in defaults: ``http`` + ``0.0.0.0`` + ``7988`` (listen on all
         interfaces so the service is reachable from other machines right
         after installation; use ``AGENTS_URL`` or start() arguments to
         restrict it to loopback when a tighter bind is wanted).

    The bind address is derived from the domain: ``localhost`` binds to
    ``127.0.0.1``, a valid IP literal binds to that IP, and any other value
    (a domain name) binds to ``0.0.0.0``.

    Returns a dict with keys ``protocol``, ``domain``, ``port``, ``bind_host``.
    """
    proto = _DEFAULT_PROTOCOL
    domain = _DEFAULT_DOMAIN
    p = _DEFAULT_PORT

    url = os.environ.get("AGENTS_URL", "").strip()
    if url:
        parsed = urllib.parse.urlsplit(url)
        scheme = (parsed.scheme or "").lower()
        if scheme in ("http", "https"):
            proto = scheme
            if parsed.hostname:
                domain = parsed.hostname
            if parsed.port is not None:
                p = int(parsed.port)
            else:
                p = 443 if proto == "https" else 80
        else:
            logger.warning(
                "Ignoring AGENTS_URL=%r: unrecognized scheme %r (expected http or https)",
                url, parsed.scheme,
            )

    if protocol is not None:
        proto_override = str(protocol).strip().lower()
        if proto_override in ("http", "https"):
            proto = proto_override
        else:
            logger.warning("Ignoring protocol override %r: expected 'http' or 'https'", protocol)
    if host is not None:
        host_override = str(host).strip().rstrip(".")
        if host_override:
            domain = host_override
    if port is not None:
        p = int(port)

    if domain == "localhost":
        bind_host = "127.0.0.1"
    else:
        try:
            bind_host = str(ipaddress.ip_address(domain))
        except ValueError:
            bind_host = "0.0.0.0"

    return {"protocol": proto, "domain": domain, "port": p, "bind_host": bind_host}


def _generate_self_signed_cert_bytes(common_name: str) -> tuple:
    """Generate an in-memory self-signed certificate.

    Returns ``(cert_pem, key_pem)``. Used only as a last-resort fallback so
    that https keeps working (the browser will warn about the certificate,
    but the site stays reachable) when no certificate files are available.
    Requires the optional ``cryptography`` package; raises ``ImportError``
    when it is not installed.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(common_name)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def _load_cert_chain_from_pem(ctx: ssl.SSLContext, cert_pem: bytes, key_pem: bytes) -> None:
    """Load an in-memory PEM cert+key pair into *ctx*.

    The material is written to short-lived temp files because not all
    Python builds support the ``certdata``/``keydata`` keyword arguments
    of ``SSLContext.load_cert_chain``.
    """
    tmp_dir = tempfile.mkdtemp(prefix="agents_tls_")
    try:
        cert_path = os.path.join(tmp_dir, "cert.pem")
        key_path = os.path.join(tmp_dir, "key.key")
        with open(cert_path, "wb") as fh:
            fh.write(cert_pem)
        with open(key_path, "wb") as fh:
            fh.write(key_pem)
        ctx.load_cert_chain(cert_path, key_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


class _SniCertLoader:
    """Load per-domain TLS certificates from the ``DATA_DIR/certs`` directory.

    Certificate files are named ``{domain}.pem`` with the matching key
    ``{domain}.key`` (domain in lowercase, matching the SNI name). Loaded
    contexts are cached, and misses are remembered for a short window so a
    hot SNI miss does not hit the disk on every connection; new certificate
    pairs dropped into the directory are picked up without a restart once
    the window expires.
    """

    def __init__(self, cert_dir: str) -> None:
        self._cert_dir = cert_dir
        self._cache: dict = {}
        self._miss: dict = {}
        self._lock = threading.Lock()

    def _cert_paths(self, name: str) -> Optional[tuple]:
        pem = os.path.join(self._cert_dir, f"{name}.pem")
        key = os.path.join(self._cert_dir, f"{name}.key")
        if os.path.isfile(pem) and os.path.isfile(key):
            return pem, key
        return None

    def context_for(self, name: Optional[str]) -> Optional[ssl.SSLContext]:
        """Return the SSLContext for *name*, or None when no cert is found."""
        name = (name or "").strip().lower().rstrip(".")
        if not name:
            return None
        with self._lock:
            now = time.monotonic()
            # Opportunistically prune stale miss entries so a flood of
            # unknown SNI names cannot grow the dict unboundedly.
            if len(self._miss) > 128:
                for key in [k for k, ts in self._miss.items() if now - ts >= _SNI_MISS_RECHECK_SECONDS]:
                    self._miss.pop(key, None)
            ctx = self._cache.get(name)
            if ctx is not None:
                return ctx
            last_miss = self._miss.get(name)
            if last_miss is not None and now - last_miss < _SNI_MISS_RECHECK_SECONDS:
                return None
            ctx = None
            try:
                paths = self._cert_paths(name)
                if paths is not None:
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                    ctx.load_cert_chain(paths[0], paths[1])
                    logger.info("Loaded TLS certificate for %r from %s", name, self._cert_dir)
            except Exception:
                logger.exception(
                    "Failed to load TLS certificate for %r from %s; using fallback certificate",
                    name, self._cert_dir,
                )
                ctx = None
            if ctx is not None:
                self._cache[name] = ctx
                self._miss.pop(name, None)
            else:
                self._miss[name] = time.monotonic()
            return ctx


def _load_first_available_cert(cert_dir: str) -> Optional[ssl.SSLContext]:
    """Load the first ``{name}.pem``/``{name}.key`` pair found in *cert_dir*."""
    try:
        names = sorted(os.listdir(cert_dir))
    except OSError:
        return None
    for name in names:
        if not name.endswith(".pem"):
            continue
        stem = name[:-4]
        key_path = os.path.join(cert_dir, f"{stem}.key")
        if not os.path.isfile(key_path):
            continue
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(os.path.join(cert_dir, name), key_path)
            return ctx
        except Exception:
            logger.exception("Failed to load fallback certificate %s", name)
    return None


def _build_ssl_context(domain: str) -> Optional[ssl.SSLContext]:
    """Build the SSLContext for an https-enabled server.

    The base context carries a fallback certificate; an SNI callback
    switches each incoming connection to the matching per-domain context
    (``{domain}.pem`` / ``{domain}.key`` files in ``DATA_DIR/certs``) before
    the rest of the HTTPS request processing runs. When no certificate
    matches the requested SNI name the fallback certificate is used, so
    https service is still provided — the browser shows a certificate
    warning, but the site remains reachable.

    Fallback certificate preference:
      1. ``default.pem`` / ``default.key`` in the cert directory;
      2. an in-memory self-signed certificate (requires the optional
         ``cryptography`` package);
      3. the first certificate pair found in the directory.

    Returns None when no certificate can be provided at all.
    """
    loader = _SniCertLoader(_CERTS_DIR)

    base = loader.context_for("default")
    source = "default"
    if base is None:
        base = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        base.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            cert_pem, key_pem = _generate_self_signed_cert_bytes(domain or "localhost")
            _load_cert_chain_from_pem(base, cert_pem, key_pem)
            source = "generated self-signed certificate"
        except ImportError:
            logger.warning(
                "The optional 'cryptography' package is not installed; "
                "cannot generate a fallback self-signed certificate"
            )
            base = None
        except Exception:
            logger.exception("Failed to generate fallback self-signed certificate")
            base = None
    if base is None:
        base = _load_first_available_cert(_CERTS_DIR)
        if base is not None:
            source = "first certificate in the directory"
        else:
            return None

    def _sni_callback(ssl_obj, server_name, context=None):
        """SNI callback: attach the matching per-domain certificate.

        Python <= 3.10 invokes the callback with ``(ssl_obj, server_name)``;
        3.11+ adds the SSLContext as a third argument. Switching is done by
        assigning the connection's ``context`` (see the CPython ssl docs for
        ``SSLContext.sni_callback``). Returning None lets the handshake
        continue with whatever context is attached to the connection.
        """
        try:
            ctx = loader.context_for(server_name)
            if ctx is not None:
                ssl_obj.context = ctx
        except Exception:
            logger.exception(
                "SNI certificate lookup failed for %r; using fallback certificate",
                server_name,
            )
        return None

    base.sni_callback = _sni_callback
    logger.info(
        "HTTPS enabled; SNI certificates loaded from %s (fallback: %s)",
        _CERTS_DIR, source,
    )
    return base


class _ThreadingHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server whose request threads do not block shutdown."""

    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, bind_and_activate=True,
                 ssl_context: Optional[ssl.SSLContext] = None) -> None:
        super().__init__(server_address, RequestHandlerClass, bind_and_activate)
        if ssl_context is not None:
            # Wrap the listening socket so every accepted connection is TLS.
            # The per-connection handshake is performed lazily on the first
            # read, and the sni_callback attached to ssl_context selects the
            # per-domain certificate (see _build_ssl_context).
            self.socket = ssl_context.wrap_socket(self.socket, server_side=True)


class RuntimeHTTPServer:
    """Lightweight HTTP API server wrapping a Runtime instance.

    Built on Python's standard library http.server module. Provides REST
    endpoints for inference, tool calling, and registry management.

    API Routes:
        POST /v1/infer          — Model inference
        POST /v1/infer/stream   — Streaming model inference (SSE)
        POST /v1/tools/call     — Direct tool call
        GET  /v1/models         — List all models
        GET  /v1/tools          — List all tools
        POST /v1/models         — Register a model
        POST /v1/tools          — Register a tool
    """

    def __init__(
        self,
        runtime: Optional[Runtime] = None,
        static_dir: Optional[str] = None,
        chats_dir: Optional[str] = None,
    ) -> None:
        """Initialize the HTTP server.

        The bind host/port/protocol are deliberately not constructor
        arguments: they are resolved in start() / start_background() so
        that they may come from the AGENTS_URL environment variable (which
        can be synced into the process environment from the web UI's env
        settings via EnvManager, and therefore only takes effect after a
        restart).

        Args:
            runtime: The Runtime instance to serve. If None, a default
                Runtime with empty registries will be created and
                persisted data will be loaded from disk on start.
            static_dir: Optional path to a directory of static files to serve.
                Requests that don't match /v1/* are served from this directory,
                with / and unknown paths falling back to index.html (SPA mode).
                Defaults to the web/dist directory next to this package if it
                exists, otherwise None (static serving disabled).
            chats_dir: Base directory for session storage. Defaults to
                ``~/.agents_runtime/chat_data`` (alongside other config files).
        """
        # Initialize EnvManager
        self._env_manager = EnvManager(env_path=_ENV_PATH)
        self._env_manager._sync_to_environ(self._env_manager.read())

        # Configure logging if not already configured
        if not logging.root.handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            )
        # Defaults before start() resolves the effective bind config from
        # explicit arguments / AGENTS_URL (see _resolve_bind_config).
        self._host = _DEFAULT_DOMAIN
        self._port = _DEFAULT_PORT
        self._protocol = _DEFAULT_PROTOCOL
        self._bind_host = "127.0.0.1"
        self._prompt_template_manager = PromptTemplateManager()
        self._agent_manager = AgentManager()
        self._auth_manager = AuthManager(_AUTH_PATH)
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

        # Resolve static_dir: use provided value, or auto-detect web/dist
        if static_dir is not None:
            self._static_dir: Optional[str] = static_dir
        else:
            _candidate = os.path.join(os.path.dirname(__file__), "..", "web", "dist")
            _candidate = os.path.realpath(_candidate)
            self._static_dir = _candidate if os.path.isdir(_candidate) else None

        if runtime is not None:
            self._runtime = runtime
        else:
            model_registry = ModelRegistry()
            tool_registry = ToolRegistry()
            mcp_manager = MCPClientManager()
            skill_manager = SkillManager(tool_registry)
            if os.path.isfile(_MODELS_PATH):
                model_registry.load(_MODELS_PATH)
            if os.path.isfile(_TOOLS_PATH):
                tool_registry.load(_TOOLS_PATH)
                # Restore SkillManager state for persisted skill tools
                for tc in tool_registry.list_by_type("skill"):
                    if tc.skill_dir:
                        try:
                            skill_manager.load_skill(tc.skill_dir)
                        except ValueError:
                            pass
            if os.path.isfile(_MCP_SERVERS_PATH):
                with open(_MCP_SERVERS_PATH, "r", encoding="utf-8") as f:
                    mcp_cfg = json.load(f)
                mcp_manager.load_config(mcp_cfg)
            if os.path.isfile(_PROMPT_TEMPLATES_PATH):
                self._prompt_template_manager.load(_PROMPT_TEMPLATES_PATH)
            self._agent_manager.load()
            from runtime.builtin_tools import register_builtin_tools
            self._runtime = Runtime(
                model_registry=model_registry,
                tool_registry=tool_registry,
                mcp_manager=mcp_manager,
                skill_manager=skill_manager,
                prompt_template_manager=self._prompt_template_manager,
            )
            register_builtin_tools(tool_registry, runtime=self._runtime)

        # Initialize ContextManager between Server and Runtime
        self._context_manager = ContextManager(
            infer_fn=self._runtime.infer,
            chats_dir=chats_dir if chats_dir is not None else os.path.join(_DATA_DIR, "chat_data"),
            prompt_template_manager=self._prompt_template_manager,
            model_registry=self._runtime._model_registry,
        )
      # Initialize SessionManager
        self._session_manager = SessionManager(
            chats_dir=chats_dir if chats_dir is not None else os.path.join(_DATA_DIR, "chat_data"),
            infer_fn=self._runtime.infer,
            broadcast_fn=_broadcast_session_event,
            model_registry=self._runtime._model_registry,
        )

    def _prepare_server(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        protocol: Optional[str] = None,
    ) -> None:
        """Resolve the bind config and create the underlying HTTPServer.

        The server object is created (socket bound) but not yet serving;
        start() and start_background() only differ in how they run
        serve_forever().
        """
        cfg = _resolve_bind_config(host=host, port=port, protocol=protocol)
        self._host = cfg["domain"]
        self._port = cfg["port"]
        self._protocol = cfg["protocol"]
        self._bind_host = cfg["bind_host"]

        ssl_context: Optional[ssl.SSLContext] = None
        if cfg["protocol"] == "https":
            ssl_context = _build_ssl_context(cfg["domain"])
            if ssl_context is None:
                logger.error(
                    "https is configured but no certificate could be loaded from %s "
                    "(no {domain}.pem/{domain}.key pair, no 'default' pair, and no "
                    "'cryptography' package available to self-sign one). Falling back "
                    "to http so the service stays reachable.",
                    _CERTS_DIR,
                )
                self._protocol = "http"

        self._active_streams: dict = {}
        self._server = _ThreadingHTTPServer(
            (self._bind_host, self._port),
            _RuntimeRequestHandler,
            ssl_context=ssl_context,
        )
        self._server.runtime = self._runtime  # type: ignore[attr-defined]
        self._server.prompt_template_manager = self._prompt_template_manager  # type: ignore[attr-defined]
        self._server.agent_manager = self._agent_manager  # type: ignore[attr-defined]
        self._server.static_dir = self._static_dir  # type: ignore[attr-defined]
        self._server.context_manager = self._context_manager  # type: ignore[attr-defined]
        self._server.env_manager = self._env_manager  # type: ignore[attr-defined]
        self._server.auth_manager = self._auth_manager  # type: ignore[attr-defined]
        self._server.session_manager = self._session_manager  # type: ignore[attr-defined]
        self._server.active_streams = self._active_streams  # type: ignore[attr-defined]
        self._server.active_inference_count = 0  # type: ignore[attr-defined]
        self._server.active_api_inference_count = 0  # type: ignore[attr-defined]
        self._server.inference_update_lock = threading.Lock()  # type: ignore[attr-defined]
        self._server.update_in_progress = False  # type: ignore[attr-defined]
        self._server.workspace_uploads = {}  # type: ignore[attr-defined]
        self._server.workspace_uploads_lock = threading.Lock()  # type: ignore[attr-defined]
        self._server.models_path = _MODELS_PATH  # type: ignore[attr-defined]
        self._server.tools_path = _TOOLS_PATH  # type: ignore[attr-defined]
        self._server.mcp_servers_path = _MCP_SERVERS_PATH  # type: ignore[attr-defined]
        self._server.prompt_templates_path = _PROMPT_TEMPLATES_PATH  # type: ignore[attr-defined]
        self._server.data_dir = _DATA_DIR  # type: ignore[attr-defined]
        self._server.ssl_context = ssl_context  # type: ignore[attr-defined]
        logger.info(
            "HTTP server bound to %s:%s (protocol=%s, domain=%s)",
            self._bind_host, self._server.server_address[1], self._protocol, self._host,
        )

    def start(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        protocol: Optional[str] = None,
    ) -> None:
        """Start the HTTP server (blocking).

        This method blocks until the server is shut down via stop().

        The bind address and protocol are resolved in this order:
        explicit arguments (final overrides) > AGENTS_URL environment
        variable (e.g. ``https://domain:7988/``; may be synced into
        the process environment from the web UI's env settings by
        EnvManager, so changes there take effect after a restart) >
       built-in defaults (http + 0.0.0.0 + 7988).

        Args:
            host: Override for the domain/hostname. The bind address is
                derived from it: ``localhost`` binds to 127.0.0.1, a valid
                IP literal binds to that IP, and any other (domain name)
                value binds to 0.0.0.0.
            port: Override for the bind port.
            protocol: Override for the protocol, ``http`` or ``https``.
                https enables TLS with SNI-based multi-certificate loading
                from the ``DATA_DIR/certs`` directory (``{domain}.pem`` /
                ``{domain}.key``).
        """
        self._prepare_server(host=host, port=port, protocol=protocol)
        self._server.serve_forever()

    def start_background(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        protocol: Optional[str] = None,
    ) -> None:
        """Start the HTTP server in a background daemon thread.

        Returns immediately. Use stop() to shut down. Accepts the same
        host/port/protocol overrides as start().
        """
        self._prepare_server(host=host, port=port, protocol=protocol)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Shut down the HTTP server."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def port(self) -> int:
        """Return the port the server is bound to."""
        if self._server is not None:
            return self._server.server_address[1]
        return self._port

    @property
    def protocol(self) -> str:
        """Return the protocol the server is serving ("http" or "https")."""
        return self._protocol
