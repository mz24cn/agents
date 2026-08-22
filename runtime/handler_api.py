"""Registry / env / sessions / agents handler mixin.

Part of the ``_RuntimeRequestHandler`` decomposition in ``runtime.server``.
Provides CRUD endpoints for models, tools, MCP servers, skills, prompt
templates, env vars, sessions (including the SSE events stream) and agents.

Path constants (``_MODELS_PATH`` etc.) are read from ``self.server.*_path``
attributes at request time so ``patch("runtime.server._MODELS_PATH", ...)``
keeps working in tests.

Zero third-party dependencies — only Python standard library.
"""

import datetime
import json
import logging
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

from runtime.agent_manager import validate_agent_id
from runtime.common import session_timestamp
from runtime.context_manager import JournalConflictError
from runtime.models import ModelConfig, ToolConfig
from runtime.skill_manager import SkillManager

_SERVER_STARTED_AT = datetime.datetime.now(datetime.timezone.utc).isoformat()
_SERVER_INSTANCE_ID = uuid.uuid4().hex

from runtime.server_state import (
    _broadcast_session_status,
    _load_function_from_file,
    _session_event_subscribers,
    _session_state_lock,
    _session_statuses,
    _unread_sessions,
)

logger = logging.getLogger("runtime.server")


class HandlerApiMixin:
    def _get_query_param(self, name: str, default=None):
        """Extract a query parameter value from the request URL."""
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        return params.get(name, [default])[0]

    def _handle_list_models(self) -> None:
        """GET /v1/models — list all registered model configurations.

        Query params:
            from_disk (bool): If true, reload from disk before listing.
        """
        runtime = self._get_runtime()
        if self._get_query_param('from_disk') == 'true':
            path = getattr(self.server, 'models_path', None)
            if path and os.path.isfile(path):
                runtime._model_registry.load(path)
        models = runtime._model_registry.list_all()
        data = [m.to_dict() for m in models]
        self._send_json_response(200, {"models": data})

    def _handle_list_tools(self) -> None:
        """GET /v1/tools — list all registered tool configurations.

        Query params:
            from_disk (bool): If true, reload from disk before listing,
                          and restore SkillManager state for skill tools.
        """
        runtime = self._get_runtime()
        if self._get_query_param('from_disk') == 'true':
            path = getattr(self.server, 'tools_path', None)
            if path and os.path.isfile(path):
                # load() clears all tools (including builtins) and reloads only
                # persisted non-builtin tools, so we must re-register builtins.
                from runtime.builtin_tools import register_builtin_tools
                runtime._tool_registry.load(path)
                register_builtin_tools(runtime._tool_registry, runtime=runtime)
                # Restore SkillManager state for persisted skill tools
                skill_manager = getattr(runtime, '_skill_manager', None)
                if skill_manager is not None:
                    for tc in runtime._tool_registry.list_by_type("skill"):
                        if tc.skill_dir:
                            try:
                                skill_manager.load_skill(tc.skill_dir)
                            except ValueError:
                                pass
        tools = runtime._tool_registry.list_all()
        data = [t.to_dict() for t in tools]
        self._send_json_response(200, {"tools": data})

    def _handle_list_prompt_templates(self) -> None:
        """GET /v1/prompt-templates — list all prompt templates.

        Query params:
            from_disk (bool): If true, reload from disk before listing.
        """
        mgr = self.server.prompt_template_manager  # type: ignore[attr-defined]
        if self._get_query_param('from_disk') == 'true':
            path = getattr(self.server, 'prompt_templates_path', None)
            if path and os.path.isfile(path):
                mgr.load(path)
        templates = mgr.list_all()
        data = [t.to_dict() for t in templates]
        self._send_json_response(200, {"templates": data})

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

        now = datetime.datetime.now().isoformat()
        body.setdefault("created_at", now)
        body.setdefault("last_modified", now)

        try:
            config = ModelConfig.from_dict(body)
        except (KeyError, TypeError, ValueError) as exc:
            self._send_json_error(400, f"Invalid model config: {exc}")
            return

        runtime = self._get_runtime()
        runtime._model_registry.register(config)
        runtime._model_registry.save(self.server.models_path)
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
                        timeout=60.0,
                    )
                elif "url" in server_cfg:
                    mcp_manager.connect_url(
                        server_name=server_name,
                        url=server_cfg["url"],
                        headers=server_cfg.get("headers"),
                        timeout=60.0,
                    )
                else:
                    errors.append(f"{server_name}: missing 'command' or 'url'")
                    continue

                # Discover tools — this is the moment the process starts
                discovered = mcp_manager.get_tools(server_name)
                for t in discovered:
                    # Preserve created_at from existing tool if already registered
                    existing = runtime._tool_registry.get(t.tool_id)
                    if existing and existing.created_at:
                        t.created_at = existing.created_at
                    runtime._tool_registry.register(t)
                    registered_tool_ids.append(t.tool_id)
                registered_servers.append(server_name)

            except Exception as exc:
                errors.append(f"{server_name}: {exc}")

        if registered_tool_ids:
            runtime._tool_registry.save(self.server.tools_path)

        # Persist server configs for restart recovery (lazy re-connect on next start)
        if registered_servers:
            saved: dict = {}
            if os.path.isfile(self.server.mcp_servers_path):
                with open(self.server.mcp_servers_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
            saved_servers = saved.setdefault("mcpServers", {})
            for server_name in registered_servers:
                saved_servers[server_name] = body["mcpServers"][server_name]
            # Persist labels at the top level alongside mcpServers
            if "labels" in body and isinstance(body["labels"], dict):
                saved_labels = saved.setdefault("labels", {})
                for server_name in registered_servers:
                    if server_name in body["labels"] and body["labels"][server_name]:
                        saved_labels[server_name] = body["labels"][server_name]
                    elif server_name in saved.get("labels", {}):
                        del saved["labels"][server_name]
                # Remove empty labels dict
                if "labels" in saved and not saved["labels"]:
                    del saved["labels"]
            os.makedirs(self.server.data_dir, exist_ok=True)
            with open(self.server.mcp_servers_path, "w", encoding="utf-8") as f:
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

        runtime._tool_registry.save(self.server.tools_path)
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
        elif body.get("tool_type") == "mcp" and "tool_id" not in body:
            # For MCP tools, generate tool_id from server name if not provided
            server_name = body.get("mcp_server_name", "unknown")
            body.setdefault("tool_id", f"mcp-{server_name}")
        elif body.get("tool_type") == "skill" and "tool_id" not in body:
            # For skill tools, generate tool_id from skill name if not provided
            body.setdefault("tool_id", f"skill-{body.get('name', '')}")

        required = ["tool_id", "tool_type", "name", "description", "parameters"]
        for field in required:
            if field not in body or not str(body[field]).strip():
                self._send_json_error(400, f"Missing required field: {field}")
                return

        now = datetime.datetime.now().isoformat()
        body.setdefault("created_at", now)
        body.setdefault("last_modified", now)

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
                    os.path.expanduser(config.function_file_path), config.function_name
                )
            except (FileNotFoundError, AttributeError, TypeError, RuntimeError) as exc:
                logger.error("加载函数工具失败 [tool_id=%s]: %s", config.tool_id, exc, exc_info=True)
                self._send_json_error(400, f"加载函数失败: {exc}")
                return

        runtime._tool_registry.register(config, callable_fn=callable_fn)
        runtime._tool_registry.save(self.server.tools_path)
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
        labels = body.get("labels")
        template = mgr.create(template_id=body["template_id"], content=body["content"], labels=labels)
        mgr.save(self.server.prompt_templates_path)
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

        # Preserve created_at from existing config, update last_modified
        body.setdefault("created_at", existing.created_at)
        body["last_modified"] = datetime.datetime.now().isoformat()

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
        runtime._model_registry.save(self.server.models_path)
        self._send_json_response(200, {"status": "updated", "model_id": new_model_id})

    def _handle_update_tool(self, tool_id: str) -> None:
        """PUT /v1/tools/{tool_id} — update a tool configuration.

        tool_id 来源逻辑：
        - URL 上的 tool_id 是"旧的" ID，用于查找要更新的工具
        - Body 里的 tool_id 是"新的" ID，用于保存更新后的工具（支持重命名）
        - 如果 body 里没有 tool_id，应理解为 tool_id 维持不变，从 URL 上获取
        """
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

        # Preserve created_at from existing config, update last_modified
        body.setdefault("created_at", existing.created_at)
        body["last_modified"] = datetime.datetime.now().isoformat()

        # 补充缺失的字段：从现有配置中获取默认值
        # tool_type 是必需字段，如果 body 中没有，从现有配置中获取
        if "tool_type" not in body:
            body["tool_type"] = existing.tool_type
        
        # tool_id 更新逻辑
        if body.get("tool_type") == "function":
            # function 工具的 tool_id 由 name 自动生成，不支持自定义
            body["tool_id"] = f"function-{body.get('name', existing.name)}"
        elif "tool_id" not in body:
            # mcp / skill / 其他类型：如果 body 中没有 tool_id，使用 URL 上的 tool_id（保持不变）
            body["tool_id"] = tool_id

        # 补充必需字段（如果 body 中没有提供，使用现有配置的值）
        for field in ("name", "description", "parameters"):
            if field not in body:
                body[field] = getattr(existing, field)
        
        # 补充可选字段（如果 body 中没有提供，保留现有配置的值）
        # 这些字段在更新时如果不提供，应该保持不变而不是被清除
        for field in ("skill_dir", "mcp_server_name", "tool_name", "function_file_path", "function_name"):
            if field not in body and getattr(existing, field) is not None:
                body[field] = getattr(existing, field)

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
                    os.path.expanduser(config.function_file_path), config.function_name
                )
            except (FileNotFoundError, AttributeError, TypeError, RuntimeError) as exc:
                logger.error("更新函数工具失败 [tool_id=%s]: %s", tool_id, exc, exc_info=True)
                self._send_json_error(400, f"加载函数失败: {exc}")
                return

        # For skill tools, reload skill in skill_manager if skill_dir is provided
        if config.tool_type == "skill" and config.skill_dir:
            skill_manager = runtime._skill_manager
            if skill_manager is not None:
                try:
                    skill_manager.load_skill(config.skill_dir)
                except ValueError:
                    pass  # best-effort reload; config change is still persisted

        if config.tool_id != tool_id:
            runtime._tool_registry.remove(tool_id)
        runtime._tool_registry.register(config, callable_fn=callable_fn)
        runtime._tool_registry.save(self.server.tools_path)
        self._send_json_response(200, {"status": "updated", "tool_id": config.tool_id})

    def _handle_update_prompt_template(self, template_id: str) -> None:
        """PUT /v1/prompt-templates/{template_id} — update a prompt template."""
        body = self._read_json_body()
        if body is None:
            return

        new_template_id = body.get("template_id", template_id)
        mgr = self.server.prompt_template_manager  # type: ignore[attr-defined]
        labels = body.get("labels")
        updated = mgr.update(
            template_id,
            new_template_id=new_template_id,
            content=body.get("content", ""),
            labels=labels,
        )
        if updated is None:
            self._send_json_error(404, f"Prompt template not found: {template_id}")
            return

        mgr.save(self.server.prompt_templates_path)
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

        runtime._model_registry.save(self.server.models_path)
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
            runtime._tool_registry.save(self.server.tools_path)
        self._send_json_response(200, {"deleted": deleted, "not_found": not_found, "skipped": skipped})

    def _handle_list_mcp_servers(self) -> None:
        """GET /v1/mcp-servers — list persisted MCP server configurations."""
        if os.path.isfile(self.server.mcp_servers_path):
            with open(self.server.mcp_servers_path, "r", encoding="utf-8") as f:
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
        if os.path.isfile(self.server.mcp_servers_path):
            with open(self.server.mcp_servers_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
        saved_servers = saved.setdefault("mcpServers", {})
        saved_servers[server_name] = body
        # Preserve labels if present in the old saved data
        if "labels" in saved and server_name in saved["labels"]:
            # Keep existing labels (they were set before the edit-delete cycle)
            pass
        os.makedirs(self.server.data_dir, exist_ok=True)
        with open(self.server.mcp_servers_path, "w", encoding="utf-8") as f:
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
            runtime._tool_registry.save(self.server.tools_path)

        # 2. Disconnect the live MCP process (if any)
        mcp_manager = runtime._mcp_manager
        if mcp_manager is not None:
            mcp_manager.disconnect(server_name)

        # 3. Remove the server entry from mcp_servers.json
        removed_from_config = False
        if os.path.isfile(self.server.mcp_servers_path):
            with open(self.server.mcp_servers_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            servers = saved.get("mcpServers", {})
            if server_name in servers:
                del servers[server_name]
                removed_from_config = True
            # Also remove labels for this server
            if "labels" in saved and server_name in saved["labels"]:
                del saved["labels"][server_name]
                if not saved["labels"]:
                    del saved["labels"]
            if removed_from_config:
                with open(self.server.mcp_servers_path, "w", encoding="utf-8") as f:
                    json.dump(saved, f, ensure_ascii=False, indent=2)

        if not tool_ids_to_remove and not removed_from_config:
            self._send_json_error(404, f"MCP server not found: {server_name}")
            return

        self._send_json_response(200, {
            "status": "deleted",
            "server_name": server_name,
            "deleted_tools": tool_ids_to_remove,
        })

    def _handle_test_tool(self) -> None:
        """POST /v1/tools/test — validate a tool without persisting it.

        Function tools are imported from the configured file, skill tools are
        checked for a readable ``SKILL.md``, and MCP tools are connected using
        isolated temporary connection names and queried with ``tools/list``.
        Nothing is added to a registry or written to disk.
        """
        body = self._read_json_body()
        if body is None:
            return

        tool_type = body.get("tool_type")
        try:
            if tool_type == "function":
                file_path = str(body.get("function_file_path", "")).strip()
                function_name = str(body.get("function_name", "")).strip()
                if not file_path or not function_name:
                    raise ValueError("function_file_path and function_name are required")
                _load_function_from_file(os.path.expanduser(file_path), function_name)
                result = {
                    "status": "ok",
                    "tool_type": "function",
                    "function_name": function_name,
                }

            elif tool_type == "skill":
                skill_dir = str(body.get("skill_dir", "")).strip()
                if not skill_dir:
                    raise ValueError("skill_dir is required")
                expanded_dir = os.path.abspath(os.path.expanduser(skill_dir))
                skill_md_path = os.path.join(expanded_dir, "SKILL.md")
                if not os.path.isdir(expanded_dir):
                    raise ValueError(f"Skill directory not found: {expanded_dir}")
                if not os.path.isfile(skill_md_path):
                    raise ValueError(f"SKILL.md not found in {expanded_dir}")
                with open(skill_md_path, "r", encoding="utf-8") as f:
                    f.read(1)
                result = {
                    "status": "ok",
                    "tool_type": "skill",
                    "skill_md_path": skill_md_path,
                }

            elif tool_type == "mcp":
                mcp_config = body.get("mcp_config", {})
                if not isinstance(mcp_config, dict):
                    raise ValueError("mcp_config must be a JSON object")
                servers = mcp_config.get("mcpServers")
                if not isinstance(servers, dict) or not servers:
                    raise ValueError('mcp_config must contain a non-empty "mcpServers" object')

                runtime = self._get_runtime()
                mcp_manager = runtime._mcp_manager
                if mcp_manager is None:
                    raise RuntimeError("MCPClientManager not available")

                test_timeout = 30.0
                server_results = []
                for configured_name, config in servers.items():
                    if not isinstance(config, dict):
                        raise ValueError(f"{configured_name}: config must be a JSON object")
                    temporary_name = f"__mcp_test_{uuid.uuid4().hex}"
                    try:
                        if "command" in config:
                            mcp_manager.connect_stdio(
                                server_name=temporary_name,
                                command=config["command"],
                                args=config.get("args"),
                                env=config.get("env"),
                                timeout=test_timeout,
                            )
                        elif "url" in config:
                            mcp_manager.connect_url(
                                server_name=temporary_name,
                                url=config["url"],
                                headers=config.get("headers"),
                                timeout=test_timeout,
                            )
                        else:
                            raise ValueError(
                                f"{configured_name}: config must contain 'command' or 'url'"
                            )
                        discovered = mcp_manager.get_tools(
                            temporary_name, timeout=test_timeout
                        )
                        server_results.append({
                            "server_name": configured_name,
                            "tools": [t.name for t in discovered],
                        })
                    finally:
                        try:
                            mcp_manager.disconnect(temporary_name)
                        except Exception:
                            pass

                result = {
                    "status": "ok",
                    "tool_type": "mcp",
                    "servers": server_results,
                    "tools": [
                        tool_name
                        for server_result in server_results
                        for tool_name in server_result["tools"]
                    ],
                }

            else:
                raise ValueError("tool_type must be function, skill, or mcp")

            self._send_json_response(200, result)
        except Exception as exc:
            self._send_json_response(200, {
                "status": "error",
                "tool_type": tool_type,
                "error": str(exc),
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
        runtime._tool_registry.save(self.server.tools_path)
        self._send_json_response(200, {"status": "deleted", "tool_id": tool_id})

    def _handle_delete_prompt_template(self, template_id: str) -> None:
        """DELETE /v1/prompt-templates/{template_id} — delete a prompt template."""
        mgr = self.server.prompt_template_manager  # type: ignore[attr-defined]
        removed = mgr.delete(template_id)
        if not removed:
            self._send_json_error(404, f"Prompt template not found: {template_id}")
            return

        mgr.save(self.server.prompt_templates_path)
        self._send_json_response(200, {"status": "deleted", "template_id": template_id})

    # ------------------------------------------------------------------
    # Env handlers
    # ------------------------------------------------------------------

    def _handle_get_env(self) -> None:
        """GET /v1/env — 返回所有环境变量键值对。

        Query params:
            from_disk (bool): If true, re-read from disk before returning.
        """
        env_manager = self.server.env_manager  # type: ignore[attr-defined]
        if self._get_query_param('from_disk') == 'true':
            env_manager._sync_to_environ(env_manager.read())
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
        runtime_dir = os.path.dirname(os.path.abspath(__file__))
        accessories_dir = os.path.realpath(os.path.join(runtime_dir, "..", "accessories"))
        keys_runtime = env_manager.detect_used_keys(runtime_dir)
        keys_accessories = env_manager.detect_used_keys(accessories_dir) if os.path.isdir(accessories_dir) else []
        keys = sorted(set(keys_runtime) | set(keys_accessories))
        self._send_json_response(200, {"keys": keys})

    def _handle_setup_script(self) -> None:
        """GET /v1/setup -- multi-purpose endpoint.

        Operations:
          GET  op=hello   Public version and inference status query.
          GET  op=delta   Authorized minimal tar.gz delta using three version thresholds.
          GET  op=update  Authorized: download remote delta and apply it locally.
          GET  no op      Authorized full self-extracting setup script.
        """
        import os
        import datetime

        # -- op=hello: public version info --
        op = self._get_query_param("op", "")
        if op == "hello":
            script_dir = os.path.dirname(os.path.abspath(__file__))  # runtime/
            project_root = os.path.dirname(script_dir)

            frontend = ""
            build_version_path = os.path.join(project_root, "web", "dist", "build_version")
            try:
                with open(build_version_path, "r") as f:
                    frontend = f.read().strip()
            except (OSError, IOError):
                pass

            runtime_dir = script_dir
            latest_mtime: float = 0.0
            for root, dirs, files in os.walk(runtime_dir):
                for fn in files:
                    if fn.endswith(".py"):
                        path = os.path.join(root, fn)
                        try:
                            mtime = os.stat(path).st_mtime
                            if mtime > latest_mtime:
                                latest_mtime = mtime
                        except OSError:
                            continue

            backend = ""
            if latest_mtime > 0:
                build_dt = datetime.datetime.fromtimestamp(latest_mtime)
                backend = build_dt.strftime("%y%m%d_%H%M%S")

            config_mtime: float = 0.0
            data_dir = self.server.data_dir
            config_paths = [
                os.path.join(data_dir, "models.json"),
                os.path.join(data_dir, "tools.json"),
                os.path.join(data_dir, "mcp_servers.json"),
                os.path.join(data_dir, "prompt_templates.json"),
            ]
            agents_dir = os.path.join(data_dir, "agents")
            if os.path.isdir(agents_dir):
                for root, _dirs, files in os.walk(agents_dir):
                    config_paths.extend(
                        os.path.join(root, filename)
                        for filename in files
                        if filename.endswith(".json")
                    )
            for path in config_paths:
                try:
                    if os.path.isfile(path):
                        config_mtime = max(config_mtime, os.path.getmtime(path))
                except OSError:
                    continue
            last_config = ""
            if config_mtime > 0:
                last_config = datetime.datetime.fromtimestamp(config_mtime).strftime("%y%m%d_%H%M%S")

            inference_active = bool(getattr(self.server, "active_streams", {})) or bool(
                int(getattr(self.server, "active_inference_count", 0) or 0)
            )
            self._send_json_response(200, {
                "frontend_build": frontend,
                "backend_build": backend,
                "last_config": last_config,
                "inference_active": inference_active,
                "server_started_at": _SERVER_STARTED_AT,
                "server_instance_id": _SERVER_INSTANCE_ID,
            })
            return

        env_manager = self.server.env_manager  # type: ignore[attr-defined]
        project_root = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
        data_dir = self.server.data_dir

        if op == "update":
            self._handle_setup_update()
            return

        if op == "delta":
            if self.command != "GET":
                self._send_json_error(405, "op=delta requires GET")
                return
            try:
                frontend_since = self._parse_build_version(
                    self._get_query_param("frontend_build", ""), allow_empty=True)
                backend_since = self._parse_build_version(
                    self._get_query_param("backend_build", ""), allow_empty=True)
                config_since = self._parse_build_version(
                    self._get_query_param("last_config", ""), allow_empty=True)
            except (ValueError, TypeError):
                self._send_json_error(400, "Invalid or missing build version parameter")
                return
            try:
                tar_data = env_manager.build_delta_tar(
                    project_root=project_root,
                    data_dir=data_dir,
                    frontend_since=frontend_since,
                    backend_since=backend_since,
                    config_since=config_since,
                )
            except Exception as exc:
                logger.exception("Failed to build delta tar: %s", exc)
                self._send_json_error(500, f"Failed to build delta tar: {exc}")
                return
            if tar_data is None:
                self.send_response(304)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Disposition", 'inline; filename="delta.tar.gz"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(tar_data)))
            self.end_headers()
            self.wfile.write(tar_data)
            return

        if op:
            self._send_json_error(400, f"Unsupported setup op: {op}")
            return

        # ---- full self-extracting script branch ----
        user_agent = self.headers.get("User-Agent", "")
        ua_lower = user_agent.lower()
        is_windows = "powershell" in ua_lower or "windows" in ua_lower or "pwsh" in ua_lower
        script_format = "ps1" if is_windows else "sh"
        try:
            script = env_manager.build_setup_script(
                project_root=project_root,
                data_dir=data_dir,
                runtime=self.server.runtime,  # type: ignore[attr-defined]
                prompt_template_manager=self.server.prompt_template_manager,  # type: ignore[attr-defined]
                agent_manager=self.server.agent_manager,  # type: ignore[attr-defined]
                script_format=script_format,
            )
        except Exception as exc:
            logger.exception("Failed to build setup script: %s", exc)
            self._send_json_error(500, f"Failed to build setup script: {exc}")
            return

        # Inject SETUP_SOURCE URL into the script (replace placeholder)
        scheme = "https" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else "http"
        host = self.headers.get("Host", "localhost:7988")
        setup_url = f"{scheme}://{host}{self.path}"
        script = script.replace(b"__SETUP_SOURCE_URL__", setup_url.encode("utf-8"))

        self.send_response(200)
        if script_format == "ps1":
            self.send_header("Content-Type", "application/x-powershell; charset=utf-8")
            self.send_header("Content-Disposition", 'inline; filename="setup.ps1"')
        else:
            self.send_header("Content-Type", "application/x-sh; charset=utf-8")
            self.send_header("Content-Disposition", 'inline; filename="setup.sh"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(script)))
        self.end_headers()
        self.wfile.write(script)

    def _handle_setup_update(self) -> None:
        """Download a remote setup delta and apply it to the local environment."""
        source = str(self._get_query_param("source", "")).strip()
        frontend_build = str(self._get_query_param("frontend_build", "")).strip()
        backend_build = str(self._get_query_param("backend_build", "")).strip()
        last_config = str(self._get_query_param("last_config", "")).strip()
        if not source:
            self._send_json_error(400, "Missing required field: source")
            return
        try:
            self._parse_build_version(frontend_build, allow_empty=True)
            self._parse_build_version(backend_build, allow_empty=True)
            self._parse_build_version(last_config, allow_empty=True)
        except (ValueError, TypeError):
            self._send_json_error(400, "Invalid or missing local build version")
            return

        # Atomically block new inference requests while checking/applying update.
        update_lock = getattr(self.server, "inference_update_lock", None)
        if update_lock is None:
            self._send_json_error(500, "Update lock is unavailable")
            return
        with update_lock:
            active_streams = getattr(self.server, "active_streams", {})
            active_count = int(getattr(self.server, "active_inference_count", 0) or 0)
            if active_streams or active_count > 0:
                self._send_json_response(409, {
                    "error": "inference_active",
                    "message": "Cannot update while inference sessions are active",
                })
                return
            if getattr(self.server, "update_in_progress", False):
                self._send_json_response(409, {
                    "error": "update_in_progress",
                    "message": "Another update is already in progress",
                })
                return
            self.server.update_in_progress = True

        def release_update_lock() -> None:
            with update_lock:
                self.server.update_in_progress = False

        parsed = urllib.parse.urlsplit(source)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            release_update_lock()
            self._send_json_error(400, "SETUP_SOURCE must be an http(s) URL")
            return

        if "/v1/setup" in parsed.path:
            setup_path = parsed.path
        else:
            setup_path = "/v1/setup"
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        managed_keys = {"op", "frontend_build", "backend_build", "last_config"}
        query = [(key, value) for key, value in query if key not in managed_keys]
        query.extend([
            ("op", "delta"),
            ("frontend_build", frontend_build),
            ("backend_build", backend_build),
            ("last_config", last_config),
        ])
        update_url = urllib.parse.urlunsplit((
            parsed.scheme,
            parsed.netloc,
            setup_path,
            urllib.parse.urlencode(query),
            "",
        ))

        project_root = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
        try:
            request = urllib.request.Request(update_url, headers={"User-Agent": "Agent-Service-Updater/1"})
            with urllib.request.urlopen(request, timeout=120) as response:
                tar_data = response.read()
        except urllib.error.HTTPError as exc:
            release_update_lock()
            if exc.code == 304:
                self._send_json_response(200, {"updated": False, "restart_backend": False})
                return
            self._send_json_error(502, f"Update source returned HTTP {exc.code}")
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            release_update_lock()
            self._send_json_error(502, f"Failed to download update: {exc}")
            return

        restart_backend = False
        config_updated = False
        updated_files: list[str] = []
        try:
            with tempfile.TemporaryDirectory(prefix="agent-update-") as tmp_dir:
                archive_path = os.path.join(tmp_dir, "update.tar.gz")
                extract_dir = os.path.join(tmp_dir, "extract")
                os.makedirs(extract_dir, exist_ok=True)
                with open(archive_path, "wb") as fh:
                    fh.write(tar_data)

                with tarfile.open(archive_path, mode="r:gz") as tar:
                    members = tar.getmembers()
                    for member in members:
                        name = member.name.replace("\\", "/")
                        normalized = os.path.normpath(name).replace("\\", "/")
                        if (not name or name.startswith("/") or normalized == ".."
                                or normalized.startswith("../") or member.issym() or member.islnk()
                                or not (member.isdir() or member.isfile())):
                            raise ValueError(f"Unsafe archive member: {member.name}")
                        target = os.path.realpath(os.path.join(extract_dir, normalized))
                        if os.path.commonpath([extract_dir, target]) != extract_dir:
                            raise ValueError(f"Archive member escapes target: {member.name}")
                    tar.extractall(extract_dir, members=members)

                # A frontend build produces content-hashed assets. Merging a
                # delta into the existing web/dist would leave obsolete hashes
                # behind indefinitely, so replace the whole compiled frontend
                # directory whenever this delta contains frontend output.
                frontend_delta_dir = os.path.join(extract_dir, "web", "dist")
                has_frontend_update = False
                if os.path.isdir(frontend_delta_dir):
                    for _root, _dirs, files in os.walk(frontend_delta_dir):
                        if files:
                            has_frontend_update = True
                            break
                if has_frontend_update:
                    target_web_dist = os.path.realpath(os.path.join(project_root, "web", "dist"))
                    if (os.path.commonpath([project_root, target_web_dist]) != project_root
                            or os.path.relpath(target_web_dist, project_root).replace("\\", "/") != "web/dist"):
                        raise ValueError("Invalid frontend target directory")
                    if os.path.isdir(target_web_dist):
                        shutil.rmtree(target_web_dist)

                for dirpath, _dirnames, filenames in os.walk(extract_dir):
                    for filename in filenames:
                        source_path = os.path.join(dirpath, filename)
                        relative = os.path.relpath(source_path, extract_dir)
                        relative_posix = relative.replace("\\", "/")
                        if relative_posix.startswith("agents_runtime/"):
                            config_relative = relative_posix[len("agents_runtime/"):]
                            target_root = os.path.realpath(self.server.data_dir)
                            target_path = os.path.realpath(os.path.join(target_root, config_relative))
                            if os.path.commonpath([target_root, target_path]) != target_root:
                                raise ValueError(f"Update config escapes DATA_DIR: {relative}")
                            config_updated = True
                        else:
                            target_root = project_root
                            target_path = os.path.realpath(os.path.join(target_root, relative))
                            if os.path.commonpath([target_root, target_path]) != target_root:
                                raise ValueError(f"Update file escapes project root: {relative}")
                        os.makedirs(os.path.dirname(target_path), exist_ok=True)
                        shutil.copy2(source_path, target_path)
                        updated_files.append(relative_posix)
                        if not relative_posix.startswith("agents_runtime/") and relative_posix.endswith(".py"):
                            restart_backend = True

                # Match the self-extracting install order: after replacing
                # web/dist and copying the downloaded frontend, re-apply the
                # persistent local patch directory so patched frontend files
                # are not lost by the cleanup above.
                if has_frontend_update:
                    patch_root = os.path.realpath(os.path.join(self.server.data_dir, "patch"))
                    data_root = os.path.realpath(self.server.data_dir)
                    if (os.path.commonpath([data_root, patch_root]) != data_root
                            or os.path.relpath(patch_root, data_root).replace("\\", "/") != "patch"):
                        raise ValueError("Invalid patch directory")
                    if os.path.isdir(patch_root):
                        for patch_dirpath, patch_dirnames, patch_filenames in os.walk(patch_root):
                            patch_dirnames[:] = [
                                dirname for dirname in patch_dirnames
                                if not os.path.islink(os.path.join(patch_dirpath, dirname))
                            ]
                            for patch_filename in patch_filenames:
                                patch_source = os.path.join(patch_dirpath, patch_filename)
                                if os.path.islink(patch_source):
                                    continue
                                patch_relative = os.path.relpath(patch_source, patch_root)
                                patch_relative_posix = patch_relative.replace("\\", "/")
                                patch_target = os.path.realpath(os.path.join(project_root, patch_relative))
                                if os.path.commonpath([project_root, patch_target]) != project_root:
                                    raise ValueError(f"Patch file escapes project root: {patch_relative}")
                                os.makedirs(os.path.dirname(patch_target), exist_ok=True)
                                shutil.copy2(patch_source, patch_target)
                                if patch_relative_posix not in updated_files:
                                    updated_files.append(patch_relative_posix)
                                if patch_relative_posix.endswith(".py"):
                                    restart_backend = True
        except (OSError, tarfile.TarError, ValueError) as exc:
            release_update_lock()
            logger.exception("Failed to apply update: %s", exc)
            self._send_json_error(500, f"Failed to apply update: {exc}")
            return

        if config_updated:
            try:
                runtime = self._get_runtime()
                models_path = os.path.join(self.server.data_dir, "models.json")
                tools_path = os.path.join(self.server.data_dir, "tools.json")
                mcp_path = os.path.join(self.server.data_dir, "mcp_servers.json")
                templates_path = os.path.join(self.server.data_dir, "prompt_templates.json")
                if os.path.isfile(models_path):
                    runtime._model_registry.load(models_path)
                if os.path.isfile(tools_path):
                    runtime._tool_registry.load(tools_path)
                    from runtime.builtin_tools import register_builtin_tools
                    register_builtin_tools(runtime._tool_registry, runtime=runtime)
                    skill_manager = getattr(runtime, "_skill_manager", None)
                    if skill_manager is not None:
                        for tool_config in runtime._tool_registry.list_by_type("skill"):
                            if tool_config.skill_dir:
                                try:
                                    skill_manager.load_skill(tool_config.skill_dir)
                                except ValueError:
                                    pass
                if os.path.isfile(mcp_path):
                    with open(mcp_path, "r", encoding="utf-8") as fh:
                        mcp_config = json.load(fh)
                    runtime._mcp_manager.disconnect_all()
                    runtime._mcp_manager._connections.clear()
                    runtime._mcp_manager.load_config(mcp_config, runtime._tool_registry)
                if os.path.isfile(templates_path):
                    self.server.prompt_template_manager.load(templates_path)
                self.server.agent_manager.load()
            except Exception as exc:
                release_update_lock()
                logger.exception("Updated configuration files but failed to reload them: %s", exc)
                self._send_json_error(500, f"Configuration updated but reload failed: {exc}")
                return

        # Frontend-only updates can immediately re-enable inference. If Python
        # files changed, keep inference blocked until execv replaces the process.
        if not restart_backend:
            release_update_lock()
        self._send_json_response(200, {
            "updated": bool(updated_files),
            "restart_backend": restart_backend,
            "updated_files": updated_files,
        })

        if restart_backend:
            app_path = os.path.join(project_root, "app.py")
            argv = [sys.executable, app_path, *sys.argv[1:]]

            def restart_process() -> None:
                try:
                    os.execv(sys.executable, argv)
                except Exception:
                    release_update_lock()
                    logger.exception("Failed to restart backend after update")

            # The response has already been written. Restart promptly; the
            # frontend watches server_instance_id and refreshes as soon as the
            # replacement process answers instead of sleeping a fixed period.
            timer = threading.Timer(0.5, restart_process)
            timer.daemon = True
            timer.start()

    @staticmethod
    def _parse_build_version(value: str, *, allow_empty: bool = False) -> float:
        """Parse YYMMdd_HHmmss into local epoch seconds; empty may mean baseline 0."""
        value = value.strip()
        if not value or value == "0":
            if allow_empty:
                return 0.0
            raise ValueError("empty value")
        dt = datetime.datetime.strptime(value, "%y%m%d_%H%M%S")
        return dt.timestamp()

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
        # SSE streams are not keep-alive compatible: close the connection when
        # the stream ends instead of letting the HTTP/1.1 loop read more.
        self.close_connection = True

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

    def _session_search_max_results(self) -> int:
        """Read SEARCH_MAX_RESULTS at request time as the max session page size.

        Missing, empty, invalid, or non-positive values fall back to 100. The value
        is intentionally read on every request so changes made through env.json / UI
        take effect without restarting the server.
        """
        raw = os.environ.get("SEARCH_MAX_RESULTS", "").strip()
        try:
            limit = int(raw) if raw else 100
        except (TypeError, ValueError):
            limit = 100
        return limit if limit > 0 else 100

    def _paginate_session_results(self, sessions: list[dict], params: dict) -> dict:
        """Paginate an already ordered session result list.

        Query parameters:
          - page: 1-based page number, defaults to 1
          - page_size: requested page size, capped by SEARCH_MAX_RESULTS
        """
        max_page_size = self._session_search_max_results()
        try:
            page = int((params.get("page") or ["1"])[0])
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = int((params.get("page_size") or [str(max_page_size)])[0])
        except (TypeError, ValueError):
            page_size = max_page_size
        page = max(1, page)
        if page_size <= 0:
            page_size = max_page_size
        page_size = min(page_size, max_page_size)

        total = len(sessions)
        start = (page - 1) * page_size
        end = start + page_size
        page_sessions = sessions[start:end]
        return {
            "sessions": page_sessions,
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_more": end < total,
        }

    def _handle_list_sessions(self) -> None:
        """GET /v1/sessions — 分页返回最近会话列表，页大小受 SEARCH_MAX_RESULTS 限制。"""
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        session_manager = self.server.session_manager  # type: ignore[attr-defined]
        result = self._paginate_session_results(session_manager.list_sessions(), params)
        self._send_json_response(200, result)

    def _handle_search_sessions(self) -> None:
        """GET /v1/sessions/search?q=... — 全量搜索后分页返回，页大小受 SEARCH_MAX_RESULTS 限制。"""
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        query = (params.get("q") or [""])[0]
        session_manager = self.server.session_manager  # type: ignore[attr-defined]
        result = self._paginate_session_results(session_manager.search_sessions(query), params)
        self._send_json_response(200, result)

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

    def _handle_session_log_dir(self, session_id: str) -> None:
        """GET /v1/sessions/{session_id}/log-dir — 返回该会话日志目录的绝对路径。

        该路径即 conversation.json 所在目录（DATA_DIR/chat_data/{session_id}），
        用于前端文件管理器直接定位到该目录。
        """
        session_manager = self.server.session_manager  # type: ignore[attr-defined]
        try:
            path = session_manager.session_dir(session_id)
        except FileNotFoundError:
            self._send_json_error(404, f"Session not found: {session_id}")
            return
        except ValueError as exc:
            self._send_json_error(400, str(exc))
            return
        self._send_json_response(200, {"path": path, "session_id": session_id})

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

    def _handle_regenerate_session_summary(self, session_id: str) -> None:
        """POST /v1/sessions/{session_id}/regenerate-summary — 手动重新生成概要和记忆。"""
        context_manager = self.server.context_manager  # type: ignore[attr-defined]
        try:
            # Verify session exists
            conv_path = os.path.join(
                context_manager._chats_dir, session_id, "conversation.json"
            )
            if not os.path.isfile(conv_path):
                self._send_json_error(404, f"Session not found: {session_id}")
                return

            context_manager.compress_context_forced(session_id)
            self._send_json_response(200, {
                "status": "success",
                "session_id": session_id,
            })
        except Exception as exc:
            logging.warning(
                "regenerate-summary: failed for session %s: %s", session_id, exc
            )
            self._send_json_error(500, f"Failed to regenerate summary for session: {session_id}")

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

    def _handle_get_file_journals(self, session_id: str) -> None:
        """GET /v1/sessions/{session_id}/file-journals — list turn keys with file changes."""
        from runtime.context_manager import get_file_journals_list

        context_manager = self.server.context_manager  # type: ignore[attr-defined]
        session_dir = os.path.join(context_manager._chats_dir, session_id)
        try:
            turn_keys = get_file_journals_list(session_dir)
        except Exception as exc:
            self._send_json_error(500, f"Failed to list file journals: {exc}")
            return
        self._send_json_response(200, {"session_id": session_id, "turn_keys": turn_keys})

    def _handle_get_file_journal_diff(self, session_id: str, turn_key: str) -> None:
        """GET /v1/sessions/{session_id}/file-journals/{turn_key} — return per-file diff data."""
        from runtime.context_manager import get_file_journal_diff

        context_manager = self.server.context_manager  # type: ignore[attr-defined]
        session_dir = os.path.join(context_manager._chats_dir, session_id)
        try:
            data = get_file_journal_diff(session_dir, turn_key)
        except Exception as exc:
            self._send_json_error(500, f"Failed to get file journal diff: {exc}")
            return
        if data.get("error") == "not_found":
            self._send_json_error(404, f"No file journal found for turn: {turn_key}")
            return
        self._send_json_response(200, data)


    # ------------------------------------------------------------------
    # Agent handlers
    # ------------------------------------------------------------------

    def _handle_list_agents(self) -> None:
        """GET /v1/agents — list all agents.

        Query params:
            from_disk (bool): If true, reload from disk before listing.
        """
        agent_manager = self.server.agent_manager  # type: ignore[attr-defined]
        if self._get_query_param('from_disk') == 'true':
            agent_manager.load()
        agents = agent_manager.list_all()
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
        agent_id = body.get("agent_id") or session_timestamp()
        try:
            validate_agent_id(agent_id)
        except ValueError as exc:
            self._send_json_error(400, str(exc))
            return
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
            avatar=body.get("avatar", ""),
            labels=body.get("labels"),
        )
        self._send_json_response(201, {"status": "created", "agent_id": agent_id})

    def _handle_update_agent(self, agent_id: str) -> None:
        """PUT /v1/agents/{agent_id} — update an agent."""
        body = self._read_json_body()
        if body is None:
            return
        if "agent_id" in body:
            try:
                validate_agent_id(body["agent_id"])
            except ValueError as exc:
                self._send_json_error(400, str(exc))
                return
        updated = self.server.agent_manager.update(agent_id, body)  # type: ignore[attr-defined]
        if updated is None:
            self._send_json_error(404, f"Agent not found: {agent_id}")
            return
        self._send_json_response(200, {"status": "updated", "agent_id": updated.get("agent_id", agent_id)})

    def _handle_delete_agent(self, agent_id: str) -> None:
        """DELETE /v1/agents/{agent_id} — delete an agent."""
        deleted = self.server.agent_manager.delete(agent_id)  # type: ignore[attr-defined]
        if not deleted:
            self._send_json_error(404, f"Agent not found: {agent_id}")
            return
        self._send_json_response(200, {"status": "deleted", "agent_id": agent_id})
