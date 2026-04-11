"""HTTP API Server for the Composable Agent Runtime.

Provides RuntimeHTTPServer, a lightweight HTTP server built on Python's
standard library http.server module. Exposes REST endpoints for inference,
tool calling, and registry management.

Zero third-party dependencies — only Python standard library.
"""

import importlib.util
import json
import logging
import mimetypes
import os
import re
import sys
import threading
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Callable, Optional

logger = logging.getLogger("runtime.server")

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
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime

_DATA_DIR = os.path.join(os.path.expanduser("~"), ".agents_runtime")


def _load_function_from_file(file_path: str, func_name: str) -> Callable:
    """从指定 .py 文件动态加载函数，每次调用都重新从磁盘读取。"""
    module_name = f"_dynamic_tool_{hash(file_path)}"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"无法加载模块文件: {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        sys.modules.pop(module_name, None)
        raise RuntimeError(f"执行模块 {file_path} 时出错: {e}") from e
    if not hasattr(module, func_name):
        raise AttributeError(f"模块 '{file_path}' 中不存在函数 '{func_name}'")
    func = getattr(module, func_name)
    if not callable(func):
        raise TypeError(f"'{func_name}' 在模块 '{file_path}' 中不是可调用对象")
    return func
_MODELS_PATH = os.path.join(_DATA_DIR, "models.json")
_TOOLS_PATH = os.path.join(_DATA_DIR, "tools.json")
_MCP_SERVERS_PATH = os.path.join(_DATA_DIR, "mcp_servers.json")
_PROMPT_TEMPLATES_PATH = os.path.join(_DATA_DIR, "prompt_templates.json")
_ENV_PATH = os.path.join(_DATA_DIR, "env.json")


def _load_env_overrides() -> None:
    """Load environment variable overrides from _DATA_DIR/env.json if it exists.

    The file should be a flat JSON object mapping string keys to string values.
    Loaded values overwrite the corresponding entries in os.environ.
    """
    if not os.path.isfile(_ENV_PATH):
        return
    try:
        with open(_ENV_PATH, "r", encoding="utf-8") as f:
            env_map = json.load(f)
        if isinstance(env_map, dict):
            for k, v in env_map.items():
                os.environ[str(k)] = str(v)
    except Exception:
        pass  # silently ignore malformed / unreadable env.json


class _RuntimeRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler that routes requests to the Runtime instance.

    The Runtime instance is accessed via self.server.runtime, which is set
    by RuntimeHTTPServer.
    """

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default stderr logging."""
        pass

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
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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

    def _get_runtime(self) -> Runtime:
        """Get the Runtime instance from the server."""
        return self.server.runtime  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        """Handle GET requests."""
        path = self.path.rstrip("/")
        if path == "/v1/models":
            self._handle_list_models()
        elif path == "/v1/tools":
            self._handle_list_tools()
        elif path == "/v1/mcp-servers":
            self._handle_list_mcp_servers()
        elif path == "/v1/prompt-templates":
            self._handle_list_prompt_templates()
        elif path.startswith("/v1/"):
            self._send_json_error(404, f"Not found: {self.path}")
        else:
            self._handle_static_file()

    def _handle_static_file(self) -> None:
        """Serve static files from the web/dist directory."""
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

        with open(file_path, "rb") as f:
            data = f.read()

        self.send_response(200)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        """Handle POST requests."""
        path = self.path.rstrip("/")
        if path == "/v1/infer":
            self._handle_infer()
        elif path == "/v1/infer/stream":
            self._handle_infer_stream()
        elif path == "/v1/tools/call":
            self._handle_tool_call()
        elif path == "/v1/tools/mcp":
            self._handle_register_mcp_servers()
        elif path == "/v1/tools/skill":
            self._handle_register_skill()
        elif path == "/v1/models":
            self._handle_register_model()
        elif path == "/v1/tools":
            self._handle_register_tool()
        elif path == "/v1/prompt-templates":
            self._handle_create_prompt_template()
        else:
            self._send_json_error(404, f"Not found: {self.path}")

    def do_PUT(self) -> None:
        """Handle PUT requests."""
        path = self.path.rstrip("/")
        m = re.match(r"^/v1/models/([^/]+)$", path)
        if m:
            self._handle_update_model(m.group(1))
            return
        m = re.match(r"^/v1/tools/([^/]+)$", path)
        if m:
            self._handle_update_tool(m.group(1))
            return
        m = re.match(r"^/v1/prompt-templates/([^/]+)$", path)
        if m:
            self._handle_update_prompt_template(m.group(1))
            return
        self._send_json_error(404, f"Not found: {self.path}")

    def do_DELETE(self) -> None:
        """Handle DELETE requests."""
        path = self.path.rstrip("/")
        m = re.match(r"^/v1/models/([^/]+)$", path)
        if m:
            self._handle_delete_model(m.group(1))
            return
        if path == "/v1/tools/batch":
            self._handle_batch_delete_tools()
            return
        m = re.match(r"^/v1/mcp-servers/([^/]+)$", path)
        if m:
            self._handle_delete_mcp_server(m.group(1))
            return
        m = re.match(r"^/v1/tools/([^/]+)$", path)
        if m:
            self._handle_delete_tool(m.group(1))
            return
        m = re.match(r"^/v1/prompt-templates/([^/]+)$", path)
        if m:
            self._handle_delete_prompt_template(m.group(1))
            return
        self._send_json_error(404, f"Not found: {self.path}")

    # ------------------------------------------------------------------
    # GET handlers
    # ------------------------------------------------------------------

    def _handle_list_models(self) -> None:
        """GET /v1/models — list all registered model configurations."""
        runtime = self._get_runtime()
        models = runtime._model_registry.list_all()
        data = [m.to_dict() for m in models]
        self._send_json_response(200, {"models": data})

    def _handle_list_tools(self) -> None:
        """GET /v1/tools — list all registered tool configurations."""
        runtime = self._get_runtime()
        tools = runtime._tool_registry.list_all()
        data = [t.to_dict() for t in tools]
        self._send_json_response(200, {"tools": data})

    def _handle_list_prompt_templates(self) -> None:
        """GET /v1/prompt-templates — list all prompt templates."""
        mgr = self.server.prompt_template_manager  # type: ignore[attr-defined]
        templates = mgr.list_all()
        data = [t.to_dict() for t in templates]
        self._send_json_response(200, {"templates": data})

    # ------------------------------------------------------------------
    # POST handlers
    # ------------------------------------------------------------------

    def _handle_infer(self) -> None:
        """POST /v1/infer — execute model inference.

        Expects JSON body with at least model_id. Optional fields:
        tool_ids, messages, text, max_tool_rounds.
        """
        body = self._read_json_body()
        if body is None:
            return

        if "model_id" not in body:
            self._send_json_error(400, "Missing required field: model_id")
            return

        # Build InferenceRequest from JSON
        messages = None
        if "messages" in body:
            messages = [Message.from_dict(m) for m in body["messages"]]

        request = InferenceRequest(
            model_id=body["model_id"],
            tool_ids=body.get("tool_ids", []),
            messages=messages,
            text=body.get("text"),
            stream=False,
            max_tool_rounds=body.get("max_tool_rounds", 10),
        )

        runtime = self._get_runtime()
        result = runtime.infer(request)

        # Serialize InferenceResult
        response_data = {
            "success": result.success,
            "messages": [m.to_dict() for m in result.messages],
        }
        if result.error is not None:
            response_data["error"] = result.error
        if result.error_code is not None:
            response_data["error_code"] = result.error_code
        if result.stat is not None:
            response_data["stat"] = result.stat.to_dict()

        status = 200 if result.success else 500
        self._send_json_response(status, response_data)

    def _handle_infer_stream(self) -> None:
        """POST /v1/infer/stream — execute streaming model inference.

        Returns Server-Sent Events (SSE) stream. Each event contains a
        JSON-encoded Message delta.
        """
        body = self._read_json_body()
        if body is None:
            return

        if "model_id" not in body:
            self._send_json_error(400, "Missing required field: model_id")
            return

        messages = None
        if "messages" in body:
            messages = [Message.from_dict(m) for m in body["messages"]]

        request = InferenceRequest(
            model_id=body["model_id"],
            tool_ids=body.get("tool_ids", []),
            messages=messages,
            text=body.get("text"),
            stream=True,
            max_tool_rounds=body.get("max_tool_rounds", 10),
        )

        # Send SSE headers
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        cancel_event = threading.Event()
        runtime = self._get_runtime()
        try:
            for msg in runtime.infer_stream(request, cancel_event=cancel_event):
                event_data = json.dumps(msg.to_dict(), ensure_ascii=False)
                self.wfile.write(f"data: {event_data}\n\n".encode("utf-8"))
                self.wfile.flush()
                # Log error messages from the stream for easier diagnosis
                if msg.role == "assistant" and msg.content and msg.content.startswith("Error:"):
                    logger.error("infer_stream error event | model=%s %s", request.model_id, msg.content)
            # Send done event
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected — signal infer_stream to stop
            cancel_event.set()
        except Exception as exc:
            try:
                error_data = json.dumps({"error": str(exc)}, ensure_ascii=False)
                self.wfile.write(f"data: {error_data}\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

    def _handle_tool_call(self) -> None:
        """POST /v1/tools/call — directly call a tool.

        Expects JSON body with tool_id and arguments.
        """
        body = self._read_json_body()
        if body is None:
            return

        tool_id = body.get("tool_id")
        if not tool_id:
            self._send_json_error(400, "Missing required field: tool_id")
            return

        arguments = body.get("arguments", {})
        if not isinstance(arguments, dict):
            self._send_json_error(400, "Field 'arguments' must be a JSON object")
            return

        runtime = self._get_runtime()
        result = runtime.call_tool(tool_id, arguments)

        # If result starts with "Error:", treat as error
        if result.startswith("Error:"):
            self._send_json_response(400, {"error": result})
        else:
            self._send_json_response(200, {"result": result})

    def _handle_register_model(self) -> None:
        """POST /v1/models — register a new model configuration.

        Expects a ModelConfig JSON body.
        """
        body = self._read_json_body()
        if body is None:
            return

        required = ["model_id", "api_base", "model_name"]
        for field in required:
            if field not in body:
                self._send_json_error(400, f"Missing required field: {field}")
                return

        try:
            config = ModelConfig.from_dict(body)
        except (KeyError, TypeError, ValueError) as exc:
            self._send_json_error(400, f"Invalid model config: {exc}")
            return

        runtime = self._get_runtime()
        runtime._model_registry.register(config)
        runtime._model_registry.save(_MODELS_PATH)
        self._send_json_response(201, {"status": "registered", "model_id": config.model_id})

    def _handle_register_mcp_servers(self) -> None:
        """POST /v1/tools/mcp — register MCP servers from a mcpServers config.

        Connects to each server, discovers its tools, and registers them in
        the ToolRegistry. The server config is also persisted so connections
        are restored on restart (tools are re-discovered lazily on first infer).

        Expected body::

            {
                "mcpServers": {
                    "time": {"command": "uvx", "args": ["mcp-server-time"]},
                    "fetch": {"url": "http://localhost:8081/mcp"}
                }
            }
        """
        body = self._read_json_body()
        if body is None:
            return

        if "mcpServers" not in body or not isinstance(body["mcpServers"], dict):
            self._send_json_error(400, 'Missing or invalid "mcpServers" object')
            return

        runtime = self._get_runtime()
        mcp_manager = runtime._mcp_manager
        if mcp_manager is None:
            self._send_json_error(500, "MCPClientManager not available")
            return

        registered_servers = []
        registered_tool_ids = []
        errors = []

        for server_name, server_cfg in body["mcpServers"].items():
            if not isinstance(server_cfg, dict):
                continue
            if server_cfg.get("disabled", False):
                continue
            try:
                if "command" in server_cfg:
                    mcp_manager.connect_stdio(
                        server_name=server_name,
                        command=server_cfg["command"],
                        args=server_cfg.get("args"),
                        env=server_cfg.get("env"),
                    )
                elif "url" in server_cfg:
                    mcp_manager.connect_url(
                        server_name=server_name,
                        url=server_cfg["url"],
                        headers=server_cfg.get("headers"),
                    )
                else:
                    errors.append(f"{server_name}: missing 'command' or 'url'")
                    continue

                # Discover tools — this is the moment the process starts
                discovered = mcp_manager.get_tools(server_name)
                for t in discovered:
                    runtime._tool_registry.register(t)
                    registered_tool_ids.append(t.tool_id)
                registered_servers.append(server_name)

            except Exception as exc:
                errors.append(f"{server_name}: {exc}")

        if registered_tool_ids:
            runtime._tool_registry.save(_TOOLS_PATH)

        # Persist server configs for restart recovery (lazy re-connect on next start)
        if registered_servers:
            saved: dict = {}
            if os.path.isfile(_MCP_SERVERS_PATH):
                with open(_MCP_SERVERS_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
            saved_servers = saved.setdefault("mcpServers", {})
            for server_name in registered_servers:
                saved_servers[server_name] = body["mcpServers"][server_name]
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(_MCP_SERVERS_PATH, "w", encoding="utf-8") as f:
                json.dump(saved, f, ensure_ascii=False, indent=2)

        resp: dict = {"registered_servers": registered_servers, "registered_tools": registered_tool_ids}
        if errors:
            resp["errors"] = errors
        if not registered_servers and errors:
            resp["error"] = "; ".join(errors)
            self._send_json_response(400, resp)
        else:
            self._send_json_response(200, resp)

    def _handle_register_skill(self) -> None:
        """POST /v1/tools/skill — register a skill from a directory containing SKILL.md.

        Expected body::

            {"skill_dir": "/path/to/skill_folder"}

        Reads SKILL.md from the directory, parses name/description from front-matter,
        and registers the skill in the ToolRegistry.
        """
        body = self._read_json_body()
        if body is None:
            return

        skill_dir = body.get("skill_dir", "").strip()
        if not skill_dir:
            self._send_json_error(400, "Missing required field: skill_dir")
            return

        runtime = self._get_runtime()

        # Use the runtime's skill_manager if available, otherwise create one
        skill_manager = runtime._skill_manager
        if skill_manager is None:
            skill_manager = SkillManager(runtime._tool_registry)
            runtime._skill_manager = skill_manager

        try:
            config = skill_manager.load_skill(skill_dir)
        except ValueError as exc:
            self._send_json_error(400, str(exc))
            return

        runtime._tool_registry.save(_TOOLS_PATH)
        self._send_json_response(201, {"status": "registered", "tool_id": config.tool_id})

    def _handle_register_tool(self) -> None:
        """POST /v1/tools — register a new tool configuration.

        Expects a ToolConfig JSON body. For MCP tools, also accepts optional
        mcp_command/mcp_args/mcp_env (stdio) or mcp_url/mcp_headers (HTTP)
        fields to register the server connection lazily.
        """
        body = self._read_json_body()
        if body is None:
            return

        required = ["tool_id", "tool_type", "name", "description", "parameters"]
        for field in required:
            if field not in body:
                self._send_json_error(400, f"Missing required field: {field}")
                return

        try:
            config = ToolConfig.from_dict(body)
        except (KeyError, TypeError, ValueError) as exc:
            self._send_json_error(400, f"Invalid tool config: {exc}")
            return

        runtime = self._get_runtime()

        # For MCP tools, register the server connection lazily if params provided
        if config.tool_type == "mcp" and config.mcp_server_name:
            mcp_manager = runtime._mcp_manager
            if mcp_manager is not None:
                if "mcp_command" in body:
                    mcp_manager.connect_stdio(
                        server_name=config.mcp_server_name,
                        command=body["mcp_command"],
                        args=body.get("mcp_args"),
                        env=body.get("mcp_env"),
                    )
                elif "mcp_url" in body:
                    mcp_manager.connect_url(
                        server_name=config.mcp_server_name,
                        url=body["mcp_url"],
                        headers=body.get("mcp_headers"),
                    )

        # For function tools, load callable from file if path and name provided
        callable_fn = None
        if config.tool_type == "function" and config.function_file_path and config.function_name:
            try:
                callable_fn = _load_function_from_file(
                    config.function_file_path, config.function_name
                )
            except (FileNotFoundError, AttributeError, TypeError, RuntimeError) as exc:
                self._send_json_error(400, f"加载函数失败: {exc}")
                return

        runtime._tool_registry.register(config, callable_fn=callable_fn)
        runtime._tool_registry.save(_TOOLS_PATH)
        self._send_json_response(201, {"status": "registered", "tool_id": config.tool_id})

    def _handle_create_prompt_template(self) -> None:
        """POST /v1/prompt-templates — create a new prompt template.

        Expects JSON body with name and content fields.
        """
        body = self._read_json_body()
        if body is None:
            return

        if "name" not in body:
            self._send_json_error(400, "Missing required field: name")
            return
        if "content" not in body:
            self._send_json_error(400, "Missing required field: content")
            return

        mgr = self.server.prompt_template_manager  # type: ignore[attr-defined]
        template = mgr.create(name=body["name"], content=body["content"])
        mgr.save(_PROMPT_TEMPLATES_PATH)
        self._send_json_response(201, {
            "status": "created",
            "template_id": template.template_id,
        })

    # ------------------------------------------------------------------
    # PUT handlers (stubs)
    # ------------------------------------------------------------------

    def _handle_update_model(self, model_id: str) -> None:
        """PUT /v1/models/{model_id} — update a model configuration."""
        body = self._read_json_body()
        if body is None:
            return

        runtime = self._get_runtime()
        existing = runtime._model_registry.get(model_id)
        if existing is None:
            self._send_json_error(404, f"Model not found: {model_id}")
            return

        try:
            config = ModelConfig.from_dict(body)
        except (KeyError, TypeError, ValueError) as exc:
            self._send_json_error(400, f"Invalid model config: {exc}")
            return

        runtime._model_registry.register(config)
        runtime._model_registry.save(_MODELS_PATH)
        self._send_json_response(200, {"status": "updated", "model_id": model_id})

    def _handle_update_tool(self, tool_id: str) -> None:
        """PUT /v1/tools/{tool_id} — update a tool configuration."""
        body = self._read_json_body()
        if body is None:
            return

        runtime = self._get_runtime()
        existing = runtime._tool_registry.get(tool_id)
        if existing is None:
            self._send_json_error(404, f"Tool not found: {tool_id}")
            return
        if existing.builtin:
            self._send_json_error(403, f"Cannot update built-in tool: {tool_id}")
            return

        try:
            config = ToolConfig.from_dict(body)
        except (KeyError, TypeError, ValueError) as exc:
            self._send_json_error(400, f"Invalid tool config: {exc}")
            return

        # Re-register MCP server connection if params provided
        if config.tool_type == "mcp" and config.mcp_server_name:
            mcp_manager = runtime._mcp_manager
            if mcp_manager is not None:
                if "mcp_command" in body:
                    mcp_manager.connect_stdio(
                        server_name=config.mcp_server_name,
                        command=body["mcp_command"],
                        args=body.get("mcp_args"),
                        env=body.get("mcp_env"),
                    )
                elif "mcp_url" in body:
                    mcp_manager.connect_url(
                        server_name=config.mcp_server_name,
                        url=body["mcp_url"],
                        headers=body.get("mcp_headers"),
                    )

        # For function tools, load callable from file if path and name provided
        callable_fn = None
        if config.tool_type == "function" and config.function_file_path and config.function_name:
            try:
                callable_fn = _load_function_from_file(
                    config.function_file_path, config.function_name
                )
            except (FileNotFoundError, AttributeError, TypeError, RuntimeError) as exc:
                self._send_json_error(400, f"加载函数失败: {exc}")
                return

        runtime._tool_registry.register(config, callable_fn=callable_fn)
        runtime._tool_registry.save(_TOOLS_PATH)
        self._send_json_response(200, {"status": "updated", "tool_id": tool_id})

    def _handle_update_prompt_template(self, template_id: str) -> None:
        """PUT /v1/prompt-templates/{template_id} — update a prompt template."""
        body = self._read_json_body()
        if body is None:
            return

        mgr = self.server.prompt_template_manager  # type: ignore[attr-defined]
        updated = mgr.update(
            template_id,
            name=body.get("name", ""),
            content=body.get("content", ""),
        )
        if updated is None:
            self._send_json_error(404, f"Prompt template not found: {template_id}")
            return

        mgr.save(_PROMPT_TEMPLATES_PATH)
        self._send_json_response(200, {"status": "updated", "template_id": template_id})

    # ------------------------------------------------------------------
    # DELETE handlers (stubs)
    # ------------------------------------------------------------------

    def _handle_delete_model(self, model_id: str) -> None:
        """DELETE /v1/models/{model_id} — delete a model configuration."""
        runtime = self._get_runtime()
        removed = runtime._model_registry.remove(model_id)
        if not removed:
            self._send_json_error(404, f"Model not found: {model_id}")
            return

        runtime._model_registry.save(_MODELS_PATH)
        self._send_json_response(200, {"status": "deleted", "model_id": model_id})

    def _handle_batch_delete_tools(self) -> None:
        """DELETE /v1/tools/batch — delete multiple tools by ID list.

        Expects JSON body: {"tool_ids": ["id1", "id2", ...]}
        """
        body = self._read_json_body()
        if body is None:
            return
        tool_ids = body.get("tool_ids")
        if not isinstance(tool_ids, list):
            self._send_json_error(400, "tool_ids must be a list")
            return
        runtime = self._get_runtime()
        deleted, not_found, skipped = [], [], []
        for tid in tool_ids:
            tc = runtime._tool_registry.get(tid)
            if tc is None:
                not_found.append(tid)
            elif tc.builtin:
                skipped.append(tid)
            elif runtime._tool_registry.remove(tid):
                deleted.append(tid)
        if deleted:
            runtime._tool_registry.save(_TOOLS_PATH)
        self._send_json_response(200, {"deleted": deleted, "not_found": not_found, "skipped": skipped})

    def _handle_list_mcp_servers(self) -> None:
        """GET /v1/mcp-servers — list persisted MCP server configurations."""
        if os.path.isfile(_MCP_SERVERS_PATH):
            with open(_MCP_SERVERS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"mcpServers": {}}
        self._send_json_response(200, data)

    def _handle_delete_mcp_server(self, server_name: str) -> None:
        """DELETE /v1/mcp-servers/{server_name} — remove an MCP server and all its tools."""
        runtime = self._get_runtime()

        # 1. Remove all tools belonging to this MCP server from the registry
        tool_ids_to_remove = [
            cfg.tool_id
            for cfg in runtime._tool_registry.list_all()
            if cfg.tool_type == "mcp" and cfg.mcp_server_name == server_name
        ]
        for tid in tool_ids_to_remove:
            runtime._tool_registry.remove(tid)
        if tool_ids_to_remove:
            runtime._tool_registry.save(_TOOLS_PATH)

        # 2. Disconnect the live MCP process (if any)
        mcp_manager = runtime._mcp_manager
        if mcp_manager is not None:
            mcp_manager.disconnect(server_name)

        # 3. Remove the server entry from mcp_servers.json
        removed_from_config = False
        if os.path.isfile(_MCP_SERVERS_PATH):
            with open(_MCP_SERVERS_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            servers = saved.get("mcpServers", {})
            if server_name in servers:
                del servers[server_name]
                removed_from_config = True
                with open(_MCP_SERVERS_PATH, "w", encoding="utf-8") as f:
                    json.dump(saved, f, ensure_ascii=False, indent=2)

        if not tool_ids_to_remove and not removed_from_config:
            self._send_json_error(404, f"MCP server not found: {server_name}")
            return

        self._send_json_response(200, {
            "status": "deleted",
            "server_name": server_name,
            "deleted_tools": tool_ids_to_remove,
        })

    def _handle_delete_tool(self, tool_id: str) -> None:
        """DELETE /v1/tools/{tool_id} — delete a tool configuration."""
        runtime = self._get_runtime()
        existing = runtime._tool_registry.get(tool_id)
        if existing is None:
            self._send_json_error(404, f"Tool not found: {tool_id}")
            return
        if existing.builtin:
            self._send_json_error(403, f"Cannot delete built-in tool: {tool_id}")
            return
        runtime._tool_registry.remove(tool_id)
        runtime._tool_registry.save(_TOOLS_PATH)
        self._send_json_response(200, {"status": "deleted", "tool_id": tool_id})

    def _handle_delete_prompt_template(self, template_id: str) -> None:
        """DELETE /v1/prompt-templates/{template_id} — delete a prompt template."""
        mgr = self.server.prompt_template_manager  # type: ignore[attr-defined]
        removed = mgr.delete(template_id)
        if not removed:
            self._send_json_error(404, f"Prompt template not found: {template_id}")
            return

        mgr.save(_PROMPT_TEMPLATES_PATH)
        self._send_json_response(200, {"status": "deleted", "template_id": template_id})


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
        port: int = 8080,
        static_dir: Optional[str] = None,
    ) -> None:
        """Initialize the HTTP server.

        Args:
            runtime: The Runtime instance to serve. If None, a default
                Runtime with empty registries will be created and
                persisted data will be loaded from disk on start.
            host: Bind address (default "0.0.0.0").
            port: Bind port (default 8080).
            static_dir: Optional path to a directory of static files to serve.
                Requests that don't match /v1/* are served from this directory,
                with / and unknown paths falling back to index.html (SPA mode).
                Defaults to the web/dist directory next to this package if it
                exists, otherwise None (static serving disabled).
        """
        _load_env_overrides()
        # Configure logging if not already configured
        if not logging.root.handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            )
        self._host = host
        self._port = port
        self._prompt_template_manager = PromptTemplateManager()
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
            from runtime.builtin_tools import register_builtin_tools
            register_builtin_tools(tool_registry)
            self._runtime = Runtime(
                model_registry=model_registry,
                tool_registry=tool_registry,
                mcp_manager=mcp_manager,
                skill_manager=skill_manager,
                prompt_template_manager=self._prompt_template_manager,
            )

    def start(self) -> None:
        """Start the HTTP server (blocking).

        This method blocks until the server is shut down via stop().
        """
        self._server = ThreadingHTTPServer((self._host, self._port), _RuntimeRequestHandler)
        self._server.runtime = self._runtime  # type: ignore[attr-defined]
        self._server.prompt_template_manager = self._prompt_template_manager  # type: ignore[attr-defined]
        self._server.static_dir = self._static_dir  # type: ignore[attr-defined]
        self._server.serve_forever()

    def start_background(self) -> None:
        """Start the HTTP server in a background daemon thread.

        Returns immediately. Use stop() to shut down.
        """
        self._server = ThreadingHTTPServer((self._host, self._port), _RuntimeRequestHandler)
        self._server.runtime = self._runtime  # type: ignore[attr-defined]
        self._server.prompt_template_manager = self._prompt_template_manager  # type: ignore[attr-defined]
        self._server.static_dir = self._static_dir  # type: ignore[attr-defined]
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
