"""Inference handler mixin: /v1/infer, /v1/infer/stream, /v1/infer/abort, /v1/tools/call.

Part of the ``_RuntimeRequestHandler`` decomposition in ``runtime.server``.

Zero third-party dependencies — only Python standard library.
"""

import copy
import json
import logging
import os
import re
import threading
from dataclasses import asdict
from typing import Optional

from runtime.common import (
    clear_request_context,
    get_request_context,
    now_iso,
    set_request_context,
)
from runtime.builtin_tools_coding import _FileJournalManagerHolder
from runtime.models import InferenceRequest, Message, ModelConfig
from runtime.server_state import (
    begin_session_stream,
    disconnect_session_stream_starter,
    finish_session_stream,
    get_terminal_for_session,
    IncrementalConversationPersister,
    mark_session_stream_persisted,
    merge_stream_messages,
    publish_session_stream_frame,
    persist_conversation,
    stream_batch_is_protocol_complete,
    StreamUsageEstimator,
    transition_session_stream_status,
)

logger = logging.getLogger("runtime.server")


def _add_exec_cli_for_open_terminal(
    tool_ids: list[str],
    session_id: Optional[str],
    is_group_chat: bool,
) -> list[str]:
    """Expose exec_cli when a single-agent session already has an open terminal.

    The terminal must already exist: inference preparation must not create one.
    Preserve the caller's tool selection unchanged when exec_cli is already
    present, and never auto-enable it for group chats.
    """
    if is_group_chat or not session_id or "exec_cli" in tool_ids:
        return tool_ids
    if get_terminal_for_session(session_id) is None:
        return tool_ids
    return [*tool_ids, "exec_cli"]


_stream_batch_is_protocol_complete = stream_batch_is_protocol_complete


class HandlerInferMixin:
    def _prepare_infer_request(self):
        body = self._read_json_body()
        if body is None:
            return None

        # === Retry/continue mode ===
        # Retry removes the final failed assistant turn, then starts inference
        # from the remaining persisted context without appending a user message.
        # retry_agent_id chooses who performs this retry; agent_ids is the
        # participant roster to retain (and may replace the old roster).
        is_continue = body.get("continue") is True
        retry_agent_id = body.get("retry_agent_id") or None

        # Resolve agent_ids: support multi-select and retry takeover.
        agent_ids = list(body.get("agent_ids") or [])
        # A bare retry_agent_id means "retry as this one agent".  An explicit
        # agent_ids value replaces the session roster but does not cause every
        # participant to answer: mentioned_agent_ids below remains one agent.
        if is_continue and retry_agent_id and "agent_ids" not in body:
            agent_ids = [retry_agent_id]
        if retry_agent_id and retry_agent_id not in agent_ids:
            agent_ids.append(retry_agent_id)
        primary_agent_id = retry_agent_id or (agent_ids[0] if agent_ids else None)
        agent = None
        if primary_agent_id:
            agent = self.server.agent_manager.get(primary_agent_id)  # type: ignore[attr-defined]
            if agent is None:
                self._send_json_error(400, f"Agent not found: {primary_agent_id}")
                return None
            body["model_id"] = agent["model_id"]
            body["tool_ids"] = agent.get("tool_ids", [])

        # === Group-chat routing (before system prompt prepend, so we know
        #     whether this is group chat or single-agent) ===
        mentioned_agent_ids: list[str] = []
        if is_continue and retry_agent_id:
            mentioned_agent_ids = [retry_agent_id]
        elif len(agent_ids) > 1:
            from runtime.group_chat import route_group_chat_user_message
            raw_messages = body.get("messages", [])
            for index in range(len(raw_messages) - 1, -1, -1):
                m = raw_messages[index]
                if m.get("role") == "user":
                    prior_turns = raw_messages[:index]
                    raw_sid = body.get("session_id") or None
                    if raw_sid not in (None, "new"):
                        try:
                            prior_turns = self.server.context_manager.load_conversation(  # type: ignore[attr-defined]
                                raw_sid
                            )
                        except (FileNotFoundError, OSError, ValueError):
                            pass
                    mentioned_agent_ids = route_group_chat_user_message(
                        m.get("content", ""),
                        self.server.agent_manager,  # type: ignore[attr-defined]
                        agent_ids,
                        prior_turns,
                    )
                    break
            # Compatibility fallback for malformed requests without a user
            # message, or explicit @mentions that resolve to no participant.
            if not mentioned_agent_ids:
                mentioned_agent_ids = list(agent_ids)

        # On first turn (new session or stateless), prepend agent system prompt.
        # Skip per-agent prompt for group chat — each agent gets its own system
        # prompt in group_chat.py.  Group chat still gets a shared default system
        # prompt so it appears in conversation.json.
        is_group_chat = len(agent_ids) > 1
        if primary_agent_id and not is_group_chat:
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

        if is_group_chat:
            raw_sid = body.get("session_id") or None
            if raw_sid in ("new", None):
                msgs = body.get("messages", [])
                existing_system = any(m.get("role") == "system" for m in msgs)
                if not existing_system:
                    from runtime.group_chat import build_agents_markdown, _GC_DEFAULT_PROMPT
                    agents_md = build_agents_markdown(
                        agent_ids,
                        self.server.agent_manager,  # type: ignore[attr-defined]
                        include_user_row=True,
                    )
                    default_prompt = _GC_DEFAULT_PROMPT.replace("{{AGENTS}}", agents_md)
                    body["messages"] = [{"role": "system", "content": default_prompt}] + msgs

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
        # A request without a session_id parameter is a stateless API call:
        # web conversations always carry session_id ("new" or an existing id).
        # The flag is read by the handlers to track API inference separately so
        # the setup page can distinguish it from visible browser sessions.
        set_request_context(api_inference=raw_session_id is None)

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

        # === Retry validation ===
        continued_user_message_timestamp = None
        if is_continue:
            if raw_session_id in (None, "new"):
                self._send_json_error(400, "continue requires an existing session_id")
                return None
            if not use_session or session_id is None:
                self._send_json_error(404, f"Session not found: {raw_session_id}")
                return None
            if len(agent_ids) > 1 and not retry_agent_id:
                self._send_json_error(400, "group-chat retry requires retry_agent_id")
                return None
            if not retry_agent_id and not body.get("model_id"):
                self._send_json_error(
                    400, "retry requires retry_agent_id or model_id + tool_ids"
                )
                return None
            # Continue is an explicit destructive user action. Do not infer
            # whether the final response is failed or incomplete; remove exactly
            # one final assistant message because the request asked us to.
            removed_message = None
            try:
                # Capture the identity before the atomic rewrite. The removal is
                # later published as a retained control frame so other browsers
                # can apply the same destructive mutation to their live stores.
                turns = context_manager.load_conversation(session_id)
                if turns and turns[-1].role == "assistant":
                    removed_message = turns[-1]
                # Continue retries the same user turn, so all file operations
                # must retain that turn's journal key rather than falling back
                # to the current retry/tool execution time.
                continued_user_message_timestamp = next(
                    (
                        turn.timestamp
                        for turn in reversed(turns)
                        if turn.role == "user" and turn.timestamp
                    ),
                    None,
                )
                removed = context_manager.remove_trailing_assistant_message(session_id)
            except (OSError, ValueError) as exc:
                self._send_json_error(
                    500, f"Failed to remove trailing assistant message: {exc}"
                )
                return None
            if not removed:
                self._send_json_error(
                    400, "continue requires the final conversation turn to be an assistant message"
                )
                return None
            body["_removed_trailing_assistant"] = True
            if removed_message is not None:
                body["_removed_trailing_assistant_timestamp"] = removed_message.timestamp
                body["_removed_trailing_assistant_agent_id"] = removed_message.agent_id
            logger.info(
                "continue: explicitly removed trailing assistant turn from session %s; "
                "retry_agent_id=%s agent_ids=%s model_id=%s tool_ids=%s",
                session_id, retry_agent_id, agent_ids,
                body.get("model_id"), body.get("tool_ids"),
            )

        original_messages = None
        user_message_timestamp = None
        timestamp_fallback_used = False
        if "messages" in body and not is_continue:
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
                # Tag user messages with @-mentioned agent IDs for group chat
                if msg.role == "user" and mentioned_agent_ids:
                    msg.mentions = mentioned_agent_ids
                original_messages.append(msg)
            try:
                original_messages = expand_workspace_file_refs(original_messages, _get_ws())
            except ValueError as exc:
                self._send_json_error(400, str(exc))
                return None
        elif is_continue:
            # 继续推理：不携带新用户消息，基于会话既有上下文推理。
            # File journals still belong to the existing initiating user turn.
            original_messages = []
            user_message_timestamp = continued_user_message_timestamp

        # Establish one stable request-level fallback before any model/tool
        # worker starts. This is a last resort for malformed/history-less input;
        # normal messages and Continue both use a real user-turn timestamp.
        if user_message_timestamp is None:
            user_message_timestamp = now_iso()
            timestamp_fallback_used = True

        assembled_messages = original_messages
        if use_session and session_id is not None:
            try:
                new_msgs_dicts = [m.to_dict() for m in original_messages] if original_messages else []
                assembled_dicts = context_manager.assemble_context(session_id, new_msgs_dicts)
                assembled_messages = [Message.from_dict(m) for m in assembled_dicts]
            except OSError as exc:
                self._send_json_error(500, f"Failed to assemble context: {exc}")
                return None

        # Render request-scoped AGENTS/TOOLS values on an inference-only copy.
        # The unmodified messages remain suitable for conversation persistence.
        if assembled_messages is not None:
            assembled_messages = copy.deepcopy(assembled_messages)

        tool_ids = list(body.get("tool_ids", []))
        tool_ids = _add_exec_cli_for_open_terminal(
            tool_ids,
            session_id,
            is_group_chat,
        )
        # Keep the normalized/augmented selection on the request body so retry
        # logging and conversation metadata reflect the actual inference tools.
        body["tool_ids"] = tool_ids
        has_delegate = "delegate" in tool_ids
        has_talk_to = "talk_to" in tool_ids

        tool_scope: list = []
        # --- TOOLS 占位符（delegate 专用）---
        if has_delegate:
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

            if assembled_messages:
                for msg in assembled_messages:
                    if msg.role == "system":
                        # Merged system messages already carry resolved
                        # content — substitute the placeholder directly.
                        # Unresolved template messages get the value injected
                        # into `arguments` for Runtime._normalize_messages.
                        if "{{TOOLS}}" in (msg.content or ""):
                            msg.content = msg.content.replace("{{TOOLS}}", tools_markdown)
                        else:
                            if msg.arguments is None:
                                msg.arguments = {}
                            # The frontend's current selection is authoritative;
                            # overwrite arguments persisted from an older turn.
                            msg.arguments["TOOLS"] = tools_markdown
                        break

        # --- AGENTS 占位符（talk_to 专用）---
        # 调度清单：排除请求主体自己，只列出可 talk_to 的目标 agent
        if has_talk_to:
            from runtime.group_chat import build_agents_markdown
            agent_manager = self.server.agent_manager  # type: ignore[attr-defined]
            agents_markdown = build_agents_markdown(
                agent_ids, agent_manager,
                exclude_agent_id=primary_agent_id,
            )
            if assembled_messages:
                for msg in assembled_messages:
                    if msg.role == "system":
                        if "{{AGENTS}}" in (msg.content or ""):
                            msg.content = msg.content.replace("{{AGENTS}}", agents_markdown)
                        else:
                            if msg.arguments is None:
                                msg.arguments = {}
                            # The frontend's current roster is authoritative;
                            # overwrite arguments persisted from an older turn.
                            # Empty is valid and prevents a literal placeholder.
                            msg.arguments["AGENTS"] = agents_markdown
                        break

        request = InferenceRequest(
            model_id=body["model_id"],
            model_config_override=model_override,
            tool_ids=tool_ids,
            messages=assembled_messages,
            text=body.get("text"),
            stream=True,
            max_tool_rounds=body.get("max_tool_rounds") or int(os.environ.get("MAX_TOOL_ROUNDS", 200)),
        )

        session_dir = None
        if use_session and session_id is not None:
            session_dir = os.path.dirname(context_manager._conversation_path(session_id))
        set_request_context(
            sse_callback=None,
            session_id=session_id,
            session_dir=session_dir,
            user_message_timestamp=user_message_timestamp,
            user_message_timestamp_fallback_used=timestamp_fallback_used,
            file_journal_holder=_FileJournalManagerHolder(),
            file_journal_manager=None,
            depth=0,
            tool_scope=tool_scope,
            available_tool_ids=tool_ids,
            context_manager=context_manager,
            session_manager=self.server.session_manager,  # type: ignore[attr-defined]
            agent_manager=self.server.agent_manager,  # type: ignore[attr-defined]
            agent_id=primary_agent_id,
            agent_ids=agent_ids,
            all_agent_ids=agent_ids,
            mentioned_agent_ids=mentioned_agent_ids,
            model_id=body["model_id"],
        )

        agent_nickname = agent.get("nickname") if agent else None
        return body, request, session_id, use_session, original_messages, context_manager, agent_ids, agent_nickname, body["model_id"], tool_ids, workspace

    def _finalize_file_journal(self) -> None:
        """Best-effort turn-level reconciliation of registered file changes."""
        journal_holder = get_request_context("file_journal_holder")
        file_journal_manager = get_request_context("file_journal_manager")
        target = journal_holder or file_journal_manager
        if target is None:
            return
        try:
            target.flush()
        except Exception as flush_err:
            logger.warning("Error finalizing file journal: %s", flush_err)

    def _cleanup_thread_local(self):
        self._finalize_file_journal()
        set_request_context(file_journal_manager=None)

        clear_request_context([
            "sse_callback", "cancel_event", "session_id", "session_dir",
            "user_message_timestamp", "user_message_timestamp_fallback_used",
            "depth", "tool_scope", "available_tool_ids",
            "context_manager", "session_manager", "agent_manager", "workspace",
            "agent_id", "agent_ids", "all_agent_ids", "mentioned_agent_ids",
            "file_journal_holder", "file_journal_session_id",
            "file_journal_session_dir", "file_journal_user_message_timestamp",
            "file_journal_timestamp_fallback_used",
        ])

    def _persist_conversation(self, context_manager, session_id, original_messages, collected_messages, agent_ids=None, agent_nickname=None, model_id=None, tool_ids=None, workspace=None, compress=True, update_title=True):
        if session_id is None:
            return None
        exc = persist_conversation(
            context_manager=context_manager,
            session_id=session_id,
            original_messages=original_messages,
            collected_messages=collected_messages,
            session_manager=self.server.session_manager,  # type: ignore[attr-defined]
            agent_ids=agent_ids,
            agent_nickname=agent_nickname,
            model_id=model_id,
            tool_ids=tool_ids,
            workspace=workspace,
            compress=compress,
            update_title=update_title,
        )
        return exc

    def _enter_api_inference(self) -> bool:
        """Begin server-level tracking of a stateless (API) inference.

        Web requests always carry a session_id and are already tracked per
        session via ``active_streams`` and the session status events.  A
        stateless API call is invisible to the web UI, so the setup page
        needs a separate counter to report it.  Returns True when this
        request is stateless and the counter was incremented.
        """
        if get_request_context("api_inference") is not True:
            return False
        server = self.server
        lock = getattr(server, "inference_update_lock", None)
        if lock is not None:
            with lock:
                server.active_api_inference_count = int(
                    getattr(server, "active_api_inference_count", 0) or 0
                ) + 1
        else:
            server.active_api_inference_count = int(
                getattr(server, "active_api_inference_count", 0) or 0
            ) + 1
        return True

    def _leave_api_inference(self) -> None:
        """Stop server-level tracking of a stateless (API) inference."""
        server = self.server
        lock = getattr(server, "inference_update_lock", None)
        if lock is not None:
            with lock:
                server.active_api_inference_count = max(
                    0, int(getattr(server, "active_api_inference_count", 0) or 0) - 1
                )
        else:
            server.active_api_inference_count = max(
                0, int(getattr(server, "active_api_inference_count", 0) or 0) - 1
            )

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
        _body, request, session_id, use_session, original_messages, context_manager, agent_ids, agent_nickname, model_id, tool_ids, workspace = result

        api_inference = self._enter_api_inference()
        try:
            runtime = self._get_runtime()

            collected_messages: list[Message] = []
            # === Group chat routing (non-streaming) ===
            mentioned_agent_ids = get_request_context("mentioned_agent_ids") or []
            try:
                if len(agent_ids) > 1:
                    # Multi-agent group chat without SSE streaming
                    from runtime.group_chat import run_group_chat_stream
                    collected_messages = run_group_chat_stream(
                        runtime=runtime,
                        mentioned_agent_ids=mentioned_agent_ids,
                        all_agent_ids=agent_ids,
                        original_messages=original_messages,
                        base_request=request,
                        cancel_event=None,  # non-streaming: no cancel support
                        sse_callback=None,   # non-streaming: no SSE output
                        context_manager=context_manager,
                        session_id=session_id,
                        agent_manager=self.server.agent_manager,  # type: ignore[attr-defined]
                        model_id=model_id,
                        tool_ids=tool_ids,
                    )
                else:
                    for msg in runtime.infer_stream(request):
                        collected_messages.append(msg)
                        if msg.role == "assistant" and msg.content and msg.content.startswith("Error:"):
                            logger.error("infer error event | model=%s %s", request.model_id, msg.content)
            except Exception as exc:
                self._send_json_error(500, f"Inference failed: {exc}")
                return

            if agent_ids and len(agent_ids) <= 1:
                for msg in collected_messages:
                    if msg.role == "assistant":
                        msg.agent_id = agent_ids[0]
                        if agent_nickname:
                            msg.name = agent_nickname

            if use_session:
                persist_exc = self._persist_conversation(context_manager, session_id, original_messages, collected_messages, agent_ids, agent_nickname, model_id, tool_ids, workspace)
                if persist_exc is not None:
                    self._send_json_error(500, f"Failed to save conversation: {persist_exc}")
                    return

            merged_turns, merged_last_stat = merge_stream_messages(collected_messages)
            if agent_ids:
                for turn in merged_turns:
                    if turn.role == "assistant":
                        if agent_nickname and not turn.name:
                            turn.name = agent_nickname
                        if not turn.agent_id:
                            turn.agent_id = agent_ids[0]

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
            # Make the journal final before the response becomes observable.
            self._finalize_file_journal()
            self._send_json_response(status, response_data)
        finally:
            self._cleanup_thread_local()
            if api_inference:
                self._leave_api_inference()

    def _handle_infer_stream(self) -> None:
        """POST /v1/infer/stream — execute streaming model inference.

        Returns Server-Sent Events (SSE) stream. 
        First event: 'init' containing session_id and user_message_timestamp.
        Subsequent events: Assistant messages.
        """
        result = self._prepare_infer_request()
        if result is None:
            return
        _body, request, session_id, use_session, original_messages, context_manager, agent_ids, agent_nickname, model_id, tool_ids, workspace = result

        api_inference = self._enter_api_inference()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        if session_id is not None:
            self.send_header("X-Session-Id", session_id)
        self.end_headers()
        # SSE streams are not keep-alive compatible: close the connection when
        # the stream ends instead of letting the HTTP/1.1 loop read more.
        self.close_connection = True

        # === 前置发送 Session Init 消息 ===
        # 从 original_messages 中提取用户消息时间戳（已在 _prepare_infer_request 中注入）
        # Continue has no new user message, but still exposes the stable existing
        # user-turn timestamp selected during preparation.
        user_message_ts = get_request_context("user_message_timestamp")
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
        elif user_message_ts and original_messages:
            # 从 original_messages 中提取第一条用户消息内容作为预览标题
            for m in original_messages:
                if m.role == "user":
                    content = m.content.strip() if m.content else ""
                    session_title = content[:30] + ".." if len(content) > 30 else content
                    break
        if not session_title:
            session_title = session_id  # 兜底：用 session_id 作为标题

        # Check if group chat mode is active
        mentioned_for_init = get_request_context("mentioned_agent_ids") or []
        is_group_chat = len(agent_ids) > 1

        init_payload = {
            "session_id": session_id,
            "type": "init",
            "user_message_timestamp": user_message_ts,
            "has_system_prompt": has_system_prompt,
            "agent_ids": agent_ids,
            "agent_nickname": None if is_group_chat else agent_nickname,
            "title": session_title,
        }
        if _body.get("_removed_trailing_assistant") is True:
            init_payload["removed_trailing_assistant"] = True
        if _body.get("retry_agent_id"):
            init_payload["retry_agent_id"] = _body["retry_agent_id"]
        if is_group_chat:
            init_payload["group_chat"] = True
            init_payload["mentioned_agent_ids"] = mentioned_for_init
        cancel_event = threading.Event()
        runtime = self._get_runtime()
        collected_messages: list[Message] = []
        # A single SSE connection may receive frames from nested-tool worker
        # threads as well as the HTTP handler thread.  Serialize every write so
        # JSON events and heartbeat comments can never interleave on wfile.
        sse_write_lock = threading.Lock()
        client_connected = True
        latest_stream_seq = 0
        stream_debug = os.environ.get("SESSION_STREAM_DEBUG", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        if session_id is not None:
            begin_session_stream(session_id, cancel_event)
            # Continue has already removed the persisted final assistant turn.
            # Retain a control frame before status broadcast so every other
            # browser applies the same mutation before replacement output starts.
            if _body.get("_removed_trailing_assistant") is True:
                remove_frame = {
                    "type": "remove_trailing_assistant",
                    "removed_timestamp": _body.get("_removed_trailing_assistant_timestamp"),
                    "removed_agent_id": _body.get("_removed_trailing_assistant_agent_id"),
                }
                published_seq = publish_session_stream_frame(
                    session_id, remove_frame, owner_event=cancel_event,
                )
                if published_seq is not None:
                    latest_stream_seq = published_seq
            # The initiating browser renders the submitted user turn from the
            # POST stream's init event, but other browsers only consume the
            # retained session stream. Publish the user turn into that broker
            # before announcing "streaming" so a newly attached browser sees
            # the prompt before any assistant/tool frames. Use the request body
            # rather than expanded workspace content to preserve exactly what
            # the user submitted in the UI.
            raw_user_messages = [
                raw for raw in (_body.get("messages") or [])
                if isinstance(raw, dict) and raw.get("role") == "user"
            ]
            for raw_user in raw_user_messages:
                user_frame = dict(raw_user)
                user_frame["role"] = "user"
                if not user_frame.get("timestamp") and user_message_ts:
                    user_frame["timestamp"] = user_message_ts
                if mentioned_for_init and not user_frame.get("mentions"):
                    user_frame["mentions"] = mentioned_for_init
                published_seq = publish_session_stream_frame(
                    session_id, user_frame, owner_event=cancel_event,
                )
                if published_seq is not None:
                    latest_stream_seq = published_seq

        # The starter consumes the same retained sequence space as every GET
        # subscriber. Expose the baseline before the first mirrored frame so it
        # can hand off to GET /sessions/{id}/stream without duplicates.
        init_payload["stream_seq"] = latest_stream_seq
        self.wfile.write(f"event: init\ndata: {json.dumps(init_payload, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()
        # ================================

        def _write_sse_payload(payload: bytes) -> None:
            with sse_write_lock:
                self.wfile.write(payload)
                self.wfile.flush()

        def _mark_starter_disconnected(exc: Exception) -> None:
            nonlocal client_connected
            if not client_connected:
                return
            client_connected = False
            cancelled = bool(session_id) and disconnect_session_stream_starter(
                session_id, owner_event=cancel_event,
            )
            logger.warning(
                "infer_stream: starter SSE disconnected%s: %s: %s",
                "; cancelling unobserved non-flight inference" if cancelled else "",
                type(exc).__name__, exc,
            )

        def _sse_write(frame: dict, event: Optional[str] = None) -> bool:
            """Publish once, then best-effort mirror to the starter browser.

            Losing the starter cancels a non-flight inference only when no other
            browser has opened and is still subscribed to this session.
            """
            nonlocal latest_stream_seq
            published_seq = publish_session_stream_frame(
                session_id, frame, owner_event=cancel_event, event=event,
            )
            if published_seq is None:
                # This handler has been superseded by a newer inference for the
                # same session. Never leak old tail frames into the new broker.
                return False
            latest_stream_seq = published_seq
            if not client_connected:
                return True
            try:
                if stream_debug:
                    logger.warning(
                        "session_stream starter_write_begin sid=%s seq=%s role=%s name=%s streaming=%s tool_use_id=%s",
                        session_id, latest_stream_seq, frame.get("role"), frame.get("name"),
                        frame.get("streaming"), frame.get("tool_use_id"),
                    )
                event_data = json.dumps(frame, ensure_ascii=False)
                event_line = f"event: {event}\n" if event else ""
                _write_sse_payload(
                    f"id: {latest_stream_seq}\n{event_line}data: {event_data}\n\n".encode("utf-8")
                )
                if stream_debug:
                    logger.warning(
                        "session_stream starter_write_end sid=%s seq=%s",
                        session_id, latest_stream_seq,
                    )
            except Exception as exc:
                _mark_starter_disconnected(exc)
            return True

        def _sse_heartbeat() -> None:
            """Best-effort heartbeat for the starter connection."""
            if not client_connected:
                return
            try:
                _write_sse_payload(b": keepalive\n\n")
            except Exception as exc:
                _mark_starter_disconnected(exc)

        set_request_context(sse_callback=_sse_write, cancel_event=cancel_event)

        # 注册到 active_streams，使 /v1/infer/abort 可以主动触发中止
        active_streams = getattr(self.server, "active_streams", None)
        if active_streams is not None and session_id is not None:
            active_streams[session_id] = cancel_event

        # --- Session Status Stream: broadcast "streaming" ---
        if session_id is not None:
            transition_session_stream_status(session_id, cancel_event, "streaming")

        usage_estimator = StreamUsageEstimator(
            context_manager=context_manager if use_session else None,
            session_id=session_id if use_session else None,
            original_messages=original_messages,
            # ``request.messages`` is the complete assembled context actually
            # handed to Runtime, unlike ``original_messages`` which normally
            # contains only this HTTP request's new user turn.
            request_messages=request.messages,
            tools=[
                tool
                for tool_id in request.tool_ids
                if (tool := runtime._tool_registry.get(tool_id)) is not None
            ],
        )
        conversation_persister = IncrementalConversationPersister(
            context_manager=context_manager,
            session_id=session_id if use_session else None,
            original_messages=original_messages,
            session_manager=self.server.session_manager,  # type: ignore[attr-defined]
            agent_ids=agent_ids,
            agent_nickname=agent_nickname,
            model_id=model_id,
            tool_ids=tool_ids,
            workspace=workspace,
            is_active=lambda: self._is_active_stream(session_id, cancel_event),
            on_incremental_persist=lambda: mark_session_stream_persisted(
                session_id, latest_stream_seq, owner_event=cancel_event,
            ) if session_id is not None else None,
        )

        # Save the input turn before inference so conversation.json exists.
        # Continue requests have no original messages, making this a no-op.
        if use_session and original_messages:
            pre_exc = conversation_persister.pre_persist()
            if pre_exc is not None:
                logger.error("infer_stream: failed to pre-persist conversation for session %s: %s", session_id, pre_exc)

        try:
            # === Group chat routing ===
            mentioned_agent_ids = get_request_context("mentioned_agent_ids") or []
            is_group_chat = len(agent_ids) > 1

            def _incremental_persist():
                """将自上次落盘以来新收集的完整轮次写入 conversation.json。

                仅在收到 role=tool 消息后调用——此时 assistant(tool_calls) +
                usage + tool(result) 已配对完整，merge_stream_messages 能正确
                合并，保证 conversation.json 中不会出现孤立 tool_calls。
                compress=False：推理过程中不触发 LLM 摘要压缩（最终持久化才做）。
                """
                exc = conversation_persister.persist_completed(collected_messages)
                if exc is not None:
                    logger.error(
                        "infer_stream: incremental persist failed for session %s: %s",
                        session_id, exc,
                    )

            # === 统一消息源 ===
            if is_group_chat:
                from runtime.group_chat import run_group_chat_stream_gen
                msg_gen = run_group_chat_stream_gen(
                    runtime=runtime,
                    mentioned_agent_ids=mentioned_agent_ids,
                    all_agent_ids=agent_ids,
                    original_messages=original_messages,
                    base_request=request,
                    cancel_event=cancel_event,
                    sse_callback=_sse_write,
                    sse_heartbeat=_sse_heartbeat,
                    context_manager=context_manager,
                    session_id=session_id,
                    agent_manager=self.server.agent_manager,  # type: ignore[attr-defined]
                    model_id=model_id,
                    tool_ids=tool_ids,
                )
            else:
                msg_gen = runtime.infer_stream(request, cancel_event=cancel_event)

            for msg in msg_gen:
                # Agent identity is attached by group-chat workers. In the
                # single-agent path attach it before usage estimation so the
                # estimator can find that agent's previous normal stat.
                if not is_group_chat and agent_ids and not getattr(msg, "agent_id", None):
                    msg.agent_id = agent_ids[0]
                usage_estimator.observe(msg)
                collected_messages.append(msg)
                # 单聊：手工设置 agent_id/name（群聊的 _run_one_gen 已设好）
                if msg.role == "usage" and not is_group_chat:
                    msg.agent_id = getattr(msg, "agent_id", None) or (agent_ids[0] if agent_ids else None)
                    try:
                        usage_stat = json.loads(msg.content)
                        usage_stat["model_id"] = request.model_id
                        msg.content = json.dumps(usage_stat, ensure_ascii=False)
                    except (TypeError, ValueError, AttributeError):
                        pass
                if msg.role == "assistant" and not is_group_chat and agent_ids:
                    msg.agent_id = agent_ids[0]
                    if agent_nickname:
                        msg.name = agent_nickname
                # 增量持久化触发点：
                #   usage 消息 → assistant 一轮结束（纯文本回复或 tool_calls 声明），
                #               merge_stream_messages 已将其 flush 为一个完整 turn
                #   tool 消息  → 工具调用结果配对完成 → 立即落盘
                if msg.role == "usage":
                    try:
                        usage_payload = json.loads(msg.content)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        usage_payload = {}
                    if msg.agent_id is not None:
                        usage_payload["agent_id"] = msg.agent_id
                    if msg.name is not None:
                        usage_payload["name"] = msg.name
                    _sse_write(usage_payload, event="usage")
                else:
                    _sse_write(msg.to_dict())
                if msg.role in ("usage", "tool"):
                    _incremental_persist()
                if msg.role == "assistant" and msg.content and msg.content.startswith("Error:"):
                    model_id_for_log = request.model_id if not is_group_chat else (getattr(msg, "agent_id", "group_chat") or "group_chat")
                    logger.error("infer_stream error event | model=%s %s", model_id_for_log, msg.content)

            # 【已移除】尾部发送 session_id 的逻辑，已在第一条消息中发送

            # A model-side failure can terminate after emitting partial output
            # but before provider usage arrives. Close that error round with an
            # estimated usage event so the UI, persistence and summary trigger
            # all observe the same non-zero statistic.
            for usage_msg in usage_estimator.terminal_usage_messages(request.model_id):
                collected_messages.append(usage_msg)
                usage_payload = json.loads(usage_msg.content)
                if usage_msg.agent_id is not None:
                    usage_payload["agent_id"] = usage_msg.agent_id
                _sse_write(usage_payload, event="usage")
                _incremental_persist()

            # The generator can finish after this request has been replaced by a
            # newer inference for the same session. Do not send the old POST's
            # [DONE] before checking generation ownership: ChatPage would treat
            # that as completion even though the replacement is still running.
            if session_id is not None and not self._is_active_stream(session_id, cancel_event):
                return

            # Reconcile file snapshots before announcing completion so a
            # client fetching the journal immediately after [DONE] sees the
            # final workspace state rather than an intermediate tool snapshot.
            self._finalize_file_journal()
            if client_connected:
                try:
                    _write_sse_payload(b"data: [DONE]\n\n")
                except Exception:
                    client_connected = False

            # Check the terminal outcome before final persistence.  Failed or
            # interrupted inference must still append its assistant error turn,
            # but must not trigger title generation as if the round completed
            # successfully.
            has_error = any(
                m.role == "assistant" and m.content and m.content.startswith("Error:")
                for m in collected_messages
            )
            if use_session and self._is_active_stream(session_id, cancel_event):
                persist_exc = conversation_persister.finalize(
                    collected_messages,
                    update_title=not has_error,
                )
                if persist_exc is not None:
                    logger.error("infer_stream: failed to save conversation for session %s: %s", session_id, persist_exc)

            # --- Session Status Stream: broadcast the terminal outcome ---
            if session_id is not None:
                status = "done_error_unread" if has_error else "done_success_unread"
                transition_session_stream_status(session_id, cancel_event, status)
        except (BrokenPipeError, ConnectionResetError):
            cancel_event.set()
            if use_session and self._is_active_stream(session_id, cancel_event):
                from runtime.common import now_iso
                collected_messages.append(Message(role="assistant", timestamp=now_iso(), content="\n\nError: user interrupted."))
                persist_exc = conversation_persister.finalize(
                    collected_messages,
                    update_title=False,
                )
                if persist_exc is not None:
                    logger.error("infer_stream: failed to save aborted conversation for session %s: %s", session_id, persist_exc)
            elif use_session:
                logger.info("infer_stream: skipped stale persist for session %s (BrokenPipe)", session_id)
            # --- Session Status Stream: broadcast "done_error_unread" ---
            if session_id is not None:
                transition_session_stream_status(
                    session_id, cancel_event, "done_error_unread",
                )
        except Exception as exc:
            if use_session and self._is_active_stream(session_id, cancel_event):
                from runtime.common import now_iso
                collected_messages.append(Message(role="assistant", timestamp=now_iso(), content=f"\n\nError: system aborted. ({exc})"))
                persist_exc = conversation_persister.finalize(
                    collected_messages,
                    update_title=False,
                )
                if persist_exc is not None:
                    logger.error("infer_stream: failed to save aborted conversation for session %s: %s", session_id, persist_exc)
            elif use_session:
                logger.info("infer_stream: skipped stale persist for session %s (Exception)", session_id)
            try:
                error_data = json.dumps({"error": str(exc)}, ensure_ascii=False)
                self.wfile.write(f"data: {error_data}\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass
            # --- Session Status Stream: broadcast "done_error_unread" ---
            if session_id is not None:
                transition_session_stream_status(
                    session_id, cancel_event, "done_error_unread",
                )
        finally:
            if session_id is not None:
                finish_session_stream(session_id, owner_event=cancel_event)
            # Only unregister from active_streams if we are still the active stream
            # for this session. A newer inference may have replaced our cancel_event.
            if active_streams is not None and session_id is not None:
                if active_streams.get(session_id) is cancel_event:
                    active_streams.pop(session_id, None)
            if api_inference:
                self._leave_api_inference()
            self._cleanup_thread_local()

    def _is_active_stream(self, session_id: Optional[str], cancel_event: threading.Event) -> bool:
        """Check whether *cancel_event* is still the active stream for *session_id*.

        When a user force-aborts and immediately starts a new inference on the
        same session, the old thread's ``cancel_event`` gets replaced in
        ``active_streams`` by the new thread's event.  Calling this method
        before persisting lets the stale thread detect that it has been
        superseded and skip the write — preventing a lost-update race on
        ``conversation.json``.
        """
        if session_id is None:
            return True
        active_streams = getattr(self.server, "active_streams", None)
        if active_streams is None:
            return True
        return active_streams.get(session_id) is cancel_event

    def _handle_infer_abort(self) -> None:
        """POST /v1/infer/abort — 主动中止指定会话的流式推理。

        请求体: {"session_id": "<session_id>", "forced": true|false}
        找到对应的 cancel_event 并 set()，使推理线程在下一个检查点退出。

        当 forced=true 时，还会主动杀死正在执行的工具进程（exec_cli、MCP），
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
        target_event = active_streams.get(session_id) if active_streams is not None else None
        if target_event is not None:
            target_event.set()

        if forced:
            self._force_abort(session_id, owner_event=target_event)
            self._send_json_response(200, {"ok": True, "forced": True})
        elif active_streams is not None and session_id in active_streams:
            self._send_json_response(200, {"ok": True})
        else:
            # 会话不存在或已结束，视为成功（幂等）
            self._send_json_response(200, {"ok": True, "note": "session not found or already done"})

    def _force_abort(self, session_id: str, owner_event=None) -> None:
        """Kill running tool processes and force the targeted generation done."""
        # 1. Kill any running exec_cli process for this session.
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

        # 3. Force only the aborted generation to done. If a replacement has
        # already taken ownership, this stale abort must not turn it red.
        if owner_event is not None:
            transition_session_stream_status(
                session_id, owner_event, "done_error_unread",
            )

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
                self._send_json_response(200, parsed)
            else:
                self._send_json_error(400, f"Result is not valid JSON: {result}")
                return
        else:
            body = result.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
