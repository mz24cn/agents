"""Inference handler mixin: /v1/infer, /v1/infer/stream, /v1/infer/abort, /v1/tools/call.

Part of the ``_RuntimeRequestHandler`` decomposition in ``runtime.server``.

Zero third-party dependencies — only Python standard library.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import mimetypes
import os
import re
import threading
import urllib.parse
from dataclasses import asdict
from typing import Optional

from runtime.common import (
    clear_request_context,
    decode_forwarded_context,
    FORWARDED_CONTEXT_HEADER,
    get_request_context,
    now_iso,
    set_request_context,
    env_int,
)
from runtime.builtin_tools_coding import _FileJournalManagerHolder
from runtime.models import (
    CLIENT_SIDE_TOOL_TYPE,
    InferenceRequest,
    Message,
    ModelConfig,
    ToolConfig,
)
from runtime.server_state import (
    _session_stream_lock,
    _session_streams,
    answered_client_tool_call_ids,
    begin_session_stream,
    clear_infer_control,
    claim_client_tool_answers,
    close_infer_loop,
    disconnect_session_stream_starter,
    end_paused_session_status,
    finish_session_stream,
    get_infer_exit_reason,
    take_infer_interjections,
    get_terminal_for_session,
    IncrementalConversationPersister,
    InLoopContextCompressor,
    mark_session_stream_persisted,
    merge_stream_messages,
    pending_client_tool_calls,
    publish_session_stream_frame,
    queue_infer_interjection,
    release_client_tool_answers,
    request_pause_infer,
    seed_paused_status,
    set_session_stream_agents,
    stream_batch_is_protocol_complete,
    StreamUsageEstimator,
    take_infer_boundary_action,
    try_close_infer_loop,
    transition_session_stream_status,
)

logger = logging.getLogger("runtime.server")

# 单个引用的可载入上限：超过就不载（推理本来也塞不进上下文）。
_MAX_REMOTE_REF_BYTES = 64 * 1024 * 1024
# 回拉的网络超时（秒）。
_REMOTE_REF_TIMEOUT = 120.0


def _make_remote_ref_hooks(remote_proxy):
    """给远程会话用的两个按需回拉钩子：``(remote_reader, image_resolver)``。

    远程会话里用户引用的实体（粘贴的图片、文件管理器插入的路径、子端工具生成的
    文件）都在**子端**文件系统上，母端本地没有这些字节。两个钩子共用一次请求内
    的字节缓存，同一引用最多回拉一次：

    * ``remote_reader(ref) -> bytes``：展开 ``<file>`` 时读文本，当场内联成代码块。
    * ``image_resolver(ref) -> str``：请求构造时把 ``message.images`` 里的子端引用
      换成 ``data:`` URI（协议层直接吃，见 ``Runtime._drop_history_images``）。

    不在母端落盘、不设 TTL/配额：图片只在真正要发出去的那一次被取一次，历史消息
    的图片本来就被剥掉，需要时再走子端 ``GET /v1/workspace/content`` 拉。持久化里
    保留的是用户写的子端引用，现场不被缓存路径污染。

    Raises:
        ValueError: 引用为空 / 子端读不到 / 超过单引用上限。展开阶段由
            ``handler_infer`` 映射成 400；请求构造阶段作为推理错误上报。
    """
    cache: dict[str, bytes] = {}

    def read(ref: str) -> bytes:
        raw = str(ref or "").strip()
        if not raw:
            raise ValueError("Empty file reference")
        if raw in cache:
            return cache[raw]
        # restrict=0：粘贴目录（/tmp）与任意子端路径都要能读 —— 引用本身就是
        # 用户已经写进消息、授权过的，母端只是替它去取字节。
        query = "path=" + urllib.parse.quote(raw, safe="") + "&restrict=0"
        status, data = remote_proxy.http_bytes(
            f"/v1/workspace/content?{query}", timeout=_REMOTE_REF_TIMEOUT)
        if status != 200:
            detail = data[:200].decode("utf-8", "replace").strip() if data else ""
            raise ValueError(
                f"Referenced file is not readable on the remote environment: {raw}"
                f" (HTTP {status}{'; ' + detail if detail else ''})"
            )
        if len(data) > _MAX_REMOTE_REF_BYTES:
            raise ValueError(
                f"Referenced file is too large to load from the remote environment: {raw}"
                f" ({len(data)} bytes)"
            )
        cache[raw] = data
        return data

    def resolve_image(ref: str) -> str:
        item = str(ref or "")
        # 已经是可直接编码的形式（data URI / http(s)）就原样交给协议层。
        if item.startswith(("data:", "http://", "https://")):
            return item
        raw = item.strip()
        data = read(raw)
        mime = mimetypes.guess_type(raw.replace("\\", "/"))[0] or "image/png"
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    return read, resolve_image


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


def _remote_session_has_terminal(
    remote_proxy,
    session_id: Optional[str],
    is_group_chat: bool,
) -> bool:
    """Whether a remote session already has a live terminal on the child.

    In remote execution the persistent terminal runs in the *child*
    environment, so the parent's local terminal registry has no entry for the
    session (that is why ``_add_exec_cli_for_open_terminal`` cannot be reused
    as-is).  Ask the child's ``GET /v1/terminals`` instead so remote sessions
    get the same "a session with an open terminal exposes exec_cli" behaviour
    as local ones.

    Reaching the child is best-effort: any failure degrades to "no terminal"
    (exec_cli simply stays hidden) rather than breaking inference.
    """
    if is_group_chat or not session_id:
        return False
    try:
        status, data = remote_proxy.http_json("/v1/terminals", timeout=5)
    except Exception:  # noqa: BLE001 - never let the probe break inference
        return False
    if status != 200 or not isinstance(data, dict):
        return False
    for term in data.get("terminals") or []:
        if not isinstance(term, dict):
            continue
        terminal_id = str(term.get("terminal_id") or "")
        term_session = str(term.get("session_id") or "")
        # Child terminals are keyed by ``{session_id}:auto`` and also carry a
        # (possibly ``:``-stripped) session_id, so match either form.
        if terminal_id == session_id or term_session == session_id \
                or terminal_id.startswith(f"{session_id}:"):
            return True
    return False


_stream_batch_is_protocol_complete = stream_batch_is_protocol_complete


class InferSink:
    """Where the inference pipeline's *output bytes* go — nothing else.

    ``_handle_infer_stream`` owns one pipeline: request preparation, session
    registration, cancel events, incremental persistence, title/compression
    finalization and the session status stream.  The only thing that differs
    between ``POST /v1/infer/stream`` and ``POST /v1/infer`` is the transport
    the frames are written to, so that is the single seam this class abstracts.

    Deliberately *not* part of the interface: message selection, persistence
    timing and error semantics.  A sink cannot change pipeline behaviour, which
    is what keeps the two endpoints from drifting apart again.
    """

    #: True when the caller is not an SSE client waiting on this connection:
    #: no bytes are emitted and the starter is never considered disconnected.
    buffered = False

    def begin(self, session_id: Optional[str]) -> None:
        """Emit the response preamble (SSE: headers + close-on-finish)."""

    def write(self, payload: bytes) -> None:
        """Write one already-serialized SSE payload; may raise on disconnect."""

    def heartbeat(self) -> None:
        """Keep the connection from being reaped by idle timeouts."""

    def error(self, message: str) -> None:
        """Report a pipeline-level failure to the caller."""


class SseStreamSink(InferSink):
    """Default sink: stream SSE frames straight to the HTTP connection.

    Reproduces, byte for byte, what ``_handle_infer_stream`` used to write
    directly to ``self.wfile`` before the sink seam existed.
    """

    def __init__(self, handler):
        self._handler = handler

    def begin(self, session_id: Optional[str]) -> None:
        handler = self._handler
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        if session_id is not None:
            handler.send_header("X-Session-Id", session_id)
        handler.end_headers()
        # SSE streams are not keep-alive compatible: close the connection when
        # the stream ends instead of letting the HTTP/1.1 loop read more.
        handler.close_connection = True

    def write(self, payload: bytes) -> None:
        self._handler.wfile.write(payload)
        self._handler.wfile.flush()

    def heartbeat(self) -> None:
        self.write(b": keepalive\n\n")

    def error(self, message: str) -> None:
        self.write(f"data: {json.dumps({'error': message}, ensure_ascii=False)}\n\n".encode("utf-8"))


class BufferedInferSink(InferSink):
    """Sink used by ``POST /v1/infer``: swallow frames, hand back one envelope.

    Frames are dropped because the pipeline already appends every protocol
    message to ``collected``, which is the authoritative list the JSON envelope
    is built from (see :meth:`build_envelope`).  The HTTP response is written by
    the caller after the run, so no SSE headers and no ``close_connection``.

    A buffered caller stays "connected" for the whole run: a plain HTTP client
    gives no way to observe an early disconnect, so the starter is never marked
    disconnected and the inference is never cancelled because nobody was
    watching — matching the historical behaviour of ``/v1/infer``.
    """

    buffered = True

    def __init__(self):
        self.started = False
        self.session_id: Optional[str] = None
        self.collected: list[Message] = []
        self.error_text: Optional[str] = None
        #: Names/ids of this session's client-side tools, used to report the
        #: tool calls the loop handed back instead of executing (see
        #: :func:`runtime.server_state.pending_client_tool_calls`).
        self.client_tool_names: set = set()

    def begin(self, session_id: Optional[str]) -> None:
        self.started = True
        self.session_id = session_id

    def attach(self, *, session_id: Optional[str], collected: list) -> None:
        """Bind the pipeline's own state so the envelope can be rebuilt."""
        self.session_id = session_id
        self.collected = collected

    def attach_client_tools(self, names) -> None:
        """Tell the sink which tools the caller executes for itself."""
        self.client_tool_names = set(names or ())

    def write(self, payload: bytes) -> None:
        return None

    def heartbeat(self) -> None:
        return None

    def error(self, message: str) -> None:
        self.error_text = message

    def build_envelope(self) -> tuple[int, dict]:
        """Assemble the ``/v1/infer`` JSON body from the collected messages.

        Returns ``(http_status, envelope)``.  Same contract as before the sink
        seam: merged ConversationTurns plus ``success`` / ``session_id`` /
        ``stat`` / ``error``, 200 on a clean run and 500 when a round failed.
        ``merge_stream_messages`` is the very same merge the persister uses, so
        the response matches what was written to ``conversation.json``.

        A run that ended on a client-side tool call additionally carries
        ``client_tool_calls``: the unanswered calls (``id`` / ``name`` /
        ``arguments``) the caller executes locally before resuming the session.
        """
        collected = self.collected
        merged_turns, merged_last_stat = merge_stream_messages(collected)
        merged_messages = [
            {k: v for k, v in asdict(turn).items() if v is not None}
            for turn in merged_turns
        ]
        has_error = any(
            m.role == "assistant" and m.content and m.content.startswith("Error:")
            for m in collected
        )
        success = has_error is False and self.error_text is None

        stat_dict = merged_last_stat
        if stat_dict is None:
            for m in reversed(collected):
                if m.role == "usage":
                    try:
                        stat_dict = json.loads(m.content)
                    except (json.JSONDecodeError, ValueError, AttributeError):
                        pass
                    break

        envelope: dict = {"success": success, "messages": merged_messages}
        if self.session_id is not None:
            envelope["session_id"] = self.session_id
        if not success:
            if self.error_text is not None:
                envelope["error"] = f"Inference failed: {self.error_text}"
            else:
                for m in collected:
                    if m.role == "assistant" and m.content and m.content.startswith("Error:"):
                        envelope["error"] = m.content
                        break
        if stat_dict is not None:
            envelope["stat"] = stat_dict
        if self.client_tool_names:
            # A client-side tool call is a *normal* end of the loop, not an
            # error: report the calls the caller must run locally so it can
            # resume this same session with their results.
            handoff = pending_client_tool_calls(merged_turns, self.client_tool_names)
            if handoff:
                envelope["client_tool_calls"] = handoff
        return (200 if success else 500), envelope


def _build_infer_session_extra_meta(body: dict, request: InferenceRequest) -> Optional[dict]:
    """Session ``meta`` fields describing how this inference run was configured.

    ``client_tools`` is the session-scoped half of the client-side tool feature:
    definitions the caller shipped with the request instead of registering them.
    Recording them in the session meta is what makes "visible in this session
    only" survive a follow-up request that omits the field (e.g. the request
    carrying the tool results) — while the global ToolRegistry, and therefore
    ``GET /v1/tools`` and every other session, never learns about them.
    """
    meta: dict = {}
    if body.get("remote_env"):
        meta["remote_env"] = body["remote_env"]
    if request.client_tools:
        meta["client_tools"] = [config.to_dict() for config in request.client_tools]
    elif body.get("client_tools") == []:
        # An explicitly empty list is the caller taking the tools back (the web
        # client sends ``[]`` when the user unticks them).  Skipping the write
        # because it is falsy would leave the old list in the meta, ready to be
        # resurrected by the next request that omits the field.
        meta["client_tools"] = []
    return meta or None


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
        # Resume continues a soft-paused session from its persisted history:
        # same context-restore path as continue, but WITHOUT removing the
        # trailing turn (a paused conversation ends on a complete round —
        # assistant turn + all tool results — waiting to be interpreted).
        is_resume = body.get("resume") is True
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

        # === Remote execution environment binding ===
        # remote_env 是 remote_envs.json 中的环境 id。绑定时，本会话的全部
        # 工具（tool_ids 中的每一个）都转发到子环境执行，不与本地工具混用：
        # 推理仍在母环境进行（母环境模型 + 会话历史），工具清单只取母环境
        # registry（简化设计，前端与模型均不再拉取子端 /v1/tools），工作区、
        # 终端与文件 journal 归属子环境。
        remote_env_id = str(body.get("remote_env") or "").strip() or None
        remote_proxy = None
        if remote_env_id:
            remote_envs_mgr = getattr(self.server, "remote_env_manager", None)
            if remote_envs_mgr is None:
                self._send_json_error(
                    400, "Remote environments are not supported by this server"
                )
                return None
            try:
                # 按环境共享的工具代理（内含子端工具清单 TTL 缓存）：同一环境
                # 的多轮推理 / talk_to / delegate 复用，不再每轮重新拉取 /v1/tools。
                remote_proxy = remote_envs_mgr.get_proxy(
                    remote_env_id,
                    tunnel_manager=getattr(self.server, "tunnel_manager", None),
                )
            except KeyError:
                self._send_json_error(
                    404, f"Remote environment not found: {remote_env_id}"
                )
                return None

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

        # === Retry / resume validation ===
        continued_user_message_timestamp = None
        resume_context: Optional[list] = None
        if is_continue or is_resume:
            if raw_session_id in (None, "new"):
                self._send_json_error(400, "continue/resume requires an existing session_id")
                return None
            if not use_session or session_id is None:
                self._send_json_error(404, f"Session not found: {raw_session_id}")
                return None
            if is_resume:
                # A resume may only target a resting (paused) session. If a
                # generation is still running — e.g. a pause was requested but
                # has not reached its round boundary yet, or another client
                # already resumed — starting a parallel request would trigger
                # the supersede-and-cancel path and destroy the running turn.
                with _session_stream_lock:
                    active = _session_streams.get(session_id)
                    still_streaming = (
                        active is not None
                        and not active.get("done")
                        and not (active.get("cancel_event") is not None and active["cancel_event"].is_set())
                    )
                if still_streaming:
                    self._send_json_error(
                        409,
                        "session is still inference; pause has not taken effect yet "
                        "or the session was already resumed",
                    )
                    return None
                # M3: consume the pause sidecar. Its context snapshot carries
                # the transient loop state that was never persisted as such
                # (skill-disclosure frames, the in-loop compression rebuild,
                # boundary-injected turns); the resume continues from exactly
                # that list. The file is deleted on consumption, so a later
                # restart cannot resurrect a stale paused marker.
                pause_state = context_manager.read_pause_state(session_id)
                if pause_state is not None:
                    context_manager.clear_pause_state(session_id)
                    snapshot = pause_state.get("context") or []
                    if snapshot:
                        resume_context = snapshot
                        body["_resume_sidecar_used"] = True
                    pending_ids = pause_state.get("pending_agent_ids")
                    if isinstance(pending_ids, list) and pending_ids:
                        body["_resume_pending_agent_ids"] = [
                            str(a) for a in pending_ids
                            if isinstance(a, str) and a
                        ]
                    logger.info(
                        "resume: consumed pause sidecar for session %s "
                        "(paused_at=%s, context_turns=%d)",
                        session_id, pause_state.get("paused_at"), len(snapshot),
                    )
            if not is_resume and len(agent_ids) > 1 and not retry_agent_id:
                self._send_json_error(400, "group-chat retry requires retry_agent_id")
                return None
            if not retry_agent_id and not body.get("model_id"):
                self._send_json_error(
                    400, "retry requires retry_agent_id or model_id + tool_ids"
                )
                return None
            removed_message = None
            removed = False
            try:
                # Capture the identity before the atomic rewrite. The removal is
                # later published as a retained control frame so other browsers
                # can apply the same destructive mutation to their live stores.
                turns = context_manager.load_conversation(session_id)
                if turns and turns[-1].role == "assistant":
                    removed_message = turns[-1]
                # Continue/retry resumes the same user turn, so all file
                # operations must retain that turn's journal key rather than
                # falling back to the current retry/tool execution time.
                continued_user_message_timestamp = next(
                    (
                        turn.timestamp
                        for turn in reversed(turns)
                        if turn.role == "user" and turn.timestamp
                    ),
                    None,
                )
                if is_continue:
                    # Continue is an explicit destructive user action. Do not
                    # infer whether the final response is failed or incomplete;
                    # remove exactly one final assistant message because the
                    # request asked us to. A resume never mutates history: the
                    # paused conversation already ends on a complete round.
                    removed = context_manager.remove_trailing_assistant_message(session_id)
            except (OSError, ValueError) as exc:
                self._send_json_error(
                    500, f"Failed to remove trailing assistant message: {exc}"
                )
                return None
            if is_continue:
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
            else:
                logger.info(
                    "resume: continuing paused session %s from intact history; "
                    "agent_ids=%s model_id=%s tool_ids=%s",
                    session_id, agent_ids,
                    body.get("model_id"), body.get("tool_ids"),
                )
            if is_resume and is_group_chat:
                # Group resume: replay exactly the wave the pause cut off.
                # The pause sidecar recorded which agents were about to be
                # dispatched; without it (or if the roster changed) the
                # regular mention routing of this request decides instead.
                replay = [
                    a for a in (body.get("_resume_pending_agent_ids") or [])
                    if a in agent_ids
                ]
                if replay:
                    mentioned_agent_ids = replay

        # 载入的文件实体（<file>）一律展开：本地会话按本地工作区解析；远程会话的
        # 文件在子端（粘贴上传写的是子端 /tmp 或子端工作区），母端本地没有这些
        # 字节，于是**按需回拉**：文本当场读成代码块，图片则把子端引用原样留在
        # message.images 里，字节等到请求构造时再取（见 InferenceRequest.image_resolver）。
        # 占位符里始终保留用户写的原始引用，conversation.json 记录的是真实现场。
        remote_reader = None
        remote_image_resolver = None
        if remote_proxy is not None:
            remote_reader, remote_image_resolver = _make_remote_ref_hooks(remote_proxy)

        original_messages = None
        user_message_timestamp = None
        timestamp_fallback_used = False
        if "messages" in body and not (is_continue or is_resume):
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
                original_messages = expand_workspace_file_refs(
                    original_messages, _get_ws(), remote_reader=remote_reader)
            except ValueError as exc:
                self._send_json_error(400, str(exc))
                return None
        elif is_continue or is_resume:
            # 继续推理（continue）/恢复暂停（resume）：不携带新用户消息，基于会话既有上下文推理。
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
            if is_resume and resume_context:
                # The paused generation continues from its exact in-flight
                # context; re-assembling from disk would drop the transient
                # frames the sidecar exists to preserve.
                assembled_messages = [
                    Message.from_dict(m) for m in resume_context if isinstance(m, dict)
                ]
            else:
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
        has_delegate = "delegate" in tool_ids
        has_talk_to = "talk_to" in tool_ids

        if remote_proxy is None:
            # 本地模式：终端已打开时暴露 exec_cli（推理准备不创建终端）。
            tool_ids = _add_exec_cli_for_open_terminal(
                tool_ids,
                session_id,
                is_group_chat,
            )
        remote_selected: Optional[list] = None
        if remote_proxy is not None:
            # 远程模式（简化设计）：工具清单只取母端 registry，与本地模式
            # 完全一致；选中工具包装为子端转发 callable（子端按母端 tool_id
            # 执行，子端没有该工具时由子端返回错误，模型自行处理）。因此
            # 不再拉取子端 /v1/tools：子端宕机只影响个别工具调用，不阻断
            # 推理请求（不再 502）。
            runtime = self._get_runtime()
            selected_configs = []
            for tid in tool_ids:
                tc = runtime._tool_registry.get(tid)
                if tc is not None:
                    selected_configs.append(tc)
            # 与本地执行保持一致：会话已打开终端时自动暴露 exec_cli。
            # 终端运行在子端，父端本地终端注册表没有该状态，因此改问子端的
            # /v1/terminals；这样非 Web 客户端（API）与恢复后的会话也能拿到
            # exec_cli，而不是只依赖前端按面板状态追加。
            if (
                "exec_cli" not in tool_ids
                and _remote_session_has_terminal(
                    remote_proxy, session_id, is_group_chat
                )
            ):
                tool_ids = [*tool_ids, "exec_cli"]
                tc = runtime._tool_registry.get("exec_cli")
                if tc is not None:
                    selected_configs.append(tc)
            remote_selected = remote_proxy.wrap_parent_configs(selected_configs)
        # Keep the normalized/augmented selection on the request body so retry
        # logging and conversation metadata reflect the actual inference tools.
        body["tool_ids"] = tool_ids

        tool_scope: list = []
        # --- TOOLS 占位符（delegate 专用）---
        if has_delegate:
            runtime = self._get_runtime()
            mcp_by_server: dict[str, list[str]] = {}
            non_mcp_rows: list[tuple[str, str]] = []

            if remote_selected is not None:
                # 远程会话：选中的工具条目已是子端代理条目（不在母端 registry）
                scope_configs = remote_selected
            else:
                scope_configs = []
                for tid in tool_ids:
                    tc = runtime._tool_registry.get(tid)
                    if tc is not None:
                        scope_configs.append(tc)
            for tc in scope_configs:
                tool_scope.append(tc)
                if tc.tool_id == "delegate" and os.environ.get("DISABLE_NESTED_DELEGATE", "false").lower() == "true":
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

        # --- Client-side tools ------------------------------------------------
        # Client-side tools are function tools without their executable entity:
        # the service shows the registration JSON to the model, but the caller
        # runs the tool and answers with the result on the same session.  They
        # reach a session two ways:
        #   * pre-registered as a ``client_side`` row in the ToolRegistry and
        #     selected through ``tool_ids`` like any other tool;
        #   * shipped with this request as ``client_tools`` (array of tool
        #     description JSON).  Those are session-scoped: never registered, so
        #     neither ``GET /v1/tools`` nor any other session sees them.  A later
        #     request of the same session may omit the field — the definitions
        #     persisted in the session meta are then restored, so the model keeps
        #     seeing them for the whole conversation.
        try:
            client_tools = self._resolve_session_client_tools(
                body, context_manager, session_id if use_session else None,
            )
        except ValueError as exc:
            self._send_json_error(400, str(exc))
            return None

        request = InferenceRequest(
            model_id=body["model_id"],
            model_config_override=model_override,
            tool_ids=tool_ids,
            tools=remote_selected,
            client_tools=client_tools,
            messages=assembled_messages,
            text=body.get("text"),
            stream=True,
            max_tool_rounds=body.get("max_tool_rounds") or env_int("MAX_TOOL_ROUNDS", 200),
            image_resolver=remote_image_resolver,
        )

        # One client-side call has exactly one result, whichever client ran the
        # tool.  Arbitrate before this request can start a generation of its
        # own: a session can have several clients that all have the tool, and
        # the first answer back is the one the conversation continues with.
        claimed_answers: list[str] = []
        if use_session and session_id is not None:
            conflicts, claimed_answers = self._claim_client_tool_answers(
                body, request, context_manager, session_id,
            )
            if conflicts:
                logger.info(
                    "infer: refused duplicate client-side answer(s) %s for session %s",
                    ", ".join(conflicts), session_id,
                )
                self._send_json_response(409, {
                    "error": (
                        "Client-side tool call(s) already answered in this session: "
                        f"{', '.join(conflicts)}. A call has exactly one result and "
                        "the first answer back is the one that counts; this session "
                        "has continued with it."
                    ),
                    "code": "client_tool_already_answered",
                    "answered_call_ids": conflicts,
                })
                return None

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
            remote_env=remote_env_id,
            remote_tool_proxy=remote_proxy,
            client_answer_claims=claimed_answers,
        )

        agent_nickname = agent.get("nickname") if agent else None
        return body, request, session_id, use_session, original_messages, context_manager, agent_ids, agent_nickname, body["model_id"], tool_ids, workspace, remote_proxy

    def _resolve_session_client_tools(
        self, body: dict, context_manager, session_id: Optional[str],
    ) -> list:
        """Parse the request's ``client_tools`` into session-scoped ToolConfigs.

        Accepts both ToolConfig-shaped objects and OpenAI function schemas
        (``ToolConfig.from_session_definition``); every entry is forced to
        ``tool_type='client_side'`` so nothing here can execute it.  When the
        field is absent for an existing session, the definitions persisted in
        that session's meta are restored instead: the tools belong to the
        conversation, not to one HTTP request.
        """
        raw = body.get("client_tools")
        if raw is None:
            if session_id is None:
                return []
            try:
                meta = context_manager.get_session_meta(session_id) or {}
            except (OSError, ValueError):
                meta = {}
            raw = meta.get("client_tools")
            if raw is None:
                return []
        if not isinstance(raw, list):
            raise ValueError(
                "client_tools must be an array of client tool description JSON"
            )
        client_tools: list = []
        seen: set = set()
        for position, item in enumerate(raw):
            try:
                config = ToolConfig.from_session_definition(item, position)
            except ValueError as exc:
                raise ValueError(f"Invalid client_tools[{position}]: {exc}") from exc
            for key in (config.name, config.tool_id):
                if key in seen:
                    raise ValueError(
                        f"Duplicate client tool name/tool_id: {key}"
                    )
            seen.update((config.name, config.tool_id))
            client_tools.append(config)
        return client_tools

    def _client_side_tool_names(self, request: InferenceRequest) -> set:
        """Names and tool_ids of every client-side tool visible to a request.

        Covers both registration routes — the session-provided definitions and
        pre-registered ``client_side`` registry rows selected through
        ``tool_ids`` — so the handoff a run produced can be recognised
        afterwards (``pending_client_tool_calls``).
        """
        names: set = set()
        for config in (request.client_tools or []):
            names.update({config.name, config.tool_id})
        registry = self._get_runtime()._tool_registry
        for tool_id in (request.tool_ids or []):
            config = registry.get(tool_id)
            if config is not None and config.tool_type == CLIENT_SIDE_TOOL_TYPE:
                names.update({config.name, config.tool_id, config.tool_name or ""})
        names.discard("")
        return names

    def _claim_client_tool_answers(
        self, body: dict, request: InferenceRequest, context_manager, session_id: str,
    ) -> tuple[list, list]:
        """Take this request's claim on the client-side calls it is answering.

        Returns ``(conflicts, claimed)``: the call ids this request may not
        answer, and - when there are none - the ids it now holds a claim on.
        Exactly one of the two is ever non-empty: a refusal claims nothing, and
        the caller must not release the ids another request is still holding.

        ``tool_type='client_side'`` means this service cannot run the tool, so
        every client of the session that has the tool can answer the same call,
        and exactly one of those answers becomes the conversation's result.  Two
        sources say who was first:

        * ``conversation.json`` - the call already has a ``role=tool`` result, so
          this submission is a late duplicate (another client answered it, or
          this client is retrying a POST that did in fact land);
        * the in-memory claim table - another request of this session is still
          carrying its answer, which the history cannot show until it persists.

        Only answers to calls of this session's client-side tools are considered:
        the service executes every other tool itself, and a caller that resends
        its own history is not answering anything.
        """
        submitted_ids: list[str] = []
        for raw in (body.get("messages") or []):
            if not isinstance(raw, dict) or raw.get("role") != "tool":
                continue
            call_id = raw.get("tool_use_id") or raw.get("tool_call_id")
            if call_id:
                submitted_ids.append(str(call_id))
        if not submitted_ids:
            return [], []
        client_names = self._client_side_tool_names(request)
        if not client_names:
            return [], []
        try:
            history = context_manager.load_conversation(session_id)
        except (OSError, ValueError) as exc:
            # Without the history this request cannot prove it is a duplicate;
            # the claim table below still arbitrates the in-flight case.
            logger.warning(
                "infer: cannot read conversation of session %s to arbitrate "
                "client-side answers: %s", session_id, exc,
            )
            history = []
        settled = answered_client_tool_call_ids(history, client_names)
        conflicts = [call_id for call_id in submitted_ids if call_id in settled]
        if conflicts:
            return conflicts, []
        # The claim table answers with the ids it refused, not with the ids it
        # took -- conflating the two both hides this refusal from the caller and
        # makes the loser's cleanup hand back the *winner's* claim.
        refused = claim_client_tool_answers(session_id, submitted_ids)
        if refused:
            return refused, []
        return [], submitted_ids

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

        # Whatever became of this run, the client-side answers it submitted are
        # no longer this request's to give: give the calls back so they can be
        # answered again.  The conversation itself is what keeps an already
        # answered call answered.
        claims = get_request_context("client_answer_claims")
        if claims:
            release_client_tool_answers(get_request_context("session_id"), claims)

        clear_request_context([
            "sse_callback", "cancel_event", "session_id", "session_dir",
            "user_message_timestamp", "user_message_timestamp_fallback_used",
            "depth", "tool_scope", "available_tool_ids",
            "context_manager", "session_manager", "agent_manager", "workspace",
            "agent_id", "agent_ids", "all_agent_ids", "mentioned_agent_ids",
            "client_answer_claims",
            "file_journal_holder", "file_journal_session_id",
            "file_journal_session_dir", "file_journal_user_message_timestamp",
            "file_journal_timestamp_fallback_used",
            "remote_env", "remote_tool_proxy",
        ])

    def _enter_api_inference(self) -> bool:
        """Begin server-level tracking of a stateless (API) inference.

        Web requests always carry a session_id and are already tracked per
        session via ``active_infers`` and the session status events.  A
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
        """POST /v1/infer — synchronous shell around the streaming pipeline.

        There is deliberately no second inference implementation here: the
        request is handed to :meth:`_handle_infer_stream` with a
        :class:`BufferedInferSink`.  Authorization and validation, session
        creation, the cancel event, ``active_infers`` registration, the session
        status stream, the three-phase incremental persistence, in-loop context
        compression, the file journal and the group-chat path therefore behave
        exactly as they do over SSE.  That is what was missing before: the
        non-streaming copy persisted only after the run finished, so during a
        long non-streaming run — one blocking call spanning hundreds of tool
        rounds — ``conversation.json`` did not exist, ``GET
        /v1/sessions/{id}`` answered 404 "Session not found" and the sidebar
        could not see the session at all.

        The sink throws the frame bytes away; the JSON body is rebuilt from the
        pipeline's own message list with the same ``merge_stream_messages`` the
        persister uses, so the response matches ``conversation.json``.  The
        contract is unchanged (``success`` / ``messages`` / ``session_id`` /
        ``stat`` / ``error``, 200 or 500).

        Consequences of sharing one pipeline, all intended:

        * the session and its first turn are on disk before inference starts;
        * the run registers in ``active_infers`` under its session id, so it
          shows up as "streaming" for the UI, can be watched via
          ``GET /v1/sessions/{id}/stream`` and stopped via ``POST /v1/infer/abort``
          (stop or pause) exactly like a streamed run;
        * ``session_inference_active`` is therefore true while it runs.  Hot
          updates were already refused during a non-streaming call, because
          ``do_POST`` counts every ``/v1/infer`` request in
          ``active_inference_count``; what is new is that the *session* itself is
          now a tracked, interruptable generation rather than an anonymous one;
        * the file journal is flushed by the pipeline's cleanup before this
          method regains control, i.e. before the response is observable.

        A buffered caller is never treated as disconnected: a plain HTTP request
        gives no way to notice the client going away, so the run plays out to the
        end instead of being cancelled for lack of an observer — as ``/v1/infer``
        always behaved.
        """
        sink = BufferedInferSink()
        try:
            self._handle_infer_stream(sink=sink)
        except Exception as exc:
            # ``_handle_infer_stream`` handles failures of the inference itself;
            # reaching here means something blew up while assembling request or
            # session state.  Nothing has been written to the connection yet (a
            # buffered sink emits no headers), so a clean 500 is still possible.
            logger.exception("infer: non-streaming pipeline raised: %s", exc)
            sink.error_text = str(exc)
            if not sink.started:
                self._send_json_error(500, f"Inference failed: {exc}")
                return

        if not sink.started:
            # ``_prepare_infer_request`` rejected the call and already sent the
            # response (400 / 401 / 404 / 409 / 413); writing again would corrupt
            # the keep-alive framing.
            return

        status, envelope = sink.build_envelope()
        self._send_json_response(status, envelope)

    def _handle_infer_stream(self, sink: Optional[InferSink] = None) -> None:
        """POST /v1/infer/stream — execute streaming model inference.

        Returns Server-Sent Events (SSE) stream.
        First event: 'init' containing session_id and user_message_timestamp.
        Subsequent events: Assistant messages.

        ``sink`` abstracts only *where the frame bytes go*.  Called without one
        (the route handler) it writes SSE to this connection exactly as before;
        ``POST /v1/infer`` passes a :class:`BufferedInferSink` to run this same
        pipeline and answer with a single JSON body.  Keep every other decision
        here — persistence timing, cancel events, status stream — endpoint
        agnostic: that is the whole point of the seam.
        """
        result = self._prepare_infer_request()
        if result is None:
            return
        _body, request, session_id, use_session, original_messages, context_manager, agent_ids, agent_nickname, model_id, tool_ids, workspace, remote_proxy = result

        if sink is None:
            sink = SseStreamSink(self)

        api_inference = self._enter_api_inference()
        sink.begin(session_id)

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
        # A buffered (non-streaming) caller builds its JSON envelope from the
        # pipeline's own message list, so hand it the live list object.  It is
        # only ever appended to here, never rebound, so the reference stays
        # valid for the whole run.  The SSE sink ignores this.
        if sink.buffered:
            sink.attach(session_id=session_id, collected=collected_messages)
            # So a non-streaming response can name the tool calls the loop left
            # for the caller instead of executing them.
            attach_client_tools = getattr(sink, "attach_client_tools", None)
            if attach_client_tools is not None:
                attach_client_tools(self._client_side_tool_names(request))
        # A single SSE connection may receive frames from nested-tool worker
        # threads as well as the HTTP handler thread.  Serialize every write so
        # JSON events and heartbeat comments can never interleave.
        sse_write_lock = threading.Lock()
        client_connected = True
        latest_stream_seq = 0
        stream_debug = os.environ.get("SESSION_STREAM_DEBUG", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        if session_id is not None:
            begin_session_stream(session_id, cancel_event)
            if _body.get("resume") is not True:
                # Any generation other than a resume supersedes the paused
                # resting state; its sidecar would be stale context (and a
                # stale durable paused-marker) for any later resume.
                context_manager.clear_pause_state(session_id)
            set_session_stream_agents(session_id, cancel_event, agent_ids)
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
            # Whatever this request *submitted* is invisible to the session's
            # other clients unless it is brokered here: the loop only replays
            # what the inference produced.  Both kinds are published before the
            # "streaming" broadcast, so a browser that attaches mid-run sees
            # them before any assistant/tool frame, in the order
            # conversation.json keeps them.  Use the request body rather than
            # expanded workspace content to preserve exactly what was submitted.
            #   * the user turn: the initiating browser renders it from its own
            #     POST, every other browser only consumes the retained stream;
            #   * a ``role=tool`` answer to a handed-back client-side call
            #     (``tool_type='client_side'``).  The caller ran that tool; this
            #     service never does and has no result frame of its own to
            #     emit, so without this every OTHER client of the session keeps
            #     showing that call as still running -- until its page is
            #     reloaded and conversation.json fills the gap in.  The web UI
            #     is frequently the bystander here while the process driving
            #     the session (e.g. the online debugger) is the one executing.
            # Published to the broker only, never mirrored back to the
            # submitting connection: a client renders what it posted, and one
            # client-side call has exactly one result to display.
            raw_submitted_messages = [
                raw for raw in (_body.get("messages") or [])
                if isinstance(raw, dict) and raw.get("role") in ("user", "tool")
            ]
            for raw_submitted in raw_submitted_messages:
                submitted_frame = dict(raw_submitted)
                if submitted_frame["role"] == "user":
                    if not submitted_frame.get("timestamp") and user_message_ts:
                        submitted_frame["timestamp"] = user_message_ts
                    if mentioned_for_init and not submitted_frame.get("mentions"):
                        submitted_frame["mentions"] = mentioned_for_init
                published_seq = publish_session_stream_frame(
                    session_id, submitted_frame, owner_event=cancel_event,
                )
                if published_seq is not None:
                    latest_stream_seq = published_seq

        # The starter consumes the same retained sequence space as every GET
        # subscriber. Expose the baseline before the first mirrored frame so it
        # can hand off to GET /sessions/{id}/stream without duplicates.
        init_payload["stream_seq"] = latest_stream_seq
        sink.write(f"event: init\ndata: {json.dumps(init_payload, ensure_ascii=False)}\n\n".encode("utf-8"))
        # ================================

        def _write_sse_payload(payload: bytes) -> None:
            with sse_write_lock:
                sink.write(payload)

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
                # Same lock as frame writes: a heartbeat must never interleave
                # with a half-written JSON event from a nested-tool thread.
                with sse_write_lock:
                    sink.heartbeat()
            except Exception as exc:
                _mark_starter_disconnected(exc)

        set_request_context(sse_callback=_sse_write, cancel_event=cancel_event)

        # 注册到 active_infers，使 /v1/infer/abort 可以主动触发中止
        active_infers = getattr(self.server, "active_infers", None)
        if active_infers is not None and session_id is not None:
            active_infers[session_id] = cancel_event

        # --- Session Status Stream: broadcast "streaming" ---
        if session_id is not None:
            transition_session_stream_status(session_id, cancel_event, "streaming")

        usage_tools = []
        for tool_id in request.tool_ids:
            tool = runtime._tool_registry.get(tool_id)
            if tool is not None:
                usage_tools.append(tool)
        usage_estimator = StreamUsageEstimator(
            context_manager=context_manager if use_session else None,
            session_id=session_id if use_session else None,
            original_messages=original_messages,
            # ``request.messages`` is the complete assembled context actually
            # handed to Runtime, unlike ``original_messages`` which normally
            # contains only this HTTP request's new user turn.
            request_messages=request.messages,
            tools=usage_tools,
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
            extra_meta=_build_infer_session_extra_meta(_body, request),
            is_active=lambda: self._is_active_stream(session_id, cancel_event),
            on_incremental_persist=lambda: mark_session_stream_persisted(
                session_id, latest_stream_seq, owner_event=cancel_event,
            ) if session_id is not None else None,
        )

        # Mid-loop compression trigger (90% of the model's ModelConfig.max_context).
        # The tool loop re-sends the whole message list on every round, so a long
        # tool chain can exceed the model's window before this inference finishes
        # — the session-completed trigger only runs afterwards.  Rebuilding the
        # context from conversation.json is only safe while this stream still owns
        # the session and every streamed frame is already persisted (otherwise the
        # pending tool round would be dropped), hence the ``ready`` guard.
        context_compressor = (
            InLoopContextCompressor(
                context_manager=context_manager,
                session_id=session_id,
                ready=lambda: (
                    self._is_active_stream(session_id, cancel_event)
                    and conversation_persister.is_current(collected_messages)
                ),
                inference_model_id=model_id or "",
            )
            if use_session and session_id is not None
            else None
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

            # === Soft-pause gate ===
            # Checked by the tool loop at round boundaries: after a pause is
            # requested (POST /v1/infer/abort {action: "pause"}), the generator
            # stops before the next model API call. The conversation persisted
            # so far is complete, so a later resume continues from it exactly
            # like a normal next round. Group chat answers the same contract at
            # its wave boundary instead (see _group_boundary_gate below).
            def _pause_gate() -> Optional[dict]:
                if session_id is None:
                    return None
                return take_infer_boundary_action(session_id, cancel_event)

            # Loop-exit gate. Asked when the tool loop would end (the last
            # model round produced no tool call). A user message that landed
            # while that round was streaming means the turn is not over, so the
            # answer must not be the last word: the gate returns False and the
            # same generation runs another round, which picks the message up at
            # its round boundary. With nothing buffered it returns True and the
            # generation is closed to further interjections — a later one is
            # refused so the client re-sends it as a normal turn instead of
            # having it accepted into a queue nothing will ever drain.
            def _loop_close_gate() -> bool:
                if session_id is None:
                    return True
                return try_close_infer_loop(session_id, cancel_event)

            # Group-chat boundary gates, both consumed at every wave boundary
            # (previous wave fully settled, next wave not yet dispatched).
            # boundary_gate implements the single-agent round-boundary
            # contract: interjections join the shared transcript AND a soft
            # pause ends the generation before the next wave is dispatched
            # (coarse group semantics — the running wave always finishes
            # first). interject_gate stays interjection-only and drives the
            # trailing acceptance after the wave loop, where a pause must
            # never be honoured (natural completion wins the race).
            def _group_boundary_gate() -> Optional[dict]:
                if session_id is None:
                    return None
                return take_infer_boundary_action(session_id, cancel_event)

            def _group_interject_gate() -> list:
                if session_id is None:
                    return []
                return take_infer_interjections(session_id, cancel_event)

            # Group-chat loop-exit gate. A wave loop cannot continue on a lone
            # interjection (waves are scheduled by @-mentions alone), so the
            # only correct answer at its end is to close the generation: what
            # was accepted in time still joins the shared transcript, anything
            # later is refused and re-sent as a normal turn that does schedule
            # agents.
            def _group_close_gate() -> bool:
                if session_id is None:
                    return True
                close_infer_loop(session_id, cancel_event)
                return True

            # === 统一消息源 ===
            # Transient snapshot captured by the tool loop when it ends at a
            # pause boundary (see Runtime.infer_stream.exit_state); persisted
            # as the session's resume_state.json sidecar at finalization.
            pause_exit_state: dict = {}
            if is_group_chat:
                from runtime.group_chat import run_group_chat_stream_gen
                # Resume of a soft-paused group generation: the sidecar
                # snapshot already IS the shared transcript (request.messages
                # was built from it), and the wave the pause cut off is
                # replayed via mentioned_agent_ids (set in _prepare).
                _group_initial_turns = (
                    list(request.messages or [])
                    if _body.get("resume") is True and _body.get("_resume_sidecar_used")
                    else None
                )
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
                    # Rebuilding an agent's context mid-loop reads the session
                    # from disk: only safe while this stream still owns the
                    # session and its frames are already persisted (same rule as
                    # the single-agent compressor below).
                    session_ready=(
                        (
                            self._is_active_stream(session_id, cancel_event)
                            and conversation_persister.is_current(collected_messages)
                        ) if session_id is not None else None
                    ),
                    interject_gate=_group_interject_gate,
                    boundary_gate=_group_boundary_gate,
                    close_gate=_group_close_gate,
                    exit_state=pause_exit_state,
                    initial_turns=_group_initial_turns,
                )
            else:
                msg_gen = runtime.infer_stream(
                    request,
                    cancel_event=cancel_event,
                    on_round_complete=context_compressor,
                    pause_gate=_pause_gate,
                    close_gate=_loop_close_gate,
                    on_context_overflow=(
                        context_compressor.ensure_within_budget
                        if context_compressor is not None else None
                    ),
                    exit_state=pause_exit_state,
                )

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

            # The message source is exhausted: this generation's loop is over.
            # Close it to interjections *before* the finalization below (a
            # context compression there can take seconds), so a message that
            # arrives in that window is refused and re-sent as a normal turn
            # rather than accepted into a buffer that no boundary will drain.
            if session_id is not None:
                close_infer_loop(session_id, cancel_event)

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

            # Client-side tool handoff: this run stopped on tool call(s) the
            # service must not execute (``tool_type='client_side'``).  Say so
            # explicitly — a streaming caller can otherwise only infer it from
            # the tool names it declared — and do it before [DONE] so the frame
            # is part of this response, not a race after the stream closed.
            try:
                _client_names = self._client_side_tool_names(request)
            except Exception:  # noqa: BLE001 - never break the stream over this
                _client_names = set()
            if _client_names:
                client_handoff = pending_client_tool_calls(
                    [m.to_dict() for m in collected_messages if hasattr(m, "to_dict")],
                    _client_names,
                )
                if client_handoff and client_connected:
                    _sse_write({
                        "role": "status",
                        "source": "system",
                        "status": "client_tool_calls",
                        "client_tool_calls": client_handoff,
                    })

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
            # A soft pause stopped the generator at a completed round boundary:
            # the conversation is intact and persisted, but the outcome is a
            # resting "paused" state, not a finished answer. No title
            # generation on this path (the aligned decision: 暂停收尾不更新标题).
            paused_exit = (
                session_id is not None
                and get_infer_exit_reason(session_id, cancel_event) == "paused"
            )
            if use_session and self._is_active_stream(session_id, cancel_event):
                persist_exc = conversation_persister.finalize(
                    collected_messages,
                    update_title=not has_error and not paused_exit,
                )
                if persist_exc is not None:
                    logger.error("infer_stream: failed to save conversation for session %s: %s", session_id, persist_exc)

            if (
                paused_exit
                and use_session
                and session_id is not None
                and self._is_active_stream(session_id, cancel_event)
            ):
                # M3: make the resting pause durable. The sidecar is both the
                # marker the server re-seeds into the status registry after a
                # restart and the transient-context snapshot a resume keeps.
                try:
                    from runtime.common import now_iso as _pause_now_iso
                    context_manager.write_pause_state(session_id, {
                        "version": context_manager.PAUSE_STATE_VERSION,
                        "kind": "infer_pause",
                        "session_id": session_id,
                        "paused_at": _pause_now_iso(),
                        "model_id": model_id or "",
                        "agent_ids": list(agent_ids or []),
                        "tool_ids": list(tool_ids or []),
                        # Group pause only: the wave that was about to be
                        # dispatched. A resume replays it verbatim instead of
                        # re-routing mentions it has no way to reconstruct.
                        "pending_agent_ids": list(
                            pause_exit_state.get("pending_agent_ids") or []
                        ),
                        "context": pause_exit_state.get("messages") or [],
                    })
                except OSError as exc:
                    logger.error(
                        "infer_stream: failed to write pause sidecar for session %s: %s",
                        session_id, exc,
                    )

            # --- Session Status Stream: broadcast the terminal outcome ---
            if session_id is not None:
                if paused_exit:
                    status = "paused"
                elif has_error:
                    status = "done_error_unread"
                else:
                    status = "done_success_unread"
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
                sink.error(str(exc))
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
            # Only unregister from active_infers if we are still the active stream
            # for this session. A newer inference may have replaced our cancel_event.
            if active_infers is not None and session_id is not None:
                if active_infers.get(session_id) is cancel_event:
                    active_infers.pop(session_id, None)
            if api_inference:
                self._leave_api_inference()
            self._cleanup_thread_local()

    def _is_active_stream(self, session_id: Optional[str], cancel_event: threading.Event) -> bool:
        """Check whether *cancel_event* is still the active stream for *session_id*.

        When a user force-aborts and immediately starts a new inference on the
        same session, the old thread's ``cancel_event`` gets replaced in
        ``active_infers`` by the new thread's event.  Calling this method
        before persisting lets the stale thread detect that it has been
        superseded and skip the write — preventing a lost-update race on
        ``conversation.json``.
        """
        if session_id is None:
            return True
        active_infers = getattr(self.server, "active_infers", None)
        if active_infers is None:
            return True
        return active_infers.get(session_id) is cancel_event

    def _recognize_pause_from_disk(self, session_id: str) -> bool:
        """惰性识别跨重启的暂停（替代启动全量扫描）。

        resume_state.json sidecar 只在会话**被使用时**逐点检查：UI 打开会话
        （pause-status）、end 收敛（abort action=end）、恢复（resume 直接读
        sidecar）。发现有效 sidecar 就把内存中的 paused 状态种回来，▶ 按钮 /
        侧边栏标记 / end 收敛在升级 agentsvc 后行为不变。
        """
        if not session_id:
            return False
        cm = getattr(self.server, "context_manager", None)
        if cm is None:
            return False
        try:
            sidecar = cm.read_pause_state(session_id)
        except Exception:
            return False
        if not sidecar:
            return False
        return seed_paused_status(session_id, sidecar.get("agent_ids") or [])

    def _handle_infer_pause_status(self) -> None:
        """GET /v1/infer/pause-status?session_id=... — 单会话 paused 检查。

        浏览器打开会话时调用，用于识别跨越 agentsvc 重启的暂停；代价是每
        次 O(1) 的单文件读取，与服务里有多少会话无关。副作用：把内存 paused
        状态种回并广播（与天然暂停收尾同一条事件通道）。
        """
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        session_id = (params.get("session_id") or [""])[0]
        if not session_id:
            self._send_json_error(400, "Missing required query param: session_id")
            return
        paused = self._recognize_pause_from_disk(session_id)
        self._send_json_response(200, {"session_id": session_id, "paused": paused})

    def _handle_infer_abort(self) -> None:
        """POST /v1/infer/abort — 主动中止/暂停指定会话的流式推理。

        请求体: {"session_id": "<session_id>", "action": "stop"|"force"|"pause"|"end"}
        （兼容旧版: {"session_id": ..., "forced": true|false}，无 action 字段时
        forced=true 等价 "force"，否则等价 "stop"。）

        - stop:  找到对应的 cancel_event 并 set()，推理线程在下一个检查点退出。
        - force: 还会主动杀死正在执行的工具进程（exec_cli、MCP），并强制把会话
                 状态标记为 done_error_unread，适用于工具调用卡死的场景。
        - pause: 软暂停。不打断当前模型轮，也不杀任何工具进程；推理循环在下一个
                 轮边界（上一轮 assistant+tool 结果已完整增量落盘之后、下一次
                 build_request 之前）干净结束本次 generation，会话状态广播
                 "paused"。历史保留在 conversation.json，后续恢复 = 一次普通的
                 续推请求（POST /v1/infer/stream 携带 resume: true）。
        - end:   结束被暂停的会话（不恢复）。仅当会话处于 paused 且没有活跃
                 generation 时生效：状态收敛为 done_success_unread 并广播；
                 其余情况幂等 no-op（ended=false）。
        """
        body = self._read_json_body()
        if body is None:
            return
        session_id = body.get("session_id")
        if not session_id:
            self._send_json_error(400, "Missing required field: session_id")
            return
        action = body.get("action")
        if action not in ("stop", "force", "pause", "end"):
            action = "force" if body.get("forced", False) is True else "stop"

        active_infers = getattr(self.server, "active_infers", None)
        target_event = active_infers.get(session_id) if active_infers is not None else None

        if action == "end":
            # 结束被暂停的会话：不恢复、不新增任何轮次，把 resting 的 paused 状态
            # 正式收敛为 done_success_unread（历史在暂停点已完整落盘）。会话正在
            # 推理或状态并非 paused 时是 no-op（幂等）。
            # 重启后内存状态为空：先用 sidecar 惰性识别（O(1) 单文件），再收敛。
            self._recognize_pause_from_disk(session_id)
            ended = end_paused_session_status(session_id)
            # Abandoning the pause also discards the durable sidecar; keeping
            # it would let a later restart resurrect the paused marker.
            try:
                self.server.context_manager.clear_pause_state(session_id)  # type: ignore[attr-defined]
            except Exception:
                pass
            self._send_json_response(200, {"ok": True, "action": "end", "ended": ended})
            return

        if action == "pause":
            effective = False
            if target_event is not None:
                effective = request_pause_infer(session_id, target_event)
            # 幂等：无活动流/已被替换时 effective=False，前端按普通空闲会话处理。
            self._send_json_response(200, {"ok": True, "action": "pause", "effective": effective})
            return

        if target_event is not None:
            # abort 永远赢：先撤掉尚未消费的暂停标志，再触发 cancel。
            clear_infer_control(session_id, target_event)
            target_event.set()

        if action == "force":
            self._force_abort(session_id, owner_event=target_event)
            self._send_json_response(200, {"ok": True, "forced": True})
        elif active_infers is not None and session_id in active_infers:
            self._send_json_response(200, {"ok": True})
        else:
            # 会话不存在或已结束，视为成功（幂等）
            self._send_json_response(200, {"ok": True, "note": "session not found or already done"})

    def _handle_infer_interject(self) -> None:
        """POST /v1/infer/interject — 流式推理途中追加一条用户消息。

        请求体: {"session_id": ..., "message": {"role":"user","content":...},
                 "client_message_id": 可选去重id}

        消息只进入当前 generation 的内存缓冲（不落盘、不新起推理），并在
        下一个轮边界（本轮所有工具调用完成之后、下一次模型请求之前）被消费：
        追加进推理循环的消息列表、作为 user 帧发布到会话流、随后由同一
        generation 继续推理 —— 即"同代续跑"，符合"实时插入"的原有语义。

        - 推理活跃 → 202 {"status": "queued"}（队列上限 10，超出 429）
        - 推理已结束/不存在 → 200 {"status": "not_active"}，由前端降级为
          普通发送（基于完整历史的新请求）。
        - 「已结束」判定与循环收尾是同一个原子决定（``try_close_infer_loop``）：
          模型最后一轮没有工具调用、正要退出循环的那一刻，若队列里已有消息，
          循环不允许结束，会带着它再跑一轮；只有队列为空时循环才真正收尾，并把
          generation 关闭，此后的插话一律 not_active 退回，不会被永久缓存。
          群聊的波次循环无法靠一条孤立的插话续跑，因此收尾时先关闭队列，再把
          已经接受的插话写入共享历史。
        - 群聊会话同样支持（粗粒度语义）：插话只在 wave 边界进入共享历史，已在执行的 agent 看不到它，
          之后的每一轮发言都能看到；插话只改上下文，不改变 agent 调度（调度仍由 @mention 决定）。
        - stop/force abort 会清空未消费的缓冲（abort 永远赢）；pause 与
          interject 同到达时插入优先（缓冲非空则不 stop）。
        """
        body = self._read_json_body()
        if body is None:
            return
        session_id = body.get("session_id")
        if not session_id:
            self._send_json_error(400, "Missing required field: session_id")
            return
        message = body.get("message")
        if not isinstance(message, dict) or message.get("role") != "user":
            self._send_json_error(400, "Field 'message' must be a user message object")
            return
        content = message.get("content")
        if isinstance(content, str):
            content = content.strip()
        if not content:
            self._send_json_error(400, "Field 'message.content' must be non-empty")
            return
        message = dict(message)
        message["content"] = content
        message.setdefault("timestamp", now_iso())
        client_message_id = body.get("client_message_id")
        if client_message_id:
            message["client_message_id"] = str(client_message_id)

        active_infers = getattr(self.server, "active_infers", None)
        target_event = active_infers.get(session_id) if active_infers is not None else None
        if target_event is None or target_event.is_set():
            self._send_json_response(200, {"ok": True, "status": "not_active", "session_id": session_id})
            return

        outcome = queue_infer_interjection(session_id, target_event, message)
        if outcome == "queued":
            # Show the turn immediately in every browser attached to the
            # session stream. The boundary drain re-publishes the same
            # timestamp, and ChatPage suppresses the duplicate on it.
            publish_session_stream_frame(session_id, dict(message), owner_event=target_event)
            self._send_json_response(202, {"ok": True, "status": "queued", "session_id": session_id})
        elif outcome == "full":
            self._send_json_error(429, "interjection queue is full (max 10)")
        else:
            self._send_json_response(200, {"ok": True, "status": "not_active", "session_id": session_id})

    def _force_abort(self, session_id: str, owner_event=None) -> None:
        """Kill running tool processes and force the targeted generation done."""
        # 1. Kill any running exec_cli process for this session.
        try:
            from runtime.builtin_tools import kill_active_process
            kill_active_process(session_id)
        except Exception as exc:
            logger.error("force_abort: kill_active_process failed for %s: %s", session_id, exc)

        # 1b. Interrupt exec_cli calls on the session's persistent terminal.
        # No hook is registered for the user-abort path, and an abandoned
        # collector would keep consuming that terminal's output, so every
        # later exec_cli call of the session would time out.  Without a
        # tool_use_id this stops all in-flight calls of the session and sends
        # Ctrl-C, which also frees the shell itself.
        try:
            from runtime.builtin_tools import cancel_running_command
            cancel_running_command(session_id)
        except Exception as exc:
            logger.error("force_abort: cancel_running_command failed for %s: %s", session_id, exc)

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

        Remote-execution context: the parent's remote tool proxy forwards
        the portable subset of its request context (workspace /
        session_id / user_message_timestamp / depth / agent_id / agent_ids
        / all_agent_ids / model_id / available_tool_ids) as one base64url
        JSON value in the ``X-Agents-Request-Context`` header (a sibling
        of the auth header; the JSON payload stays a pure tool-call
        contract). This handler decodes it and rebuilds the calling
        session's context on THIS host: the workspace pins exec_shell cwd,
        relative file paths and the PTY terminal; session_id drives the
        file journal, terminal and delegate sub-session ids; the agent
        fields drive talk_to / delegate. Host-specific values (session_dir,
        the server singletons, the file-journal holder) are rebuilt here
        from this server, never forwarded. The session context comes
        *only* from the header: direct callers of this endpoint (curl /
        MCP adapters) pass just ``tool_id`` + ``arguments`` and run against
        the child's default workspace.

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

        # ---- Remote-execution request context ------------------------
        # The whole session context travels in one transport-layer header
        # written by the parent's remote tool proxy; the JSON body carries
        # only the tool call itself (tool_id + arguments).
        fwd = decode_forwarded_context(self.headers.get(FORWARDED_CONTEXT_HEADER))

        # "base64": "auto" -- direct callers (web tool-call test) opt into
        # the inference-loop base64 pre/post processing around the call.
        base64_auto = fwd.get("base64") == "auto"

        # Workspace: pin only when the directory exists on *this* host. A
        # stale parent-side path (or a temporarily unreachable child) falls
        # back to the child default workspace instead of failing Popen / writes.
        workspace = str(fwd.get("workspace") or "").strip()
        workspace_applied = False
        if workspace:
            if os.path.isdir(workspace):
                set_request_context(workspace=workspace)
                workspace_applied = True
            else:
                logger.warning(
                    "tools/call: forwarded workspace %s not found on this "
                    "host; falling back to the child default workspace",
                    workspace,
                )

        session_id = fwd.get("session_id") or None
        user_message_timestamp = fwd.get("user_message_timestamp") or None

        runtime = self._get_runtime()
        # Everything set for a session-scoped call, cleared afterwards.
        session_keys = [
            "session_id", "session_dir", "user_message_timestamp",
            "user_message_timestamp_fallback_used", "file_journal_holder",
            "file_journal_manager", "context_manager", "session_manager",
            "agent_manager", "depth", "agent_id", "agent_ids",
            "all_agent_ids", "model_id", "tool_scope", "available_tool_ids",
        ]
        try:
            if session_id:
                # 远程代理调用（母环境转发）：带上前向的会话上下文，使本端（子端）的
                # 文件 journal / 终端 / delegate 子会话与母端会话同 id；host 相关的
                # session_dir 与服务器单例在此按子端自身重建。
                context_manager = self.server.context_manager  # type: ignore[attr-defined]
                session_dir = os.path.dirname(context_manager._conversation_path(session_id))
                # 依前向的工具 id 重建 tool_scope，使 delegate / talk_to 在本端
                # registry 上解析子工具（子代理仍在子端执行）。
                scope = []
                registry = getattr(runtime, "_tool_registry", None)
                for tid in fwd.get("available_tool_ids") or []:
                    tc = registry.get(tid) if registry is not None else None
                    if tc is not None:
                        scope.append(tc)
                set_request_context(
                    session_id=session_id,
                    session_dir=session_dir,
                    user_message_timestamp=user_message_timestamp,
                    user_message_timestamp_fallback_used=bool(
                        fwd.get("user_message_timestamp_fallback_used")),
                    file_journal_holder=_FileJournalManagerHolder(),
                    file_journal_manager=None,
                    context_manager=context_manager,
                    session_manager=self.server.session_manager,  # type: ignore[attr-defined]
                    agent_manager=self.server.agent_manager,  # type: ignore[attr-defined]
                    depth=fwd.get("depth") or 0,
                    agent_id=fwd.get("agent_id"),
                    agent_ids=fwd.get("agent_ids"),
                    all_agent_ids=fwd.get("all_agent_ids"),
                    model_id=fwd.get("model_id"),
                    tool_scope=scope,
                    available_tool_ids=fwd.get("available_tool_ids"),
                )
                try:
                    result = runtime.call_tool(tool_id, arguments, base64_auto=base64_auto)
                finally:
                    # 与推理路径一致：响应前先 finalize 本轮文件 journal
                    self._finalize_file_journal()
                    clear_request_context(session_keys)
            else:
                result = runtime.call_tool(tool_id, arguments, base64_auto=base64_auto)
        finally:
            if workspace_applied:
                clear_request_context(["workspace"])

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
