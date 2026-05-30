"""HTTP API Server for the Agent Service.

Provides RuntimeHTTPServer, a lightweight HTTP server built on Python's
standard library http.server module. Exposes REST endpoints for inference,
tool calling, and registry management.

Zero third-party dependencies — only Python standard library.
"""

import datetime
import importlib.util
import json
import logging
import mimetypes
import os
import re
import sys
import threading
import urllib.parse
from dataclasses import asdict
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Callable, Optional

logger = logging.getLogger("runtime.server")


# ---------------------------------------------------------------------------
# Conversation formatting helpers
# ---------------------------------------------------------------------------


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


def merge_stream_messages(stream_messages: list) -> tuple[list, Optional[dict]]:
    """将流式推理产生的原始 Message 列表合并为 ConversationTurn 列表。

    流式推理中每个 token 都是一条独立的 Message，本函数将它们按语义合并：
    - 连续的 assistant content/thinking/tool_calls 合并为一条 assistant turn
    - tool 角色消息先 flush 当前 assistant turn，再保存 tool result turn
    - usage 消息提取 stat，不生成 turn
    - system 消息跳过

    Args:
        stream_messages: runtime.infer_stream() 产生的原始 Message 对象列表。

    Returns:
        (turns, last_stat) 元组：
        - turns: ConversationTurn 列表，可直接追加到会话历史
        - last_stat: 最后一条 usage 消息解析出的 stat dict，若无则为 None
    """
    from runtime.common import now_iso
    import json as _json

    turns: list = []
    assistant_text_buf: str = ""
    assistant_thinking_buf: str = ""
    pending_tool_calls: list = []
    last_stat: Optional[dict] = None

    def _flush_assistant(stat=None):
        nonlocal assistant_text_buf, assistant_thinking_buf, pending_tool_calls
        if assistant_text_buf or pending_tool_calls or assistant_thinking_buf:
            # timestamp should be the inference completion time from stat
            # Fall back to now_iso() if stat doesn't have it
            ts = stat.get("completed_at") if stat else None
            if not ts:
                ts = now_iso()
            turns.append(ConversationTurn(
                role="assistant",
                content=assistant_text_buf,
                timestamp=ts,
                tool_calls=pending_tool_calls if pending_tool_calls else None,
                thinking=assistant_thinking_buf or None,
                stat=stat,
            ))
            assistant_text_buf = ""
            assistant_thinking_buf = ""
            pending_tool_calls = []

    current_stat: Optional[dict] = None

    for m in stream_messages:
        if m.role == "usage":
            try:
                current_stat = _json.loads(m.content)
                last_stat = current_stat
            except (ValueError, AttributeError):
                pass
            # stat arrives BEFORE tool messages in infer_stream,
            # flush the assistant turn now so it has stat attached
            _flush_assistant(stat=current_stat)
            current_stat = None
            continue
        if m.role == "assistant":
            if m.tool_calls:
                # tool_calls 到来时，合并到当前 assistant turn（不单独 flush）
                for tc in m.tool_calls:
                    idx = tc.get("_index")
                    if idx is None:
                        pending_tool_calls.append(dict(tc))
                        continue
                    while len(pending_tool_calls) <= idx:
                        pending_tool_calls.append({"id": "", "name": "", "arguments": ""})
                    target = pending_tool_calls[idx]
                    if tc.get("id"):
                        target["id"] = tc["id"]
                    if tc.get("tool_use_id"):
                        target["id"] = tc["tool_use_id"]
                    if tc.get("name"):
                        target["name"] = target.get("name", "") + tc["name"]
                    if tc.get("arguments"):
                        if isinstance(tc["arguments"], dict):
                            target["arguments"] = tc["arguments"]
                        else:
                            target["arguments"] = target.get("arguments", "") + tc["arguments"]
            if m.content:
                assistant_text_buf += m.content
            if m.thinking:
                assistant_thinking_buf += m.thinking
        elif m.role == "tool":
            # tool result 到来时，直接 append（assistant 已经在 stat 到来时 flush 了）
            ts = m.timestamp if m.timestamp else now_iso()
            turns.append(ConversationTurn(
                role="tool",
                content=m.content or "",
                timestamp=ts,
                name=m.name or "",
                tool_id=getattr(m, "tool_id", None),
                tool_use_id=getattr(m, "tool_use_id", None),
            ))
        # skip system deltas

    # Flush 剩余 assistant 内容（最后一条，无 tool calls）
    _flush_assistant(stat=current_stat or last_stat)

    return turns, last_stat


def persist_conversation(
    context_manager: "ContextManager",
    session_id: str,
    original_messages: list,
    collected_messages: list,
    session_manager=None,
    tool_ids: Optional[list] = None,
    extra_meta: Optional[dict] = None,
    agent_id: Optional[str] = None,
    agent_nickname: Optional[str] = None,
    model_id: Optional[str] = None,
    workspace: Optional[str] = None,
) -> Optional[Exception]:
    """将一次推理的消息持久化到会话存储。

    提取自 _RuntimeRequestHandler._persist_conversation，供 server 和
    delegate 工具共用，避免两处维护不同的持久化逻辑。

    Args:
        context_manager: ContextManager 实例，负责读写会话文件。
        session_id: 目标会话 ID。
        original_messages: 本次推理的原始输入消息列表（Message 对象）。
        collected_messages: infer_stream 产生的原始流式消息列表（Message 对象）。
        session_manager: 可选的 SessionManager 实例，用于更新 index.json 和
            生成标题。为 None 时跳过 index 更新（适用于 sub-session 场景）。
        tool_ids: 可选的工具 ID 列表，记录到会话 meta 中，便于回溯。
        extra_meta: 可选的额外 meta 字段（如 parent_session_id），与 tool_ids
            一并通过 save_conversation 的 extra_meta 参数一次写入。
        agent_id: 可选的 agent ID，用于标记 role=assistant 的消息的 assistant_id。
        agent_nickname: 可选的 agent nickname，用于标记 role=assistant 的消息的 name 字段。
        model_id: 可选的模型 ID，记录到会话 meta 中，便于恢复会话设置。

    Returns:
        成功时返回 None；失败时返回捕获的异常（OSError 或其他）。
    """
    from runtime.common import now_iso

    try:
        try:
            existing_turns = context_manager.load_conversation(session_id)
        except (FileNotFoundError, ValueError):
            existing_turns = []
        new_turns = list(existing_turns)
        for m in (original_messages or []):
            # 复用 Message 自带的时间戳，无则 fallback 为当前时间
            ts = m.timestamp if m.timestamp else now_iso()
            new_turns.append(ConversationTurn(
                role=m.role,
                content=m.content or "",
                timestamp=ts,
                images=getattr(m, "images", None) or None,
                audio=getattr(m, "audio", None) or None,
                prompt_template=getattr(m, "prompt_template", None) or None,
                arguments=getattr(m, "arguments", None) or None,
                name=getattr(m, "name", None),
                tool_id=getattr(m, "tool_id", None),
                tool_use_id=getattr(m, "tool_use_id", None),
            ))
        merged_turns, last_stat = merge_stream_messages(collected_messages)
        # 如果有 agent_id，为所有 role=assistant 的消息设置 name（nickname）和 assistant_id 字段
        if agent_id:
            for turn in merged_turns:
                if turn.role == "assistant":
                    if agent_nickname:
                        turn.name = agent_nickname
                    turn.assistant_id = agent_id
        new_turns.extend(merged_turns)
        last_total_tokens = (
            (last_stat.get("prompt_tokens", 0) + last_stat.get("completion_tokens", 0))
            if last_stat else None
        ) or None
        merged_extra: Optional[dict] = None
        if tool_ids is not None or extra_meta or model_id or agent_id or workspace:
            merged_extra = {}
            if tool_ids is not None:
                merged_extra["tool_ids"] = tool_ids
            if model_id is not None:
                merged_extra["model_id"] = model_id
            if agent_id is not None:
                merged_extra["agent_id"] = agent_id
            if workspace:
                merged_extra["workspace"] = workspace
            if extra_meta:
                merged_extra.update(extra_meta)
        context_manager.save_conversation(
            session_id, new_turns,
            last_total_tokens=last_total_tokens,
            extra_meta=merged_extra,
        )
        # Trigger context compression exactly once per persistence operation.
        # Use the unified API directly to avoid accidental double-compression
        # through compatibility wrappers.
        context_manager.compress_context(session_id, new_turns, last_total_tokens=last_total_tokens)
        if session_manager is not None:
            session_manager.update_index(session_id, last_total_tokens=last_total_tokens)
    except Exception as exc:
        return exc
    return None


from runtime.common import DATA_DIR as _DATA_DIR, set_request_context, get_request_context, clear_request_context, now_iso as _now_iso, session_timestamp


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
        logger.exception("执行模块 %s 时出错: %s", file_path, e)
        raise RuntimeError(f"执行模块 {file_path} 时出错（详情参见日志）: {e}") from e
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
_AGENTS_DIR = os.path.join(_DATA_DIR, "agents")

# ---------------------------------------------------------------------------
# Session Status Stream – in-memory state
# ---------------------------------------------------------------------------
# Set of session_ids that are currently streaming (registered/cancelled per request).
# Maps session_id -> status string: "streaming" | "done_success_unread" | "done_error_unread"
# "idle" state means session is neither active nor unread (i.e. viewed or just completed-and-read).
_session_statuses: dict[str, str] = {}
# Set of session_ids that have completed but whose result has not been "viewed" yet.
# Entries are status strings: "done_success_unread" or "done_error_unread".
_unread_sessions: dict[str, str] = {}
# List of subscriber callbacks for session-event SSE connections.
# Each entry is a callable(data: dict) -> bool; returns False if write failed (caller removes).
_session_event_subscribers: list = []
# Lock protecting all the above shared state.
_session_state_lock = threading.Lock()


def _broadcast_session_status(session_id: str, status: str) -> None:
    """Broadcast a session status change to all SSE subscribers.

    Called with _session_state_lock held (or outside if safe).  Here we
    iterate a *snapshot* of the subscriber list so we can safely remove
    dead entries without holding the lock during I/O.
    """
    _broadcast_session_event(session_id, "message", {"status": status})


def _broadcast_session_event(session_id: str, event_type: str, data: dict) -> None:
    """Broadcast an arbitrary session event to all SSE subscribers.

    Args:
        session_id: The session this event belongs to.
        event_type: Event type string (e.g. 'message', 'title_update').
        data: Extra key-value pairs merged into the event payload.
    """
    payload = {"event": event_type, "session_id": session_id, **data}
    event_payload = json.dumps(payload, ensure_ascii=False)
    frame = f"data: {event_payload}\n\n"

    # Take snapshot of subscriber list under lock
    with _session_state_lock:
        subscribers_snapshot = list(_session_event_subscribers)

    dead: list = []
    for send_fn in subscribers_snapshot:
        try:
            ok = send_fn(frame)
            if ok is False:
                dead.append(send_fn)
        except Exception:
            dead.append(send_fn)

    if dead:
        with _session_state_lock:
            for fn in dead:
                try:
                    _session_event_subscribers.remove(fn)
                except ValueError:
                    pass


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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Upload-Offset, X-Upload-Size, X-File-Size")
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
        # Strip query string before routing so GET endpoints can accept query params.
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/v1/models":
            self._handle_list_models()
        elif path == "/v1/tools":
            self._handle_list_tools()
        elif path == "/v1/mcp-servers":
            self._handle_list_mcp_servers()
        elif path == "/v1/prompt-templates":
            self._handle_list_prompt_templates()
        elif path == "/v1/env":
            self._handle_get_env()
        elif path == "/v1/setup":
            self._handle_setup_script()
        elif path == "/v1/sessions":
            self._handle_list_sessions()
        elif path == "/v1/sessions/search":
            self._handle_search_sessions()
        elif path == "/v1/sessions/events":
            self._handle_sessions_events()
        elif re.match(r"^/v1/sessions/[^/]+$", path):
            session_id = path[len("/v1/sessions/"):]
            self._handle_get_session(session_id)
        elif path == "/v1/agents":
            self._handle_list_agents()
        elif re.match(r"^/v1/agents/[^/]+$", path):
            agent_id = path[len("/v1/agents/"):]
            self._handle_get_agent(agent_id)
        elif path == "/v1/workspace/list":
            self._handle_workspace_list()
        elif path == "/v1/workspace/children":
            self._handle_workspace_children()
        elif path == "/v1/workspace/search":
            self._handle_workspace_search()
        elif path == "/v1/workspace/content":
            self._handle_workspace_content()
        elif path == "/v1/workspace/download":
            self._handle_workspace_download()
        elif path == "/v1/workspace/thumbnail":
            self._handle_workspace_thumbnail()
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
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/v1/infer":
            self._handle_infer()
        elif path == "/v1/infer/stream":
            self._handle_infer_stream()
        elif path == "/v1/infer/abort":
            self._handle_infer_abort()
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
        elif path == "/v1/env":
            self._handle_set_env()
        elif path == "/v1/env/detect":
            self._handle_detect_env()
        elif re.match(r"^/v1/sessions/[^/]+/generate-title$", path):
            # POST /v1/sessions/{session_id}/generate-title
            session_id = path[len("/v1/sessions/"):-len("/generate-title")]
            self._handle_generate_session_title(urllib.parse.unquote(session_id))
        elif re.match(r"^/v1/sessions/[^/]+/read$", path):
            # POST /v1/sessions/{session_id}/read
            session_id = path[len("/v1/sessions/"):-len("/read")]
            self._handle_mark_session_read(urllib.parse.unquote(session_id))
        elif re.match(r"^/v1/sessions/[^/]+/revoke$", path):
            # POST /v1/sessions/{session_id}/revoke
            session_id = path[len("/v1/sessions/"):-len("/revoke")]
            self._handle_revoke_session(urllib.parse.unquote(session_id))
        elif path == "/v1/agents":
            self._handle_create_agent()
        elif path == "/v1/workspace/rename":
            self._handle_workspace_rename()
        elif path == "/v1/workspace/duplicate":
            self._handle_workspace_duplicate()
        elif path == "/v1/workspace/upload/init":
            self._handle_workspace_upload_init()
        elif re.match(r"^/v1/workspace/upload/[^/]+/complete$", path):
            upload_id = path[len("/v1/workspace/upload/"):-len("/complete")]
            self._handle_workspace_upload_complete(urllib.parse.unquote(upload_id))
        else:
            self._send_json_error(404, f"Not found: {self.path}")

    def do_PUT(self) -> None:
        """Handle PUT requests."""
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        m = re.match(r"^/v1/models/([^/]+)$", path)
        if m:
            self._handle_update_model(m.group(1))
            return
        m = re.match(r"^/v1/tools/([^/]+)$", path)
        if m:
            self._handle_update_tool(m.group(1))
            return
        m = re.match(r"^/v1/mcp-servers/([^/]+)$", path)
        if m:
            self._handle_restore_mcp_server_config(urllib.parse.unquote(m.group(1)))
            return
        m = re.match(r"^/v1/prompt-templates/([^/]+)$", path)
        if m:
            self._handle_update_prompt_template(m.group(1))
            return
        m = re.match(r"^/v1/agents/([^/]+)$", path)
        if m:
            self._handle_update_agent(m.group(1))
            return
        m = re.match(r"^/v1/workspace/upload/([^/]+)/chunk/(\d+)$", path)
        if m:
            self._handle_workspace_upload_chunk(urllib.parse.unquote(m.group(1)), int(m.group(2)))
            return
        self._send_json_error(404, f"Not found: {self.path}")

    def do_DELETE(self) -> None:
        """Handle DELETE requests."""
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
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
        m = re.match(r"^/v1/env/([^/]+)$", path)
        if m:
            self._handle_delete_env(urllib.parse.unquote(m.group(1)))
            return
        m = re.match(r"^/v1/sessions/([^/]+)$", path)
        if m:
            self._handle_delete_session(urllib.parse.unquote(m.group(1)))
            return
        m = re.match(r"^/v1/agents/([^/]+)$", path)
        if m:
            self._handle_delete_agent(m.group(1))
            return
        if path == "/v1/workspace/delete":
            self._handle_workspace_delete()
            return
        m = re.match(r"^/v1/workspace/upload/([^/]+)$", path)
        if m:
            self._handle_workspace_upload_cancel(urllib.parse.unquote(m.group(1)))
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

    def _handle_workspace_list(self) -> None:
        """GET /v1/workspace/list — list files in workspace directory."""
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            
            path = params.get('path', [''])[0]
            page = int(params.get('page', ['1'])[0])
            page_size = int(params.get('page_size', ['50'])[0])
            restrict = params.get('restrict', ['1'])[0] != '0'
            
            if not path:
                self._send_json_error(400, "Missing 'path' parameter")
                return
            
            workspace_mgr = self._get_workspace_manager()
            result = workspace_mgr.list_files(path, page, page_size, restrict_workspace=restrict)
            self._send_json_response(200, result)
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace list error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_children(self) -> None:
        """GET /v1/workspace/children — list child directories of any path (no workspace restriction)."""
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            path = params.get('path', [''])[0]
            
            workspace_mgr = self._get_workspace_manager()
            children = workspace_mgr.list_children(path)
            self._send_json_response(200, children)
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace children error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_search(self) -> None:
        """GET /v1/workspace/search — search files in workspace."""
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            
            path = params.get('path', [''])[0]
            query = params.get('query', [''])[0]
            
            if not path:
                self._send_json_error(400, "Missing 'path' parameter")
                return
            
            if not query:
                self._send_json_error(400, "Missing 'query' parameter")
                return
            
            workspace_mgr = self._get_workspace_manager()
            results = workspace_mgr.search_files(path, query, restrict_workspace=False)
            self._send_json_response(200, results)
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace search error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_content(self) -> None:
        """GET /v1/workspace/content — get file content for preview."""
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            
            path = params.get('path', [''])[0]
            restrict = params.get('restrict', ['1'])[0] != '0'
            
            if not path:
                self._send_json_error(400, "Missing 'path' parameter")
                return
            
            workspace_mgr = self._get_workspace_manager()
            content = workspace_mgr.get_file_content(path, restrict_workspace=restrict)
            file_info = workspace_mgr.get_file_info(path, restrict_workspace=restrict)
            
            content_type = file_info.get('mime_type') or 'application/octet-stream'
            
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace content error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_download(self) -> None:
        """GET /v1/workspace/download — download file."""
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            
            path = params.get('path', [''])[0]
            restrict = params.get('restrict', ['1'])[0] != '0'
            
            if not path:
                self._send_json_error(400, "Missing 'path' parameter")
                return
            
            workspace_mgr = self._get_workspace_manager()
            content = workspace_mgr.get_file_content(path, restrict_workspace=restrict)
            file_info = workspace_mgr.get_file_info(path, restrict_workspace=restrict)
            
            content_type = file_info.get('mime_type') or 'application/octet-stream'
            file_name = file_info.get('name', 'download')
            
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Disposition', f'attachment; filename="{file_name}"')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace download error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_thumbnail(self) -> None:
        """GET /v1/workspace/thumbnail — get image thumbnail."""
        # For now, just serve the original image
        # TODO: Implement actual thumbnail generation
        self._handle_workspace_content()

    # ------------------------------------------------------------------
    # POST handlers
    # ------------------------------------------------------------------

    def _prepare_infer_request(self):
        body = self._read_json_body()
        if body is None:
            return None

        # Resolve agent_id: override model_id, tool_ids, and inject system prompt into messages
        agent_id = body.get("agent_id")
        agent = None
        if agent_id:
            agent = self.server.agent_manager.get(agent_id)  # type: ignore[attr-defined]
            if agent is None:
                self._send_json_error(400, f"Agent not found: {agent_id}")
                return None
            body["model_id"] = agent["model_id"]
            body["tool_ids"] = agent.get("tool_ids", [])

            # On first turn (new session or stateless), prepend agent system prompt to messages
            raw_sid = body.get("session_id") or None
            if raw_sid in ("new", None):
                sys_prompt = agent.get("system_prompt", "")
                template_id = agent.get("template_id")
                template_args = agent.get("template_arguments", {})
                if template_id is not None:
                    sys_msg = {"role": "system", "content": "", "prompt_template": template_id, "arguments": template_args or {}}
                elif sys_prompt:
                    sys_msg = {"role": "system", "content": sys_prompt}
                else:
                    sys_msg = None
                if sys_msg:
                    msgs = body.get("messages", [])
                    # Remove any existing system message, then prepend agent's
                    msgs = [m for m in msgs if m.get("role") != "system"]
                    body["messages"] = [sys_msg] + msgs

        if "model_id" not in body:
            self._send_json_error(400, "Missing required field: model_id")
            return None

        model_override = None
        if "model" in body:
            model_data = body.get("model")
            if not isinstance(model_data, dict):
                self._send_json_error(400, "Field 'model' must be a JSON object")
                return None
            try:
                model_override = ModelConfig.from_dict(model_data)
            except Exception as exc:
                self._send_json_error(400, f"Invalid model config: {exc}")
                return None

        context_manager = self.server.context_manager  # type: ignore[attr-defined]

        # Extract workspace from body early and set on thread_local before
        # parsing messages, so <file>...</file> references resolve against the
        # request workspace rather than a stale/default context.
        workspace = body.get("workspace") or None
        if workspace:
            set_request_context(workspace=workspace)

        raw_session_id: Optional[str] = body.get("session_id") or None
        session_id: Optional[str] = None
        use_session: bool = False

        if raw_session_id is not None:
            if raw_session_id == "new":
                try:
                    session_id = context_manager.create_session()
                    # 提取第一条用户消息文本作为初始标题
                    first_user_msg: Optional[str] = None
                    if "messages" in body:
                        for m in body["messages"]:
                            if m.get("role") == "user":
                                content = m.get("content", "")
                                if isinstance(content, str) and content.strip():
                                    first_user_msg = content.strip()
                                break
                    self.server.session_manager.on_session_created(session_id, first_user_msg)  # type: ignore[attr-defined]
                    use_session = True
                except OSError as exc:
                    self._send_json_error(500, f"Failed to create session: {exc}")
                    return None
            else:
                recovered = context_manager.recover_session(raw_session_id)
                if recovered:
                    session_id = raw_session_id
                    use_session = True
                    logger.info("recovered existing session from disk: %s", session_id)
                else:
                    logger.info(
                        "session_id %s not found on disk; running as stateless inference",
                        raw_session_id,
                    )

        original_messages = None
        user_message_timestamp = None
        if "messages" in body:
            from runtime.common import now_iso
            from runtime.workspace_manager import expand_workspace_file_refs
            from runtime.common import get_workspace as _get_ws
            now_ts = now_iso()
            original_messages = []
            for m in body["messages"]:
                msg = Message.from_dict(m)
                # 为每条消息注入实际创建时间戳
                if msg.timestamp is None:
                    msg.timestamp = now_ts
                if user_message_timestamp is None and msg.role == "user":
                    user_message_timestamp = msg.timestamp
                original_messages.append(msg)
            try:
                original_messages = expand_workspace_file_refs(original_messages, _get_ws())
            except ValueError as exc:
                self._send_json_error(400, str(exc))
                return None

        assembled_messages = original_messages
        if use_session and session_id is not None:
            try:
                new_msgs_dicts = [m.to_dict() for m in original_messages] if original_messages else []
                assembled_dicts = context_manager.assemble_context(session_id, new_msgs_dicts)
                assembled_messages = [Message.from_dict(m) for m in assembled_dicts]
            except OSError as exc:
                self._send_json_error(500, f"Failed to assemble context: {exc}")
                return None

        tool_ids = body.get("tool_ids", [])
        has_delegate = "delegate" in tool_ids

        tool_scope: list = []
        if has_delegate and len(tool_ids) > 1:
            runtime = self._get_runtime()
            mcp_by_server: dict[str, list[str]] = {}
            non_mcp_rows: list[tuple[str, str]] = []

            for tid in tool_ids:
                tc = runtime._tool_registry.get(tid)
                if tc is None:
                    continue
                tool_scope.append(tc)
                if tid == "delegate" and os.environ.get("DISABLE_NESTED_DELEGATE", "false").lower() == "true":
                    continue
                if tc.tool_type == "mcp" and tc.mcp_server_name:
                    mcp_by_server.setdefault(tc.mcp_server_name, []).append(tc.name)
                else:
                    non_mcp_rows.append((tc.name, tc.description))

            rows: list[tuple[str, str]] = list(non_mcp_rows)
            for server_name, names in mcp_by_server.items():
                rows.append((", ".join(names), server_name))

            if rows:
                lines = ["| Tool(s) | Description |", "| --- | --- |"]
                for name, desc in rows:
                    lines.append(f"| {name} | {desc} |")
                tools_markdown = "\n".join(lines)
            else:
                tools_markdown = ""

            if tools_markdown and assembled_messages:
                for msg in assembled_messages:
                    if msg.role == "system":
                        if msg.arguments is None:
                            msg.arguments = {}
                        if not msg.arguments.get("TOOLS"):
                            msg.arguments["TOOLS"] = tools_markdown
                        break

            tool_ids = ["delegate"]

        request = InferenceRequest(
            model_id=body["model_id"],
            model_config_override=model_override,
            tool_ids=tool_ids,
            messages=assembled_messages,
            text=body.get("text"),
            stream=True,
            max_tool_rounds=body.get("max_tool_rounds") or int(os.environ.get("MAX_TOOL_ROUNDS", 100)),
        )

        session_dir = None
        if use_session and session_id is not None:
            session_dir = os.path.dirname(context_manager._conversation_path(session_id))
        set_request_context(
            sse_callback=None,
            session_id=session_id,
            session_dir=session_dir,
            user_message_timestamp=user_message_timestamp,
            depth=0,
            tool_scope=tool_scope,
            context_manager=context_manager,
            session_manager=self.server.session_manager,  # type: ignore[attr-defined]
        )

        agent_nickname = agent.get("nickname") if agent else None
        return body, request, session_id, use_session, original_messages, context_manager, agent_id, agent_nickname, body["model_id"], tool_ids, workspace

    def _cleanup_thread_local(self):
        from runtime.models import Message
        import logging
        logger = logging.getLogger("runtime.server")

        file_journal_manager = get_request_context("file_journal_manager")
        if file_journal_manager is not None:
            try:
                file_journal_manager.flush()
            except Exception as flush_err:
                logger.warning("Error flushing file journal: %s", flush_err)
            finally:
                set_request_context(file_journal_manager=None)

        clear_request_context([
            "sse_callback", "cancel_event", "session_id", "session_dir",
            "user_message_timestamp", "depth", "tool_scope",
            "context_manager", "session_manager", "workspace",
        ])

    def _persist_conversation(self, context_manager, session_id, original_messages, collected_messages, agent_id=None, agent_nickname=None, model_id=None, tool_ids=None, workspace=None):
        if session_id is None:
            return None
        exc = persist_conversation(
            context_manager=context_manager,
            session_id=session_id,
            original_messages=original_messages,
            collected_messages=collected_messages,
            session_manager=self.server.session_manager,  # type: ignore[attr-defined]
            agent_id=agent_id,
            agent_nickname=agent_nickname,
            model_id=model_id,
            tool_ids=tool_ids,
            workspace=workspace,
        )
        return exc

    def _handle_infer(self) -> None:
        """POST /v1/infer — execute model inference (non-streaming).

        This is a thin wrapper around infer_stream that collects all stream
        messages and returns them as a single JSON response. This ensures
        non-streaming inference automatically benefits from any improvements
        to the streaming implementation.
        """
        result = self._prepare_infer_request()
        if result is None:
            return
        _body, request, session_id, use_session, original_messages, context_manager, agent_id, agent_nickname, model_id, tool_ids, workspace = result

        try:
            runtime = self._get_runtime()

            collected_messages: list[Message] = []
            try:
                for msg in runtime.infer_stream(request):
                    collected_messages.append(msg)
                    if msg.role == "assistant" and msg.content and msg.content.startswith("Error:"):
                        logger.error("infer error event | model=%s %s", request.model_id, msg.content)
            except Exception as exc:
                self._send_json_error(500, f"Inference failed: {exc}")
                return

            if agent_id:
                for msg in collected_messages:
                    if msg.role == "assistant":
                        msg.assistant_id = agent_id
                        if agent_nickname:
                            msg.name = agent_nickname

            if use_session:
                persist_exc = self._persist_conversation(context_manager, session_id, original_messages, collected_messages, agent_id, agent_nickname, model_id, tool_ids, workspace)
                if persist_exc is not None:
                    self._send_json_error(500, f"Failed to save conversation: {persist_exc}")
                    return

            merged_turns, merged_last_stat = merge_stream_messages(collected_messages)
            if agent_id:
                for turn in merged_turns:
                    if turn.role == "assistant":
                        turn.assistant_id = agent_id
                        if agent_nickname:
                            turn.name = agent_nickname

            merged_messages = [
                {k: v for k, v in asdict(turn).items() if v is not None}
                for turn in merged_turns
            ]

            has_error = any(
                m.role == "assistant" and m.content and m.content.startswith("Error:")
                for m in collected_messages
            )
            success = not has_error

            stat_dict = merged_last_stat
            if stat_dict is None:
                for m in reversed(collected_messages):
                    if m.role == "usage":
                        try:
                            stat_dict = json.loads(m.content)
                        except (json.JSONDecodeError, ValueError, AttributeError):
                            pass
                        break

            response_data: dict = {
                "success": success,
                "messages": merged_messages,
            }
            if session_id is not None:
                response_data["session_id"] = session_id
            if not success:
                for m in collected_messages:
                    if m.role == "assistant" and m.content and m.content.startswith("Error:"):
                        response_data["error"] = m.content
                        break
            if stat_dict is not None:
                response_data["stat"] = stat_dict

            status = 200 if success else 500
            self._send_json_response(status, response_data)
        finally:
            self._cleanup_thread_local()

    def _handle_infer_stream(self) -> None:
        """POST /v1/infer/stream — execute streaming model inference.

        Returns Server-Sent Events (SSE) stream. 
        First event: 'init' containing session_id and user_message_timestamp.
        Subsequent events: Assistant messages.
        """
        result = self._prepare_infer_request()
        if result is None:
            return
        _body, request, session_id, use_session, original_messages, context_manager, agent_id, agent_nickname, model_id, tool_ids, workspace = result

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        if session_id is not None:
            self.send_header("X-Session-Id", session_id)
        self.end_headers()

        # === 前置发送 Session Init 消息 ===
        # 从 original_messages 中提取用户消息时间戳（已在 _prepare_infer_request 中注入）
        user_message_ts = None
        has_system_prompt = False
        if original_messages:
            if original_messages[0].role == "system":
                has_system_prompt = True
            for m in original_messages:
                if m.role == "user":
                    user_message_ts = m.timestamp
                    break

        # === 生成会话标题（如果尚未设置） ===
        session_title = None
        session = None
        if use_session and session_id:
            try:
                session_manager = self.server.session_manager  # type: ignore[attr-defined]
                session = session_manager.get_session(session_id)
            except Exception:
                pass
        if session and session.get("title"):
            session_title = session["title"]
        elif user_message_ts:
            # 从 original_messages 中提取第一条用户消息内容作为预览标题
            for m in original_messages:
                if m.role == "user":
                    content = m.content.strip() if m.content else ""
                    session_title = content[:30] + ".." if len(content) > 30 else content
                    break
        if not session_title:
            session_title = session_id  # 兜底：用 session_id 作为标题

        init_payload = {
            "session_id": session_id,
            "type": "init",
            "user_message_timestamp": user_message_ts,
            "has_system_prompt": has_system_prompt,
            "agent_id": agent_id,
            "agent_nickname": agent_nickname,
            "title": session_title,
        }
        # 发送 event: init 帧
        self.wfile.write(f"event: init\ndata: {json.dumps(init_payload, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()
        # ================================

        cancel_event = threading.Event()
        runtime = self._get_runtime()
        collected_messages: list[Message] = []

        def _sse_write(frame: dict) -> None:
            try:
                event_data = json.dumps(frame, ensure_ascii=False)
                self.wfile.write(f"data: {event_data}\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

        set_request_context(sse_callback=_sse_write, cancel_event=cancel_event)

        # 注册到 active_streams，使 /v1/infer/abort 可以主动触发中止
        active_streams = getattr(self.server, "active_streams", None)
        if active_streams is not None and session_id is not None:
            active_streams[session_id] = cancel_event

        # --- Session Status Stream: broadcast "streaming" ---
        if session_id is not None:
            with _session_state_lock:
                _session_statuses[session_id] = "streaming"
                _unread_sessions.pop(session_id, None)
            _broadcast_session_status(session_id, "streaming")

        # --- Pre-inference persistence: save user message so conversation.json exists ---
        if use_session:
            pre_exc = self._persist_conversation(
                context_manager, session_id, original_messages, [],
                agent_id, agent_nickname, model_id, tool_ids, workspace,
            )
            if pre_exc is not None:
                logger.error("infer_stream: failed to pre-persist conversation for session %s: %s", session_id, pre_exc)

        try:
            for msg in runtime.infer_stream(request, cancel_event=cancel_event):
                collected_messages.append(msg)
                if msg.role == "assistant" and agent_id:
                    msg.assistant_id = agent_id
                    if agent_nickname:
                        msg.name = agent_nickname
                # delegate 工具通过 sse_callback 自行管理流式帧和结束帧，跳过 infer_stream 的重复输出
                if msg.role == "tool" and msg.name == "delegate":
                    continue
                event_data = json.dumps(msg.to_dict(), ensure_ascii=False)
                self.wfile.write(f"data: {event_data}\n\n".encode("utf-8"))
                self.wfile.flush()
                if msg.role == "assistant" and msg.content and msg.content.startswith("Error:"):
                    logger.error("infer_stream error event | model=%s %s", request.model_id, msg.content)

            # 【已移除】尾部发送 session_id 的逻辑，已在第一条消息中发送

            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

            if use_session:
                # original_messages already saved in pre-inference step, pass [] to avoid duplication
                persist_exc = self._persist_conversation(context_manager, session_id, [], collected_messages, agent_id, agent_nickname, model_id, tool_ids, workspace)
                if persist_exc is not None:
                    logger.error("infer_stream: failed to save conversation for session %s: %s", session_id, persist_exc)

            # --- Session Status Stream: broadcast "done_success_unread" ---
            if session_id is not None:
                with _session_state_lock:
                    _session_statuses[session_id] = "done_success_unread"
                    _unread_sessions[session_id] = "done_success_unread"
                _broadcast_session_status(session_id, "done_success_unread")
        except (BrokenPipeError, ConnectionResetError):
            cancel_event.set()
            if use_session:
                from runtime.common import now_iso
                collected_messages.append(Message(role="assistant", timestamp=now_iso(), content="\n\nError: user interrupted."))
                # original_messages already saved in pre-inference step, pass [] to avoid duplication
                persist_exc = self._persist_conversation(context_manager, session_id, [], collected_messages, agent_id, agent_nickname, model_id, tool_ids, workspace)
                if persist_exc is not None:
                    logger.error("infer_stream: failed to save aborted conversation for session %s: %s", session_id, persist_exc)
            # --- Session Status Stream: broadcast "done_error_unread" ---
            if session_id is not None:
                with _session_state_lock:
                    _session_statuses[session_id] = "done_error_unread"
                    _unread_sessions[session_id] = "done_error_unread"
                _broadcast_session_status(session_id, "done_error_unread")
        except Exception as exc:
            if use_session:
                from runtime.common import now_iso
                collected_messages.append(Message(role="assistant", timestamp=now_iso(), content=f"\n\nError: system aborted. ({exc})"))
                # original_messages already saved in pre-inference step, pass [] to avoid duplication
                persist_exc = self._persist_conversation(context_manager, session_id, [], collected_messages, agent_id, agent_nickname, model_id, tool_ids, workspace)
                if persist_exc is not None:
                    logger.error("infer_stream: failed to save aborted conversation for session %s: %s", session_id, persist_exc)
            try:
                error_data = json.dumps({"error": str(exc)}, ensure_ascii=False)
                self.wfile.write(f"data: {error_data}\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass
            # --- Session Status Stream: broadcast "done_error_unread" ---
            if session_id is not None:
                with _session_state_lock:
                    _session_statuses[session_id] = "done_error_unread"
                    _unread_sessions[session_id] = "done_error_unread"
                _broadcast_session_status(session_id, "done_error_unread")
        finally:
            # 注销 active_streams 中的 cancel_event
            if active_streams is not None and session_id is not None:
                active_streams.pop(session_id, None)
            self._cleanup_thread_local()

    def _handle_infer_abort(self) -> None:
        """POST /v1/infer/abort — 主动中止指定会话的流式推理。

        请求体: {"session_id": "<session_id>", "forced": true|false}
        找到对应的 cancel_event 并 set()，使推理线程在下一个检查点退出。

        当 forced=true 时，还会主动杀死正在执行的工具进程（bash、MCP），
        并强制将会话状态标记为 done_error_unread，适用于工具调用卡死的场景。
        """
        body = self._read_json_body()
        if body is None:
            return
        session_id = body.get("session_id")
        if not session_id:
            self._send_json_error(400, "Missing required field: session_id")
            return
        forced = body.get("forced", False) is True

        active_streams = getattr(self.server, "active_streams", None)
        if active_streams is not None and session_id in active_streams:
            active_streams[session_id].set()

        if forced:
            self._force_abort(session_id)
            self._send_json_response(200, {"ok": True, "forced": True})
        elif active_streams is not None and session_id in active_streams:
            self._send_json_response(200, {"ok": True})
        else:
            # 会话不存在或已结束，视为成功（幂等）
            self._send_json_response(200, {"ok": True, "note": "session not found or already done"})

    def _force_abort(self, session_id: str) -> None:
        """Kill running tool processes and force session status to done."""
        # 1. Kill any running bash process for this session.
        try:
            from runtime.builtin_tools import kill_active_process
            kill_active_process(session_id)
        except Exception as exc:
            logger.error("force_abort: kill_active_process failed for %s: %s", session_id, exc)

        # 2. Kill all MCP stdio server processes so pending call_tool() unblocks.
        try:
            runtime = self._get_runtime()
            mcp_manager = getattr(runtime, "_mcp_manager", None)
            if mcp_manager is not None:
                mcp_manager.abort_all()
        except Exception as exc:
            logger.error("force_abort: mcp abort_all failed for %s: %s", session_id, exc)

        # 3. Force session status to done so the frontend can stop waiting.
        with _session_state_lock:
            _session_statuses[session_id] = "done_error_unread"
            _unread_sessions[session_id] = "done_error_unread"
        _broadcast_session_status(session_id, "done_error_unread")

    @staticmethod
    def _extract_json(text: str) -> Optional[dict | list]:
        """Extract valid JSON from a string.

        Handles:
        - Direct valid JSON
        - JSON wrapped in markdown code blocks (```json ... ```)
        """
        if not text or not isinstance(text, str):
            return None

        s = text.strip()
        if not s:
            return None

        # 1. Try direct parse
        try:
            parsed = json.loads(s)
            if isinstance(parsed, (dict, list)):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass

        # 2. Try extracting from markdown code blocks
        code_block_pattern = re.compile(
            r'`{3}(?:json|JSON)?\s*\n?([\s\S]*?)\n?`{3}',
            re.IGNORECASE,
        )
        match = code_block_pattern.search(s)
        if match:
            block_content = match.group(1).strip()
            try:
                parsed = json.loads(block_content)
                if isinstance(parsed, (dict, list)):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass

        return None

    def _handle_tool_call(self) -> None:
        """POST /v1/tools/call — directly call a tool.

        Expects JSON body with tool_id and arguments.
        Optional 'format' field: if 'json', the result is returned as a parsed JSON object
        instead of a raw string.

        When format='json', the method attempts to extract valid JSON from the result.
        It handles: direct JSON, markdown code blocks, escaped JSON, and embedded JSON
        within mixed text.
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

        fmt = body.get("format", "text")

        runtime = self._get_runtime()
        result = runtime.call_tool(tool_id, arguments)

        # If result starts with "Error:", treat as error
        if result.startswith("Error:"):
            self._send_json_response(400, {"error": result})
        elif fmt == "json":
            parsed = self._extract_json(result)
            if parsed is not None:
                self._send_json_response(200, {"result": parsed})
            else:
                self._send_json_error(400, "Result is not valid JSON and no embedded JSON could be extracted")
                return
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

        if body.get("tool_type") == "function":
            body.setdefault("tool_id", f"function-{body.get('name', '')}")

        required = ["tool_id", "tool_type", "name", "description", "parameters"]
        for field in required:
            if field not in body or not str(body[field]).strip():
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
                logger.error("加载函数工具失败 [tool_id=%s]: %s", config.tool_id, exc, exc_info=True)
                self._send_json_error(400, f"加载函数失败: {exc}")
                return

        runtime._tool_registry.register(config, callable_fn=callable_fn)
        runtime._tool_registry.save(_TOOLS_PATH)
        self._send_json_response(201, {"status": "registered", "tool_id": config.tool_id})

    def _handle_create_prompt_template(self) -> None:
        """POST /v1/prompt-templates — create a new prompt template.

        Expects JSON body with template_id and content fields.
        """
        body = self._read_json_body()
        if body is None:
            return

        if "template_id" not in body:
            self._send_json_error(400, "Missing required field: template_id")
            return
        if "content" not in body:
            self._send_json_error(400, "Missing required field: content")
            return

        mgr = self.server.prompt_template_manager  # type: ignore[attr-defined]
        template = mgr.create(template_id=body["template_id"], content=body["content"])
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

        new_model_id = config.model_id
        if new_model_id != model_id:
            # ID changed: remove old entry and register with new ID
            runtime._model_registry.remove(model_id)
        runtime._model_registry.register(config)
        runtime._model_registry.save(_MODELS_PATH)
        self._send_json_response(200, {"status": "updated", "model_id": new_model_id})

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

        if body.get("tool_type") == "function":
            body["tool_id"] = f"function-{body.get('name', '')}"

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
                logger.error("更新函数工具失败 [tool_id=%s]: %s", tool_id, exc, exc_info=True)
                self._send_json_error(400, f"加载函数失败: {exc}")
                return

        if config.tool_id != tool_id:
            runtime._tool_registry.remove(tool_id)
        runtime._tool_registry.register(config, callable_fn=callable_fn)
        runtime._tool_registry.save(_TOOLS_PATH)
        self._send_json_response(200, {"status": "updated", "tool_id": config.tool_id})

    def _handle_update_prompt_template(self, template_id: str) -> None:
        """PUT /v1/prompt-templates/{template_id} — update a prompt template."""
        body = self._read_json_body()
        if body is None:
            return

        new_template_id = body.get("template_id", template_id)
        mgr = self.server.prompt_template_manager  # type: ignore[attr-defined]
        updated = mgr.update(
            template_id,
            new_template_id=new_template_id,
            content=body.get("content", ""),
        )
        if updated is None:
            self._send_json_error(404, f"Prompt template not found: {template_id}")
            return

        mgr.save(_PROMPT_TEMPLATES_PATH)
        self._send_json_response(200, {"status": "updated", "template_id": new_template_id})

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

    def _handle_restore_mcp_server_config(self, server_name: str) -> None:
        """PUT /v1/mcp-servers/{server_name} — restore/update a single MCP server config.

        Only persists the config to mcp_servers.json without connecting or
        discovering tools.  Used for rollback when a create step fails after
        the old server was already deleted.
        """
        body = self._read_json_body()
        if body is None:
            return

        saved: dict = {}
        if os.path.isfile(_MCP_SERVERS_PATH):
            with open(_MCP_SERVERS_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
        saved_servers = saved.setdefault("mcpServers", {})
        saved_servers[server_name] = body
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_MCP_SERVERS_PATH, "w", encoding="utf-8") as f:
            json.dump(saved, f, ensure_ascii=False, indent=2)
        self._send_json_response(200, {"status": "restored", "server_name": server_name})

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

    # ------------------------------------------------------------------
    # Env handlers
    # ------------------------------------------------------------------

    def _handle_get_env(self) -> None:
        """GET /v1/env — 返回所有环境变量键值对。"""
        env_manager = self.server.env_manager  # type: ignore[attr-defined]
        try:
            env_map = env_manager.read()
        except ValueError as exc:
            self._send_json_error(500, f"env.json format error: {exc}")
            return
        self._send_json_response(200, {"env": env_map})

    def _handle_set_env(self) -> None:
        """POST /v1/env — 新增或更新一个环境变量。"""
        body = self._read_json_body()
        if body is None:
            return
        if "key" not in body:
            self._send_json_error(400, "Missing required field: key")
            return
        key = body["key"]
        if not key:
            self._send_json_error(400, "key 不能为空")
            return
        value = str(body.get("value", ""))
        env_manager = self.server.env_manager  # type: ignore[attr-defined]
        try:
            updated = env_manager.set(key, value)
        except OSError as exc:
            self._send_json_error(500, f"Failed to write env.json: {exc}")
            return
        self._send_json_response(200, {"env": updated})

    def _handle_delete_env(self, key: str) -> None:
        """DELETE /v1/env/{key} — 删除指定环境变量。"""
        env_manager = self.server.env_manager  # type: ignore[attr-defined]
        try:
            updated = env_manager.delete(key)
        except OSError as exc:
            self._send_json_error(500, f"Failed to write env.json: {exc}")
            return
        self._send_json_response(200, {"env": updated})

    def _handle_detect_env(self) -> None:
        """POST /v1/env/detect — 扫描项目目录，返回检测到的 key 列表。"""
        env_manager = self.server.env_manager  # type: ignore[attr-defined]
        keys = env_manager.detect_used_keys(os.path.dirname(os.path.abspath(__file__)))
        self._send_json_response(200, {"keys": keys})

    def _handle_setup_script(self) -> None:
        """GET /v1/setup — 返回当前 agent service 的自解压安装脚本。"""
        env_manager = self.server.env_manager  # type: ignore[attr-defined]
        project_root = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
        try:
            script = env_manager.build_setup_script(
                project_root=project_root,
                data_dir=_DATA_DIR,
                runtime=self.server.runtime,  # type: ignore[attr-defined]
                prompt_template_manager=self.server.prompt_template_manager,  # type: ignore[attr-defined]
                agent_manager=self.server.agent_manager,  # type: ignore[attr-defined]
            )
        except Exception as exc:
            logger.exception("Failed to build setup script: %s", exc)
            self._send_json_error(500, f"Failed to build setup script: {exc}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-sh; charset=utf-8")
        self.send_header("Content-Disposition", 'inline; filename="setup.sh"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(script)))
        self.end_headers()
        self.wfile.write(script)

    # ------------------------------------------------------------------
    # Session handlers
    # ------------------------------------------------------------------

    def _handle_sessions_events(self) -> None:
        """GET /v1/sessions/events — SSE endpoint for session status changes.

        On connect:
          1. Send an `init` event containing the current snapshot of all
             active (streaming) sessions and all unread sessions combined.
          2. Subsequently send `message` events for every status change.

        No heartbeat. Write failure removes the subscriber.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        # Build snapshot under lock
        with _session_state_lock:
            snapshot: dict[str, str] = {}
            # Active / streaming sessions
            for sid, st in _session_statuses.items():
                snapshot[sid] = st
            # Unread sessions (may overlap with active – prefer active)
            for sid, st in _unread_sessions.items():
                if sid not in snapshot:
                    snapshot[sid] = st

        # Send init event
        init_payload = json.dumps({
            "event": "init",
            "sessions": snapshot,
        }, ensure_ascii=False)
        try:
            self.wfile.write(f"data: {init_payload}\n\n".encode("utf-8"))
            self.wfile.flush()
        except Exception:
            return

        # Register this connection's write function
        import queue as _queue
        event_q: _queue.Queue = _queue.Queue()

        def _send(frame: str) -> bool:
            """Enqueue a frame. Returns False only if the queue is full (shouldn't happen)."""
            try:
                event_q.put_nowait(frame)
                return True
            except _queue.Full:
                return False

        with _session_state_lock:
            _session_event_subscribers.append(_send)

        try:
            while True:
                try:
                    frame = event_q.get(timeout=30)  # block up to 30s
                except _queue.Empty:
                    # No events for 30s — just loop; no heartbeat per spec
                    continue
                try:
                    self.wfile.write(frame.encode("utf-8") if isinstance(frame, str) else frame)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        finally:
            with _session_state_lock:
                try:
                    _session_event_subscribers.remove(_send)
                except ValueError:
                    pass

    def _session_search_max_results(self) -> Optional[int]:
        """Read SEARCH_MAX_RESULTS at request time for session list/search limits.

        Empty, missing, invalid, or non-positive values mean "no limit". The value
        is intentionally read on every request so changes made through env.json / UI
        take effect without restarting the server.
        """
        raw = os.environ.get("SEARCH_MAX_RESULTS", "").strip()
        if not raw:
            return None
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            return None
        return limit if limit > 0 else None

    def _limit_session_results(self, sessions: list[dict]) -> list[dict]:
        """Apply SEARCH_MAX_RESULTS to an already ordered session result list."""
        limit = self._session_search_max_results()
        if limit is None:
            return sessions
        return sessions[:limit]

    def _handle_list_sessions(self) -> None:
        """GET /v1/sessions — 返回最近会话列表，按 SEARCH_MAX_RESULTS 限制最大条目数。"""
        session_manager = self.server.session_manager  # type: ignore[attr-defined]
        sessions = self._limit_session_results(session_manager.list_sessions())
        self._send_json_response(200, {"sessions": sessions})

    def _handle_search_sessions(self) -> None:
        """GET /v1/sessions/search?q=... — 全量搜索后按 SEARCH_MAX_RESULTS 限制最大条目数。"""
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        query = (params.get("q") or [""])[0]
        session_manager = self.server.session_manager  # type: ignore[attr-defined]
        sessions = self._limit_session_results(session_manager.search_sessions(query))
        self._send_json_response(200, {"sessions": sessions})

    def _handle_get_session(self, session_id: str) -> None:
        """GET /v1/sessions/{session_id} — 返回指定会话的完整消息记录。

        成功返回后实现"查看即已读"：如果该 session 处于 unread 状态，
        清除 unread 并广播 idle。
        """
        session_manager = self.server.session_manager  # type: ignore[attr-defined]
        try:
            data = session_manager.get_session(session_id)
        except FileNotFoundError:
            # conversation.json 不存在（人为删除或磁盘故障），顺手清理 index
            session_manager.remove_from_index(session_id)
            self._send_json_error(404, f"Session not found: {session_id}")
            return
        except ValueError as exc:
            self._send_json_error(400, f"Invalid conversation format: {exc}")
            return
        self._send_json_response(200, data)

    def _handle_mark_session_read(self, session_id: str) -> None:
        """POST /v1/sessions/{session_id}/read — 将指定会话标记为已读。"""
        was_unread = False
        with _session_state_lock:
            if session_id in _unread_sessions:
                del _unread_sessions[session_id]
                _session_statuses[session_id] = "idle"
                was_unread = True
        if was_unread:
            _broadcast_session_status(session_id, "idle")
        self._send_json_response(200, {"ok": True})

    def _handle_delete_session(self, session_id: str) -> None:
        """DELETE /v1/sessions/{session_id} — 删除指定会话目录。"""
        session_manager = self.server.session_manager  # type: ignore[attr-defined]
        try:
            session_manager.delete_session(session_id)
        except FileNotFoundError:
            self._send_json_error(404, f"Session not found: {session_id}")
            return
        except ValueError as exc:
            self._send_json_error(400, str(exc))
            return
        self._send_json_response(200, {"status": "deleted", "session_id": session_id})

    def _handle_generate_session_title(self, session_id: str) -> None:
        """POST /v1/sessions/{session_id}/generate-title — 手动生成会话标题。"""
        session_manager = self.server.session_manager  # type: ignore[attr-defined]
        try:
            # 强制生成标题（传入 None 表示强制生成，跳过 token 阈值检查）
            title = session_manager.generate_title_forced(session_id)
            if title:
                self._send_json_response(200, {"status": "success", "session_id": session_id, "title": title})
            else:
                self._send_json_error(500, f"Failed to generate title for session: {session_id}")
        except FileNotFoundError:
            self._send_json_error(404, f"Session not found: {session_id}")
            return
        except ValueError as exc:
            self._send_json_error(400, str(exc))
            return

    def _handle_revoke_session(self, session_id: str) -> None:
        """POST /v1/sessions/{session_id}/revoke — 撤回指定用户消息及其后的所有消息。"""
        body = self._read_json_body()
        if body is None:
            return

        timestamp = body.get("timestamp")
        if not timestamp:
            self._send_json_error(400, "Missing required field: timestamp")
            return

        forced = bool(body.get("forced", False))
        keep_files = bool(body.get("keep_files", False))

        context_manager = self.server.context_manager  # type: ignore[attr-defined]
        try:
            revoke_result = context_manager.revoke_conversation(
                session_id,
                timestamp,
                force=forced,
                keep_files=keep_files,
            )

            self._send_json_response(200, {
                "status": "success",
                "session_id": session_id,
                "removed_count": revoke_result.get("removed_count", 0),
                "git": revoke_result.get("git", {}),
                "journal": revoke_result.get("journal", {}),
            })
        except JournalConflictError as exc:
            self._send_json_response(409, exc.to_dict())
            return
        except FileNotFoundError:
            self._send_json_error(404, f"Session not found: {session_id}")
            return
        except ValueError as exc:
            self._send_json_error(400, str(exc))
            return
        except RuntimeError as exc:
            self._send_json_error(500, str(exc))
            return


    # ------------------------------------------------------------------
    # Agent handlers
    # ------------------------------------------------------------------

    def _handle_list_agents(self) -> None:
        """GET /v1/agents — list all agents."""
        agents = self.server.agent_manager.list_all()  # type: ignore[attr-defined]
        self._send_json_response(200, {"agents": agents})

    def _handle_get_agent(self, agent_id: str) -> None:
        """GET /v1/agents/{agent_id} — get a single agent."""
        agent = self.server.agent_manager.get(agent_id)  # type: ignore[attr-defined]
        if agent is None:
            self._send_json_error(404, f"Agent not found: {agent_id}")
            return
        self._send_json_response(200, agent)

    def _handle_create_agent(self) -> None:
        """POST /v1/agents — create a new agent."""
        body = self._read_json_body()
        if body is None:
            return
        required = ["model_id", "nickname"]
        for field in required:
            if field not in body:
                self._send_json_error(400, f"Missing required field: {field}")
                return
        agent_id = session_timestamp()
        self.server.agent_manager.create(  # type: ignore[attr-defined]
            agent_id=agent_id,
            model_id=body["model_id"],
            nickname=body["nickname"],
            tool_ids=body.get("tool_ids"),
            template_id=body.get("template_id"),
            template_arguments=body.get("template_arguments"),
            system_prompt=body.get("system_prompt", ""),
            myself_view=body.get("myself_view", ""),
            description=body.get("description", ""),
            group=body.get("group", ""),
            avatar=body.get("avatar", ""),
        )
        self._send_json_response(201, {"status": "created", "agent_id": agent_id})

    def _handle_update_agent(self, agent_id: str) -> None:
        """PUT /v1/agents/{agent_id} — update an agent."""
        body = self._read_json_body()
        if body is None:
            return
        updated = self.server.agent_manager.update(agent_id, body)  # type: ignore[attr-defined]
        if updated is None:
            self._send_json_error(404, f"Agent not found: {agent_id}")
            return
        self._send_json_response(200, {"status": "updated", "agent_id": agent_id})

    def _handle_delete_agent(self, agent_id: str) -> None:
        """DELETE /v1/agents/{agent_id} — delete an agent."""
        deleted = self.server.agent_manager.delete(agent_id)  # type: ignore[attr-defined]
        if not deleted:
            self._send_json_error(404, f"Agent not found: {agent_id}")
            return
        self._send_json_response(200, {"status": "deleted", "agent_id": agent_id})

    def _handle_workspace_rename(self) -> None:
        """POST /v1/workspace/rename — rename a file or directory."""
        try:
            body = self._read_json_body()
            if body is None:
                return
            
            path = body.get('path')
            new_name = body.get('new_name')
            
            if not path:
                self._send_json_error(400, "Missing 'path' field")
                return
            
            if not new_name:
                self._send_json_error(400, "Missing 'new_name' field")
                return
            
            workspace_mgr = self._get_workspace_manager()
            result = workspace_mgr.rename_file(path, new_name, restrict_workspace=False)
            self._send_json_response(200, result)
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace rename error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_duplicate(self) -> None:
        """POST /v1/workspace/duplicate — create a duplicate of a file."""
        try:
            body = self._read_json_body()
            if body is None:
                return
            
            path = body.get('path')
            
            if not path:
                self._send_json_error(400, "Missing 'path' field")
                return
            
            workspace_mgr = self._get_workspace_manager()
            result = workspace_mgr.duplicate_file(path, restrict_workspace=False)
            self._send_json_response(200, result)
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace duplicate error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_delete(self) -> None:
        """DELETE /v1/workspace/delete — delete a file or directory."""
        try:
            body = self._read_json_body()
            if body is None:
                return
            
            path = body.get('path')
            
            if not path:
                self._send_json_error(400, "Missing 'path' field")
                return
            
            workspace_mgr = self._get_workspace_manager()
            workspace_mgr.delete_file(path, restrict_workspace=False)
            self._send_json_response(200, {"status": "deleted", "path": path})
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace delete error: {e}")
            self._send_json_error(500, "Internal server error")

    def _upload_error_status(self, message: str) -> int:
        if message.startswith("UPLOAD_NOT_FOUND") or message.startswith("CHUNK_NOT_FOUND"):
            return 404
        if message.startswith("UPLOAD_NOT_READY") or message.startswith("UPLOAD_CANCELLED"):
            return 409
        return 400

    def _get_workspace_upload_state(self):
        if not hasattr(self.server, 'workspace_uploads'):
            self.server.workspace_uploads = {}
        if not hasattr(self.server, 'workspace_uploads_lock'):
            self.server.workspace_uploads_lock = threading.Lock()
        return self.server.workspace_uploads, self.server.workspace_uploads_lock

    def _upload_header_int(self, name: str) -> Optional[int]:
        value = self.headers.get(name)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"CHUNK_SIZE_MISMATCH: invalid {name}")

    def _handle_workspace_upload_init(self) -> None:
        try:
            body = self._read_json_body()
            if body is None:
                return
            for field in ('workspace_id', 'file_name', 'file_size', 'target_path'):
                if field not in body:
                    self._send_json_error(400, f"INVALID_REQUEST: missing field {field}")
                    return

            from runtime.workspace_manager import parse_upload_size, parse_upload_max_threads

            parallel_size = parse_upload_size(os.environ.get('UPLOAD_PARALLEL_SIZE'))
            parallel_max_threads = parse_upload_max_threads(os.environ.get('UPLOAD_PARALLEL_MAX_THREADS'))
            workspace_mgr = self._get_workspace_manager()
            task = workspace_mgr.create_upload_task(
                body.get('file_name'),
                body.get('file_size'),
                body.get('target_path'),
                parallel_size,
                parallel_max_threads,
            )
            task['workspace_id'] = body.get('workspace_id')

            uploads, lock = self._get_workspace_upload_state()
            with lock:
                uploads[task['upload_id']] = task

            self._send_json_response(200, {
                "upload_id": task['upload_id'],
                "parallel_size": task['parallel_size'],
                "parallel_max_threads": task['parallel_max_threads'],
                "chunk_count": task['chunk_count'],
                "chunks": [{
                    "parallel_id": chunk['parallel_id'],
                    "offset": chunk['offset'],
                    "size": chunk['size'],
                } for chunk in task['chunks']],
            })
        except ValueError as e:
            self._send_json_error(self._upload_error_status(str(e)), str(e))
        except Exception as e:
            logger.error(f"Workspace upload init error: {e}")
            self._send_json_error(500, "SERVER_ERROR: internal server error")

    def _handle_workspace_upload_chunk(self, upload_id: str, parallel_id: int) -> None:
        uploads, lock = self._get_workspace_upload_state()
        workspace_mgr = self._get_workspace_manager()
        try:
            content_length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self._send_json_error(400, "CHUNK_SIZE_MISMATCH: invalid Content-Length")
            return

        with lock:
            task = uploads.get(upload_id)
            if task is None:
                self._send_json_error(404, "UPLOAD_NOT_FOUND: upload_id not found")
                return
            if task.get('status') == 'cancelled':
                self._send_json_error(409, "UPLOAD_CANCELLED: upload has been cancelled")
                return
            if task.get('status') in {'completing', 'completed'}:
                self._send_json_error(409, "UPLOAD_NOT_READY: upload is not accepting chunks")
                return
            chunks = {chunk['parallel_id']: chunk for chunk in task['chunks']}
            chunk = chunks.get(parallel_id)
            if chunk is None:
                self._send_json_error(404, "CHUNK_NOT_FOUND: parallel_id not found")
                return
            if content_length != chunk['size']:
                self._send_json_error(400, "CHUNK_SIZE_MISMATCH: Content-Length does not match expected size")
                return
            try:
                upload_offset = self._upload_header_int('X-Upload-Offset')
                upload_size = self._upload_header_int('X-Upload-Size')
                file_size = self._upload_header_int('X-File-Size')
            except ValueError as e:
                self._send_json_error(400, str(e))
                return
            if upload_offset is not None and upload_offset != chunk['offset']:
                self._send_json_error(400, "CHUNK_SIZE_MISMATCH: X-Upload-Offset mismatch")
                return
            if upload_size is not None and upload_size != chunk['size']:
                self._send_json_error(400, "CHUNK_SIZE_MISMATCH: X-Upload-Size mismatch")
                return
            if file_size is not None and file_size != task['file_size']:
                self._send_json_error(400, "CHUNK_SIZE_MISMATCH: X-File-Size mismatch")
                return
            chunk['status'] = 'uploading'
            task['status'] = 'uploading'

        try:
            received = workspace_mgr.write_upload_chunk(upload_id, parallel_id, self.rfile, content_length)
            with lock:
                task = uploads.get(upload_id)
                if task is None or task.get('status') == 'cancelled':
                    workspace_mgr.cleanup_upload_temp(upload_id, {"chunks": [{"parallel_id": parallel_id}], "target_path": ""})
                    self._send_json_error(409, "UPLOAD_CANCELLED: upload has been cancelled")
                    return
                for chunk in task['chunks']:
                    if chunk['parallel_id'] == parallel_id:
                        chunk['status'] = 'uploaded'
                        break
            self._send_json_response(200, {
                "upload_id": upload_id,
                "parallel_id": parallel_id,
                "received": received,
                "status": "uploaded",
            })
        except (ConnectionError, ValueError) as e:
            with lock:
                task = uploads.get(upload_id)
                if task:
                    for chunk in task['chunks']:
                        if chunk['parallel_id'] == parallel_id:
                            chunk['status'] = 'pending'
                            break
            self._send_json_error(self._upload_error_status(str(e)), str(e))
        except Exception as e:
            logger.error(f"Workspace upload chunk error: {e}")
            self._send_json_error(500, "SERVER_ERROR: internal server error")

    def _handle_workspace_upload_complete(self, upload_id: str) -> None:
        uploads, lock = self._get_workspace_upload_state()
        workspace_mgr = self._get_workspace_manager()
        with lock:
            task = uploads.get(upload_id)
            if task is None:
                self._send_json_error(404, "UPLOAD_NOT_FOUND: upload_id not found")
                return
            if task.get('status') == 'cancelled':
                self._send_json_error(409, "UPLOAD_CANCELLED: upload has been cancelled")
                return
            if any(chunk.get('status') == 'uploading' for chunk in task['chunks']):
                self._send_json_error(409, "UPLOAD_NOT_READY: some chunks are still uploading")
                return
            if any(chunk.get('status') != 'uploaded' for chunk in task['chunks']):
                self._send_json_error(409, "UPLOAD_NOT_READY: some chunks are missing")
                return
            task['status'] = 'completing'

        try:
            result = workspace_mgr.complete_upload_task(task)
            with lock:
                uploads.pop(upload_id, None)
            self._send_json_response(200, result)
        except ValueError as e:
            with lock:
                task = uploads.get(upload_id)
                if task:
                    task['status'] = 'failed'
            self._send_json_error(self._upload_error_status(str(e)), str(e))
        except Exception as e:
            with lock:
                task = uploads.get(upload_id)
                if task:
                    task['status'] = 'failed'
            logger.error(f"Workspace upload complete error: {e}")
            self._send_json_error(500, "SERVER_ERROR: internal server error")

    def _handle_workspace_upload_cancel(self, upload_id: str) -> None:
        uploads, lock = self._get_workspace_upload_state()
        workspace_mgr = self._get_workspace_manager()
        with lock:
            task = uploads.pop(upload_id, None)
            if task is not None:
                task['status'] = 'cancelled'
        if task is None:
            self._send_json_error(404, "UPLOAD_NOT_FOUND: upload_id not found")
            return
        workspace_mgr.cleanup_upload_temp(upload_id, task)
        self._send_json_response(200, {"upload_id": upload_id, "status": "cancelled"})

    def _get_workspace_manager(self):
        """Get or create workspace manager instance."""
        if not hasattr(self.server, '_workspace_manager'):
            from runtime.common import get_workspace
            workspace_path = get_workspace()
            from runtime.workspace_manager import WorkspaceManager
            self.server._workspace_manager = WorkspaceManager(workspace_path)
        return self.server._workspace_manager

    # ------------------------------------------------------------------
    # RuntimeHTTPServer
    # ------------------------------------------------------------------


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
        )
        # Initialize SessionManager
        self._session_manager = SessionManager(
            chats_dir=chats_dir if chats_dir is not None else os.path.join(_DATA_DIR, "chat_data"),
            infer_fn=self._runtime.infer,
            broadcast_fn=_broadcast_session_event,
        )
        # Log Phase 2 configuration status
        _summary_model = os.environ.get("SUMMARY_MODEL_ID", "")
        _max_tokens = os.environ.get("MAX_TOKENS_IN_CONTEXT", "")
        if _summary_model:
            logger.info(
                "ContextManager Phase 2 enabled: SUMMARY_MODEL_ID=%s, "
                "MAX_TOKENS_IN_CONTEXT=%s (default 65536 if unset). "
                "Both values are re-read from env vars at runtime.",
                _summary_model,
                _max_tokens if _max_tokens else "65536 (default)",
            )
        else:
            logger.info(
                "ContextManager running in Phase 1 (storage-only) mode. "
                "Set SUMMARY_MODEL_ID env var to enable Phase 2 compression "
                "(takes effect immediately without restart)."
            )

    def start(self) -> None:
        """Start the HTTP server (blocking).

        This method blocks until the server is shut down via stop().
        """
        self._active_streams: dict = {}
        self._server = ThreadingHTTPServer((self._host, self._port), _RuntimeRequestHandler)
        self._server.runtime = self._runtime  # type: ignore[attr-defined]
        self._server.prompt_template_manager = self._prompt_template_manager  # type: ignore[attr-defined]
        self._server.agent_manager = self._agent_manager  # type: ignore[attr-defined]
        self._server.static_dir = self._static_dir  # type: ignore[attr-defined]
        self._server.context_manager = self._context_manager  # type: ignore[attr-defined]
        self._server.env_manager = self._env_manager  # type: ignore[attr-defined]
        self._server.session_manager = self._session_manager  # type: ignore[attr-defined]
        self._server.active_streams = self._active_streams  # type: ignore[attr-defined]
        self._server.workspace_uploads = {}  # type: ignore[attr-defined]
        self._server.workspace_uploads_lock = threading.Lock()  # type: ignore[attr-defined]
        self._server.serve_forever()

    def start_background(self) -> None:
        """Start the HTTP server in a background daemon thread.

        Returns immediately. Use stop() to shut down.
        """
        self._active_streams: dict = {}
        self._server = ThreadingHTTPServer((self._host, self._port), _RuntimeRequestHandler)
        self._server.runtime = self._runtime  # type: ignore[attr-defined]
        self._server.prompt_template_manager = self._prompt_template_manager  # type: ignore[attr-defined]
        self._server.agent_manager = self._agent_manager  # type: ignore[attr-defined]
        self._server.static_dir = self._static_dir  # type: ignore[attr-defined]
        self._server.context_manager = self._context_manager  # type: ignore[attr-defined]
        self._server.env_manager = self._env_manager  # type: ignore[attr-defined]
        self._server.session_manager = self._session_manager  # type: ignore[attr-defined]
        self._server.active_streams = self._active_streams  # type: ignore[attr-defined]
        self._server.workspace_uploads = {}  # type: ignore[attr-defined]
        self._server.workspace_uploads_lock = threading.Lock()  # type: ignore[attr-defined]
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
