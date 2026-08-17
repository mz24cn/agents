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
import io
import json
import logging
import mimetypes
import os
import re
import select
import signal
import sys
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
    merge_stream_messages,
    persist_conversation,
    register_terminal_session,
    unregister_terminal_session,
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


class _ThreadingHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server whose request threads do not block shutdown."""

    daemon_threads = True


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
        host: str = "0.0.0.0",
        port: int = 7988,
        static_dir: Optional[str] = None,
        chats_dir: Optional[str] = None,
    ) -> None:
        """Initialize the HTTP server.

        Args:
            runtime: The Runtime instance to serve. If None, a default
                Runtime with empty registries will be created and
                persisted data will be loaded from disk on start.
            host: Bind address (default "0.0.0.0").
            port: Bind port (default 7988).
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
        self._host = host
        self._port = port
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

    def start(self) -> None:
        """Start the HTTP server (blocking).

        This method blocks until the server is shut down via stop().
        """
        self._active_streams: dict = {}
        self._server = _ThreadingHTTPServer((self._host, self._port), _RuntimeRequestHandler)
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
        self._server.inference_update_lock = threading.Lock()  # type: ignore[attr-defined]
        self._server.update_in_progress = False  # type: ignore[attr-defined]
        self._server.workspace_uploads = {}  # type: ignore[attr-defined]
        self._server.workspace_uploads_lock = threading.Lock()  # type: ignore[attr-defined]
        self._server.models_path = _MODELS_PATH  # type: ignore[attr-defined]
        self._server.tools_path = _TOOLS_PATH  # type: ignore[attr-defined]
        self._server.mcp_servers_path = _MCP_SERVERS_PATH  # type: ignore[attr-defined]
        self._server.prompt_templates_path = _PROMPT_TEMPLATES_PATH  # type: ignore[attr-defined]
        self._server.data_dir = _DATA_DIR  # type: ignore[attr-defined]
        self._server.serve_forever()

    def start_background(self) -> None:
        """Start the HTTP server in a background daemon thread.

        Returns immediately. Use stop() to shut down.
        """
        self._active_streams: dict = {}
        self._server = _ThreadingHTTPServer((self._host, self._port), _RuntimeRequestHandler)
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
        self._server.inference_update_lock = threading.Lock()  # type: ignore[attr-defined]
        self._server.update_in_progress = False  # type: ignore[attr-defined]
        self._server.workspace_uploads = {}  # type: ignore[attr-defined]
        self._server.workspace_uploads_lock = threading.Lock()  # type: ignore[attr-defined]
        self._server.models_path = _MODELS_PATH  # type: ignore[attr-defined]
        self._server.tools_path = _TOOLS_PATH  # type: ignore[attr-defined]
        self._server.mcp_servers_path = _MCP_SERVERS_PATH  # type: ignore[attr-defined]
        self._server.prompt_templates_path = _PROMPT_TEMPLATES_PATH  # type: ignore[attr-defined]
        self._server.data_dir = _DATA_DIR  # type: ignore[attr-defined]
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
