"""Shared in-memory state and module-level helpers for the runtime HTTP server.

Holds the session/terminal registries, the SSE broadcast helpers and the
module-level functions (conversation persistence, terminal session management,
dynamic function loading) that are shared between ``runtime.server`` and the
domain-specific handler mixins (``runtime.handler_*``).

Moved verbatim from ``runtime.server`` so the handler mixins can import them
without creating a circular dependency. ``runtime.server`` re-exports every
public name so ``from runtime.server import ...`` keeps working.

Zero third-party dependencies — only Python standard library.
"""

import importlib.util
import json
import logging
import os
import select
import signal
import struct
import sys
import threading
import time
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

from runtime.common import get_system_encoding, SYSTEM_ENCODING
from runtime.context_manager import ConversationTurn

logger = logging.getLogger("runtime.server")


def _safe_token_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _message_token_chars(message) -> int:
    """Estimate token contribution using the requested one-character rule."""
    total = len(getattr(message, "content", "") or "")
    total += len(getattr(message, "thinking", "") or "")
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        try:
            total += len(json.dumps(tool_calls, ensure_ascii=False))
        except (TypeError, ValueError):
            total += len(str(tool_calls))
    return total


class StreamUsageEstimator:
    """Normalize provider usage and synthesize stats for interrupted rounds.

    A provider-reported usage object is authoritative, including an explicit
    0/0. When usage was not reported, estimate this round from the previous
    normal inference plus newly observed input/output characters (1 char = 1
    token), and mark the result with ``estimated=true``.
    """

    def __init__(self, context_manager=None, session_id=None, original_messages=None):
        self._base_by_agent: dict[str, dict] = {}
        self._pending_input_by_agent: dict[str, int] = {}
        self._output_by_agent: dict[str, int] = {}
        self._open_by_agent: dict[str, bool] = {}
        self._error_by_agent: dict[str, bool] = {}
        self._model_by_agent: dict[str, Optional[str]] = {}
        self._default_input_chars = sum(
            _message_token_chars(message) for message in (original_messages or [])
        )

        turns = []
        if context_manager is not None and session_id:
            try:
                turns = context_manager.load_conversation(session_id)
            except (FileNotFoundError, OSError, ValueError):
                turns = []
        for turn in turns:
            if getattr(turn, "role", None) != "assistant":
                continue
            stat = getattr(turn, "stat", None)
            if not isinstance(stat, dict) or stat.get("estimated") is True:
                continue
            if stat.get("usage_reported") is False:
                continue
            key = getattr(turn, "agent_id", None) or ""
            self._base_by_agent[key] = stat

    @staticmethod
    def _agent_key(message) -> str:
        return (
            getattr(message, "agent_id", None)
            or getattr(message, "assistant_id", None)
            or ""
        )

    def _base(self, key: str) -> dict:
        return self._base_by_agent.get(key) or self._base_by_agent.get("") or {}

    def _ensure_agent(self, key: str) -> None:
        self._pending_input_by_agent.setdefault(key, self._default_input_chars)
        self._output_by_agent.setdefault(key, 0)
        self._open_by_agent.setdefault(key, False)
        self._error_by_agent.setdefault(key, False)

    def _estimate(self, key: str, stat: Optional[dict] = None) -> dict:
        source = dict(stat or {})
        base = self._base(key)
        prompt = _safe_token_int(base.get("prompt_tokens")) + self._pending_input_by_agent.get(key, 0)
        completion = _safe_token_int(base.get("completion_tokens")) + self._output_by_agent.get(key, 0)
        source.update({
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "total_prompt_tokens": prompt,
            "total_completion_tokens": completion,
            "total_all_tokens": prompt + completion,
            "estimated": True,
            "usage_reported": False,
        })
        # Provider usage was absent, so cache counters are unknown even for
        # protocols that normally report numeric cache fields.
        source["cached_input_tokens"] = None
        source["new_token_cache"] = None
        source.pop("model_id", None)
        return source

    def observe(self, message) -> None:
        """Mutate an unreported usage message into an estimate, if necessary."""
        key = self._agent_key(message)
        self._ensure_agent(key)
        role = getattr(message, "role", None)
        if role == "assistant":
            self._open_by_agent[key] = True
            content = getattr(message, "content", "") or ""
            if content.startswith("Error:") or content.lstrip().startswith("Error:"):
                self._error_by_agent[key] = True
            else:
                self._output_by_agent[key] += _message_token_chars(message)
            return
        if role == "tool":
            self._pending_input_by_agent[key] = (
                self._pending_input_by_agent.get(key, self._default_input_chars)
                + _message_token_chars(message)
            )
            return
        if role != "usage":
            return

        try:
            stat = json.loads(getattr(message, "content", "") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            stat = {}
        if stat.get("usage_reported") is False:
            stat = self._estimate(key, stat)
            message.content = json.dumps(stat, ensure_ascii=False)
        else:
            self._base_by_agent[key] = stat
        if stat.get("model_id"):
            self._model_by_agent[key] = stat.get("model_id")
        self._pending_input_by_agent[key] = 0
        self._output_by_agent[key] = 0
        self._open_by_agent[key] = False
        self._error_by_agent[key] = False

    def terminal_usage_messages(self, default_model_id: Optional[str] = None) -> list:
        """Return synthetic usage messages for error rounds that never closed."""
        from runtime.models import Message
        from runtime.common import now_iso

        messages = []
        for key, is_open in list(self._open_by_agent.items()):
            if not is_open or not self._error_by_agent.get(key):
                continue
            stat = self._estimate(key)
            stat["completed_at"] = now_iso()
            usage = Message(
                role="usage",
                name="round",
                timestamp=now_iso(),
                agent_id=key or None,
                content=json.dumps(stat, ensure_ascii=False),
            )
            # The estimate is already normalized. Close the local round state
            # directly instead of feeding it through observe() again (which
            # would intentionally re-estimate usage_reported=false payloads).
            self._pending_input_by_agent[key] = 0
            self._output_by_agent[key] = 0
            self._open_by_agent[key] = False
            self._error_by_agent[key] = False
            normalized = json.loads(usage.content)
            model_id = self._model_by_agent.get(key) or default_model_id
            if model_id:
                normalized["model_id"] = model_id
            usage.content = json.dumps(normalized, ensure_ascii=False)
            messages.append(usage)
        return messages


# ---------------------------------------------------------------------------
# Conversation formatting helpers
# ---------------------------------------------------------------------------

def _merge_single_agent_stream_messages(
    stream_messages: list,
) -> tuple[list, Optional[dict]]:
    """将单个 Agent 的流式推理消息合并为 ConversationTurn 列表。

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
    # Agent identity tracking — carried over from stream Message objects
    current_agent_id: Optional[str] = None
    current_name: Optional[str] = None
    current_started_at: Optional[str] = None
    # @-mention tracking: the last assistant chunk carrying mentions wins
    # (group-chat aggregated assistant messages carry the full mentions list).
    current_mentions: Optional[list] = None

    def _flush_assistant(stat=None):
        nonlocal assistant_text_buf, assistant_thinking_buf, pending_tool_calls
        nonlocal current_agent_id, current_name, current_started_at, current_mentions
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
                agent_id=current_agent_id,
                name=current_name,
                mentions=current_mentions,
                started_at=(
                    current_started_at
                    or (stat.get("request_started_at") if stat else None)
                ),
            ))
            assistant_text_buf = ""
            assistant_thinking_buf = ""
            pending_tool_calls = []
            current_agent_id = None
            current_name = None
            current_started_at = None
            current_mentions = None

    current_stat: Optional[dict] = None

    for m in stream_messages:
        if m.role == "usage":
            try:
                current_stat = _json.loads(m.content)
                usage_agent_id = getattr(m, "agent_id", None) or getattr(m, "assistant_id", None)
                if usage_agent_id and not current_agent_id:
                    current_agent_id = usage_agent_id
                last_stat = current_stat
            except (ValueError, AttributeError):
                pass
            # stat arrives BEFORE tool messages in infer_stream,
            # flush the assistant turn now so it has stat attached
            _flush_assistant(stat=current_stat)
            current_stat = None
            continue
        if m.role == "assistant":
            # Track agent identity from the first assistant chunk that carries it
            if getattr(m, "agent_id", None) and not current_agent_id:
                current_agent_id = m.agent_id
            elif getattr(m, "assistant_id", None) and not current_agent_id:
                current_agent_id = m.assistant_id
            if getattr(m, "name", None) and not current_name:
                current_name = m.name
            if getattr(m, "started_at", None) and not current_started_at:
                current_started_at = m.started_at
            if getattr(m, "mentions", None):
                current_mentions = m.mentions
            if getattr(m, "tool_calls_dropped", False):
                # Max tool-call rounds reached: the tool_calls in this round were
                # never executed.  Drop the accumulated (already-streamed) tool_call
                # deltas so the flushed assistant turn has no dangling tool_calls —
                # otherwise OpenAI/Anthropic reject the next request.
                pending_tool_calls = []
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
            # tool result 到来时，直接 append（assistant 已经在 stat 到来时 flush 了）。
            # 加固：若 pending assistant 尚未 flush（如异常分支中 tool 消息先于 stat
            # 到达），先 flush 再 append，避免落盘成 [tool, assistant] 的非法顺序。
            if assistant_text_buf or assistant_thinking_buf or pending_tool_calls:
                _flush_assistant(stat=current_stat or last_stat)
            ts = m.timestamp if m.timestamp else now_iso()
            turns.append(ConversationTurn(
                role="tool",
                content=m.content or "",
                timestamp=ts,
                name=m.name or "",
                tool_id=getattr(m, "tool_id", None),
                tool_use_id=getattr(m, "tool_use_id", None),
                agent_id=getattr(m, "agent_id", None),
                started_at=getattr(m, "started_at", None),
            ))
        # skip system deltas

    # Flush 剩余 assistant 内容（最后一条，无 tool calls）
    _flush_assistant(stat=current_stat or last_stat)

    return turns, last_stat


def merge_stream_messages(stream_messages: list) -> tuple[list, Optional[dict]]:
    """Merge streamed messages without mixing concurrent agent buffers.

    Group-chat workers publish into one queue, so raw frames can arrive as
    ``A-token, B-token, A-token, B-usage``.  The single-agent merger owns one
    assistant/tool-call buffer and would concatenate that interleaving under
    whichever agent arrived first.  Partition true multi-agent streams by
    ``agent_id`` first, then merge each independent inference loop.

    Agent groups are emitted in first-seen order.  Within each group the
    assistant/tool chronology is preserved.  The ordinary zero/one-agent path
    remains exactly the historical single-buffer implementation.
    """
    import json as _json

    agent_order: list[str] = []
    explicit_agents: set[str] = set()
    for message in stream_messages:
        agent_id = (
            getattr(message, "agent_id", None)
            or getattr(message, "assistant_id", None)
        )
        if agent_id and agent_id not in explicit_agents:
            explicit_agents.add(agent_id)
            agent_order.append(agent_id)

    if len(agent_order) <= 1:
        return _merge_single_agent_stream_messages(stream_messages)

    by_agent: dict[str, list] = {agent_id: [] for agent_id in agent_order}
    unscoped: list = []
    call_owner: dict[str, str] = {}

    # Learn tool-call ownership first so a legacy result without agent_id can
    # still follow the assistant declaration that created it.
    for message in stream_messages:
        agent_id = (
            getattr(message, "agent_id", None)
            or getattr(message, "assistant_id", None)
        )
        if not agent_id or message.role != "assistant":
            continue
        for call in (message.tool_calls or []):
            call_id = call.get("id") or call.get("tool_use_id")
            if call_id:
                call_owner.setdefault(str(call_id), agent_id)

    for message in stream_messages:
        agent_id = (
            getattr(message, "agent_id", None)
            or getattr(message, "assistant_id", None)
        )
        if not agent_id and message.role == "tool":
            tool_use_id = getattr(message, "tool_use_id", None)
            if tool_use_id:
                agent_id = call_owner.get(str(tool_use_id))
        if agent_id in by_agent:
            if not getattr(message, "agent_id", None):
                # Preserve recovered ownership in the persisted turn without
                # mutating the raw frame retained for replay/debugging.
                import copy as _copy
                message = _copy.copy(message)
                message.agent_id = agent_id
            by_agent[agent_id].append(message)
        else:
            unscoped.append(message)

    turns: list = []
    for agent_id in agent_order:
        agent_turns, _ = _merge_single_agent_stream_messages(by_agent[agent_id])
        turns.extend(agent_turns)

    # Do not silently discard old/unscoped frames. Current group-chat producers
    # tag assistant, usage, and tool messages, so this is mainly compatibility.
    if unscoped:
        unscoped_turns, _ = _merge_single_agent_stream_messages(unscoped)
        turns.extend(unscoped_turns)

    last_stat: Optional[dict] = None
    for message in stream_messages:
        if message.role != "usage":
            continue
        try:
            last_stat = _json.loads(message.content)
        except (ValueError, TypeError, AttributeError):
            pass
    return turns, last_stat


def stream_batch_is_protocol_complete(messages: list) -> bool:
    """Return whether an incremental stream slice is safe to persist.

    A plain assistant round is complete when its usage frame arrives.  An
    assistant tool-call declaration is only complete after every declared call
    has a matching tool result.  Multi-agent slices are checked independently
    so one agent cannot cause another agent's partial round to be written.
    """
    agent_order: list[str] = []
    by_agent: dict[str, list] = {}
    unscoped: list = []
    for message in messages:
        agent_id = (
            getattr(message, "agent_id", None)
            or getattr(message, "assistant_id", None)
        )
        if agent_id:
            if agent_id not in by_agent:
                by_agent[agent_id] = []
                agent_order.append(agent_id)
            by_agent[agent_id].append(message)
        else:
            unscoped.append(message)

    if len(agent_order) > 1:
        return (
            all(
                any(message.role == "usage" for message in by_agent[aid])
                and stream_batch_is_protocol_complete(by_agent[aid])
                for aid in agent_order
            )
            and (
                not unscoped
                or stream_batch_is_protocol_complete(unscoped)
            )
        )

    turns, _ = merge_stream_messages(messages)
    pending: list[tuple[Optional[str], Optional[str]]] = []
    for turn in turns:
        if turn.role == "assistant" and turn.tool_calls:
            if pending:
                return False
            pending = [
                (call.get("id") or call.get("tool_use_id"), call.get("name"))
                for call in turn.tool_calls
            ]
            if any(not call_id or not name for call_id, name in pending):
                return False
        elif turn.role == "tool":
            if not pending:
                return False
            tool_use_id = turn.tool_use_id
            match_index = next(
                (
                    i for i, (call_id, _) in enumerate(pending)
                    if tool_use_id and call_id == tool_use_id
                ),
                None,
            )
            if match_index is None and not tool_use_id:
                match_index = next(
                    (i for i, (_, name) in enumerate(pending) if name == turn.name),
                    0,
                )
            if match_index is None:
                return False
            pending.pop(match_index)

    return not pending


def persist_conversation(
    context_manager: "ContextManager",
    session_id: str,
    original_messages: list,
    collected_messages: list,
    session_manager=None,
    tool_ids: Optional[list] = None,
    extra_meta: Optional[dict] = None,
    agent_ids: Optional[list] = None,
    agent_nickname: Optional[str] = None,
    model_id: Optional[str] = None,
    workspace: Optional[str] = None,
    compress: bool = True,
    update_title: bool = True,
) -> Optional[Exception]:
    """将一次推理的消息持久化到会话存储。

    提取自 _RuntimeRequestHandler._persist_conversation，供 server 和
    delegate 工具共用，避免两处维护不同的持久化逻辑。

    Args:
        context_manager: ContextManager 实例，负责读写会话文件。
        session_id: 目标会话 ID。
        original_messages: 本次推理的原始输入消息列表（Message 对象）。
        collected_messages: infer_stream 产生的原始流式消息列表（Message 对象）。
        compress: 是否在保存后触发 compress_context。推理过程中做增量持久化时
            应传 False（避免每轮都触发一次 LLM 摘要压缩），仅在推理结束时传 True。
        session_manager: 可选的 SessionManager 实例，用于更新 index.json 和
            生成标题。为 None 时跳过 index 更新（适用于 sub-session 场景）。
        update_title: 是否允许本次持久化触发自动标题生成。流式推理的前置和
            工具轮次增量落盘应传 False；只有推理完成后的最终落盘传 True。
        tool_ids: 可选的工具 ID 列表，记录到会话 meta 中，便于回溯。
        extra_meta: 可选的额外 meta 字段（如 parent_session_id），与 tool_ids
            一并通过 save_conversation 的 extra_meta 参数一次写入。
        agent_ids: 可选的 agent ID 列表（多选支持），用于标记 role=assistant 的消息的 agent_id。
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
        # Existing history is immutable unless the client explicitly requests a
        # destructive operation (Continue or revoke). Persistence must never
        # classify and silently delete prior messages on its own.
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
                agent_id=getattr(m, "agent_id", None),
                tool_id=getattr(m, "tool_id", None),
                tool_use_id=getattr(m, "tool_use_id", None),
                mentions=getattr(m, "mentions", None),
                started_at=getattr(m, "started_at", None),
            ))
        merged_turns, last_stat = merge_stream_messages(collected_messages)
        # 如果有 agent_ids，为所有 role=assistant 的消息设置 name（nickname）和 agent_id 字段
        if agent_ids:
            primary_agent_id = agent_ids[0]
            for turn in merged_turns:
                if turn.role == "assistant":
                    # Only overwrite if not already set (group-chat messages come pre-tagged)
                    if agent_nickname and not turn.name:
                        turn.name = agent_nickname
                    if not turn.agent_id:
                        turn.agent_id = primary_agent_id
                elif turn.role == "tool" and not turn.agent_id:
                    # Tool messages inherit agent_id from the assistant that called them
                    turn.agent_id = primary_agent_id
        new_turns.extend(merged_turns)
        last_total_tokens = None
        if last_stat:
            last_total_tokens = _safe_token_int(last_stat.get("total_tokens"))
            if not last_total_tokens:
                last_total_tokens = (
                    _safe_token_int(last_stat.get("prompt_tokens"))
                    + _safe_token_int(last_stat.get("completion_tokens"))
                )
            # Preserve an explicit provider-reported 0/0, but never manufacture
            # 0/0 merely because usage was absent. Unreported rounds are
            # converted to estimates by StreamUsageEstimator before persistence.
            if last_total_tokens == 0 and last_stat.get("usage_reported") is False:
                last_total_tokens = None
        merged_extra: Optional[dict] = None
        if tool_ids is not None or extra_meta or model_id or agent_ids or workspace:
            merged_extra = {}
            if tool_ids is not None:
                merged_extra["tool_ids"] = tool_ids
            if model_id is not None:
                merged_extra["model_id"] = model_id
            # Store agent_ids array
            if agent_ids is not None:
                merged_extra["agent_ids"] = agent_ids
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
        summary_version_before = context_manager.get_summary(session_id)[1].get(
            "summary_version", 0
        ) if compress else 0
        if compress:
            context_manager.compress_context(session_id, new_turns, last_total_tokens=last_total_tokens)
        summary_version_after = context_manager.get_summary(session_id)[1].get(
            "summary_version", 0
        ) if compress else summary_version_before
        compression_updated = summary_version_after != summary_version_before
        if session_manager is not None:
            session_manager.update_index(
                session_id,
                last_total_tokens=last_total_tokens,
                generate_title=update_title,
                compression_updated=compression_updated,
            )
    except Exception as exc:
        return exc
    return None


class IncrementalConversationPersister:
    """Shared state machine for safely persisting an inference stream.

    The HTTP inference handler and nested agent tools all use the same three
    phases: pre-persist the input messages, append protocol-complete streamed
    batches without compression, then persist the remaining tail and perform
    the one final compression/title update.
    """

    def __init__(
        self,
        *,
        context_manager,
        session_id: Optional[str],
        original_messages: Optional[list] = None,
        session_manager=None,
        tool_ids: Optional[list] = None,
        extra_meta: Optional[dict] = None,
        agent_ids: Optional[list] = None,
        agent_nickname: Optional[str] = None,
        model_id: Optional[str] = None,
        workspace: Optional[str] = None,
        is_active: Optional[Callable[[], bool]] = None,
        on_incremental_persist: Optional[Callable[[], None]] = None,
    ):
        self.context_manager = context_manager
        self.session_id = session_id
        self.original_messages = list(original_messages or [])
        self.session_manager = session_manager
        self.tool_ids = tool_ids
        self.extra_meta = extra_meta
        self.agent_ids = agent_ids
        self.agent_nickname = agent_nickname
        self.model_id = model_id
        self.workspace = workspace
        self.is_active = is_active
        self.on_incremental_persist = on_incremental_persist
        self.persisted_until = 0
        self._original_persisted = not self.original_messages
        self._disabled = context_manager is None or session_id is None

    def _active(self) -> bool:
        return not self._disabled and (
            self.is_active is None or self.is_active()
        )

    def _persist(
        self,
        original_messages: list,
        collected_messages: list,
        *,
        compress: bool,
        update_title: bool,
    ) -> Optional[Exception]:
        return persist_conversation(
            context_manager=self.context_manager,
            session_id=self.session_id,
            original_messages=original_messages,
            collected_messages=collected_messages,
            session_manager=self.session_manager,
            tool_ids=self.tool_ids,
            extra_meta=self.extra_meta,
            agent_ids=self.agent_ids,
            agent_nickname=self.agent_nickname,
            model_id=self.model_id,
            workspace=self.workspace,
            compress=compress,
            update_title=update_title,
        )

    def pre_persist(self) -> Optional[Exception]:
        """Persist input messages so the conversation exists before inference."""
        if self._original_persisted or not self._active():
            return None
        exc = self._persist(
            self.original_messages, [], compress=False, update_title=False,
        )
        if exc is None:
            self._original_persisted = True
        return exc

    def persist_completed(self, collected_messages: list) -> Optional[Exception]:
        """Append the pending slice if it is a complete protocol batch."""
        if not self._active():
            self.persisted_until = len(collected_messages)
            return None
        new_messages = collected_messages[self.persisted_until:]
        if not new_messages or not stream_batch_is_protocol_complete(new_messages):
            return None
        originals = [] if self._original_persisted else self.original_messages
        exc = self._persist(
            originals, new_messages, compress=False, update_title=False,
        )
        if exc is None:
            self._original_persisted = True
            self.persisted_until = len(collected_messages)
            if self.on_incremental_persist is not None:
                self.on_incremental_persist()
        return exc

    def finalize(
        self,
        collected_messages: list,
        *,
        compress: bool = True,
        update_title: bool = True,
    ) -> Optional[Exception]:
        """Persist the remaining tail and perform final compression/update."""
        if not self._active():
            self.persisted_until = len(collected_messages)
            return None
        originals = [] if self._original_persisted else self.original_messages
        remaining = collected_messages[self.persisted_until:]
        exc = self._persist(
            originals,
            remaining,
            compress=compress,
            update_title=update_title,
        )
        if exc is None:
            self._original_persisted = True
            self.persisted_until = len(collected_messages)
        return exc




def _load_function_from_file(file_path: str, func_name: str) -> Callable:
    """从指定 .py 文件动态加载函数，每次调用都重新从磁盘读取。"""
    file_path = os.path.expanduser(file_path)
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

# Per-session inference message broker. Frames are retained for the lifetime of
# the inference and fanned out to every browser that has opened the session.
# Outside flight mode, however, the producer may run only while at least one
# such browser connection (including the starter SSE) remains alive.
_session_stream_lock = threading.RLock()
_session_streams: dict[str, dict] = {}
# "Flight mode" is shared by all browsers and explicitly permits inference to
# continue with zero browser connections.
_flight_sessions: set[str] = set()


def _session_stream_debug_enabled() -> bool:
    return os.environ.get("SESSION_STREAM_DEBUG", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _session_stream_frame_summary(frame: dict) -> str:
    """Compact description for stream diagnostics without logging payloads."""
    tool_calls = frame.get("tool_calls") or []
    return (
        f"role={frame.get('role')} type={frame.get('type')} name={frame.get('name')} "
        f"streaming={frame.get('streaming')} tool_use_id={frame.get('tool_use_id')} "
        f"agent={frame.get('agent_id') or frame.get('assistant_id')} "
        f"target={frame.get('target_agent_id')} content={len(frame.get('content') or '')} "
        f"delta={len(frame.get('delta') or '')} thinking={len(frame.get('thinking') or '')} "
        f"tool_calls={len(tool_calls)}"
    )


def _cancel_unobserved_stream_locked(session_id: str, stream: dict) -> bool:
    """Cancel an active non-flight stream after its last browser disconnects.

    Must be called with ``_session_stream_lock`` held. The starter inference SSE
    counts as one session-specific connection, as does every retained/live
    subscriber created after a browser opens the session.
    """
    if stream["done"] or session_id in _flight_sessions:
        return False
    if stream["starter_connected"] or stream["subscribers"]:
        return False
    cancel_event = stream.get("cancel_event")
    if cancel_event is None:
        return False
    cancel_event.set()
    return True


def begin_session_stream(session_id: str, cancel_event=None) -> None:
    """Start a new inference generation and retire any previous broker."""
    with _session_stream_lock:
        previous = _session_streams.get(session_id)
        previous_subscribers = list(previous["subscribers"]) if previous else []
        _session_streams[session_id] = {
            "next_seq": 1,
            "persisted_seq": 0,
            "frames": [],
            "subscribers": [],
            "starter_connected": True,
            "cancel_event": cancel_event,
            "done": False,
        }
    # Wake retained GET handlers attached to the retired generation. Their UI
    # observes the authoritative streaming status and reconnects to this broker.
    for send_fn in previous_subscribers:
        try:
            send_fn(None)
        except Exception:
            pass


def publish_session_stream_frame(
    session_id: Optional[str],
    frame: dict,
    owner_event=None,
    event: Optional[str] = None,
) -> Optional[int]:
    """Publish a retained frame for the current inference generation.

    ``session_id`` can be reused immediately after a force-abort. In that case
    the old handler may still unwind while the replacement inference is already
    producing output. ``owner_event`` prevents that stale handler from
    publishing into the replacement stream's broker.
    """
    if not session_id:
        return 0
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is None:
            begin_session_stream(session_id, owner_event)
            stream = _session_streams[session_id]
        elif owner_event is not None and stream.get("cancel_event") is not owner_event:
            return None
        seq = stream["next_seq"]
        stream["next_seq"] += 1
        envelope = {
            "seq": seq,
            "event": event,
            "frame": frame,
        }
        stream["frames"].append(envelope)
        subscribers = list(stream["subscribers"])
        if _session_stream_debug_enabled():
            logging.getLogger("runtime.server").warning(
                "session_stream publish sid=%s seq=%s persisted=%s subscribers=%s starter=%s %s",
                session_id, seq, stream["persisted_seq"], len(subscribers),
                stream["starter_connected"], _session_stream_frame_summary(frame),
            )
    dead = []
    for send_fn in subscribers:
        try:
            if send_fn(envelope) is False:
                dead.append(send_fn)
        except Exception:
            dead.append(send_fn)
    if dead:
        with _session_stream_lock:
            current = _session_streams.get(session_id)
            if current:
                previous_count = len(current["subscribers"])
                current["subscribers"] = [fn for fn in current["subscribers"] if fn not in dead]
                if len(current["subscribers"]) != previous_count:
                    _cancel_unobserved_stream_locked(session_id, current)
                    if _session_stream_debug_enabled():
                        logging.getLogger("runtime.server").warning(
                            "session_stream removed_dead sid=%s dead=%s subscribers=%s",
                            session_id, len(dead), len(current["subscribers"]),
                        )
    return seq


def mark_session_stream_persisted(session_id: str, seq: int, owner_event=None) -> bool:
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is None:
            return False
        if owner_event is not None and stream.get("cancel_event") is not owner_event:
            return False
        stream["persisted_seq"] = max(stream["persisted_seq"], seq)
        return True


def finish_session_stream(session_id: str, owner_event=None) -> bool:
    """Finish only the broker generation owned by ``owner_event``."""
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is None:
            return False
        if owner_event is not None and stream.get("cancel_event") is not owner_event:
            return False
        stream["done"] = True
        subscribers = list(stream["subscribers"])
    for send_fn in subscribers:
        try:
            send_fn(None)
        except Exception:
            pass
    return True


def transition_session_stream_status(
    session_id: str,
    owner_event,
    status: str,
) -> bool:
    """Set/broadcast status only for the current inference generation.

    The stream-owner check, state mutation and event enqueue are serialized
    against ``begin_session_stream``. This guarantees that an old request cannot
    broadcast ``done_error_unread`` after a newer request for the same session
    has already announced ``streaming``.
    """
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is None or stream.get("cancel_event") is not owner_event:
            return False
        with _session_state_lock:
            _session_statuses[session_id] = status
            if status in {"done_success_unread", "done_error_unread"}:
                _unread_sessions[session_id] = status
            else:
                _unread_sessions.pop(session_id, None)
        # Keep the stream-generation lock through enqueueing. A replacement
        # begin/status transition therefore always appears after this event.
        _broadcast_session_status(session_id, status)
        return True


def get_session_stream_snapshot(session_id: str, after_seq: int = -1) -> dict:
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is None:
            return {"active": False, "done": True, "persisted_seq": 0, "latest_seq": 0, "frames": []}
        # Negative means "conversation.json is the baseline": replay only frames
        # produced after the latest successful incremental persistence.
        effective_after = stream["persisted_seq"] if after_seq < 0 else after_seq
        return {
            "active": not stream["done"],
            "done": stream["done"],
            "persisted_seq": stream["persisted_seq"],
            "latest_seq": stream["next_seq"] - 1,
            "frames": [item.copy() for item in stream["frames"] if item["seq"] > effective_after],
        }


def subscribe_session_stream(session_id: str, send_fn) -> bool:
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is None or stream["done"]:
            return False
        stream["subscribers"].append(send_fn)
        return True


def unsubscribe_session_stream(session_id: str, send_fn) -> None:
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is not None:
            try:
                stream["subscribers"].remove(send_fn)
            except ValueError:
                return
            _cancel_unobserved_stream_locked(session_id, stream)


def disconnect_session_stream_starter(session_id: str, owner_event=None) -> bool:
    """Mark the inference-starting browser connection as disconnected.

    A stale POST connection must not disconnect the starter belonging to a
    replacement inference for the same session.
    """
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is None:
            return False
        if owner_event is not None and stream.get("cancel_event") is not owner_event:
            return False
        stream["starter_connected"] = False
        return _cancel_unobserved_stream_locked(session_id, stream)


def set_session_flight_mode(session_id: str, enabled: bool) -> bool:
    with _session_stream_lock:
        if enabled:
            _flight_sessions.add(session_id)
        else:
            _flight_sessions.discard(session_id)
            stream = _session_streams.get(session_id)
            if stream is not None:
                _cancel_unobserved_stream_locked(session_id, stream)
        return session_id in _flight_sessions


def is_session_flight_mode(session_id: str) -> bool:
    with _session_stream_lock:
        return session_id in _flight_sessions


def flight_sessions_snapshot() -> list[str]:
    with _session_stream_lock:
        return sorted(_flight_sessions)


def register_session_stream_with_snapshot(session_id: str, send_fn, after_seq: int = -1) -> tuple[bool, dict]:
    """Atomically register a subscriber and take its replay snapshot."""
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is None:
            return False, {"active": False, "done": True, "persisted_seq": 0, "latest_seq": 0, "frames": []}
        registered = not stream["done"]
        if registered:
            stream["subscribers"].append(send_fn)
        effective_after = stream["persisted_seq"] if after_seq < 0 else after_seq
        snapshot = {
            "active": registered,
            "done": stream["done"],
            "persisted_seq": stream["persisted_seq"],
            "latest_seq": stream["next_seq"] - 1,
            "frames": [item.copy() for item in stream["frames"] if item["seq"] > effective_after],
        }
        if _session_stream_debug_enabled():
            logging.getLogger("runtime.server").warning(
                "session_stream register sid=%s requested_after=%s effective_after=%s active=%s "
                "persisted=%s latest=%s replay=%s subscribers=%s",
                session_id, after_seq, effective_after, registered, stream["persisted_seq"],
                snapshot["latest_seq"], len(snapshot["frames"]), len(stream["subscribers"]),
            )
        return registered, snapshot


# ---------------------------------------------------------------------------
# Terminal Sessions – in-memory state
# ---------------------------------------------------------------------------
# Maps terminal_id -> { master_fd, pid, sock, session_id, output_buffer, buffer_lock }
# terminal_id format: "{session_id}" or "{session_id}:{assistant_id}"
_terminal_sessions: dict[str, dict] = {}
_terminal_sessions_lock = threading.Lock()


def get_terminal_session(terminal_id: str) -> Optional[dict]:
    """Get terminal session by terminal_id."""
    with _terminal_sessions_lock:
        return _terminal_sessions.get(terminal_id)


def register_terminal_session(terminal_id: str, master_fd, pid, sock, session_id: str) -> None:
    """Register a new terminal session."""
    with _terminal_sessions_lock:
        _terminal_sessions[terminal_id] = {
            "master_fd": master_fd,
            "pid": pid,
            "sock": sock,
            "session_id": session_id,
            "output_buffer": [],  # Buffer for collecting command output
            "buffer_lock": threading.Lock(),
            "disconnected_at": None,  # When the session was disconnected (for cleanup)
            "active": True,  # Whether this connection is active
        }


def get_or_create_terminal(
    session_id: str,
    cols: int = 80,
    rows: int = 24,
    send_output: Optional[Callable[[str], None]] = None,
) -> Optional[dict]:
    """Get existing terminal session for session_id, or create a new one.
    
    Args:
        session_id: Session identifier
        cols: Initial terminal columns (used only when creating a new PTY)
        rows: Initial terminal rows (used only when creating a new PTY)
        send_output: Optional output callback installed before a new PTY starts
    
    Returns terminal_info dict, or None if creation failed.
    """
    # Try to find existing terminal
    terminal_info = get_terminal_for_session(session_id)
    if terminal_info:
        if send_output is not None:
            terminal_info["send_output"] = send_output
        return terminal_info
    
    # Create new terminal session
    terminal_id = f"{session_id}:auto"
    session_id_short = session_id.split(":")[0] if session_id else session_id
    
    if sys.platform == "win32":
        try:
            from winpty import PtyProcess
        except ImportError:
            PtyProcess = None
        
        if PtyProcess is None:
            return None
        
        try:
            # Resolve the workspace directory for this terminal session.
            from runtime.common import get_workspace as _get_ws
            workspace_dir = _get_ws()
            proc = PtyProcess.spawn("powershell.exe", dimensions=(rows, cols), cwd=workspace_dir)
            with _terminal_sessions_lock:
                _terminal_sessions[terminal_id] = {
                    "proc": proc,
                    "sock": None,
                    "session_id": session_id_short,
                    "active": True,
                    "disconnected_at": None,
                    "output_buffer": [],
                    "buffer_lock": threading.Lock(),
                    "shell_kind": "powershell",
                    "send_output": send_output,
                }
            
            # Start background thread to drain PTY output into buffer
            terminal_info = _terminal_sessions[terminal_id]
            def read_pty():
                logger.debug("read_pty thread started for %s (win32)", terminal_id)
                try:
                    while terminal_info.get("active", True):
                        try:
                            data = proc.read(4096)
                            if data:
                                # WinPTY reads are blocking, so this must remain the
                                # sole reader for the process.  Fan the same stream
                                # out to the attached browser and exec_cli buffer;
                                # starting another reader on WebSocket attach can
                                # split PowerShell's VT redraw sequences between
                                # consumers and corrupt the displayed command line.
                                send_output = terminal_info.get("send_output")
                                if send_output:
                                    try:
                                        send_output(data)
                                    except Exception:
                                        pass
                                with terminal_info["buffer_lock"]:
                                    terminal_info["output_buffer"].append(data)
                            else:
                                logger.debug("read_pty: EOF for %s (win32)", terminal_id)
                                break
                        except EOFError:
                            logger.debug("read_pty: EOFError for %s (win32)", terminal_id)
                            break
                except Exception as e:
                    logger.error("read_pty: unexpected error for %s (win32): %s", terminal_id, e)
                logger.debug("read_pty thread exiting for %s (win32)", terminal_id)
            threading.Thread(target=read_pty, daemon=True).start()
            
            return terminal_info
        except Exception as e:
            logger.debug("Failed to create winpty terminal: %s", e)
            return None
    else:
        try:
            shell = os.environ.get("SHELL", "/bin/bash")
            # Resolve the workspace directory for this terminal session.
            # Respects per-request workspace overrides (set_request_context)
            # falling back to AGENTS_WORKSPACE env var, then os.getcwd().
            from runtime.common import get_workspace as _get_ws
            workspace_dir = _get_ws()
            pid, master_fd = pty.fork()
            if pid == 0:
                # Child process: change to workspace directory, then start shell
                try:
                    os.chdir(workspace_dir)
                except OSError:
                    pass  # If the workspace dir doesn't exist, stay in inherited CWD
                env = os.environ.copy()
                env["TERM"] = "xterm-256color"
                # Ensure the shell also knows the workspace
                env["AGENTS_WORKSPACE"] = workspace_dir
                os.execvpe(shell, [shell], env)
            
            # Set initial window size for the PTY
            try:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
            except Exception:
                pass
            
            with _terminal_sessions_lock:
                _terminal_sessions[terminal_id] = {
                    "master_fd": master_fd,
                    "pid": pid,
                    "sock": None,
                    "session_id": session_id_short,
                    "active": True,
                    "disconnected_at": None,
                    "output_buffer": [],
                    "buffer_lock": threading.Lock(),
                    "shell_kind": os.path.basename(shell or "/bin/bash"),
                }
            
            # Start background thread to drain PTY output into buffer
            terminal_info = _terminal_sessions[terminal_id]
            def read_pty():
                logger.debug("read_pty thread started for %s", terminal_id)
                try:
                    while terminal_info.get("active", True):
                        try:
                            ready, _, _ = select.select([master_fd], [], [], 0.1)
                            if ready:
                                data = os.read(master_fd, 4096)
                                if data:
                                    with terminal_info["buffer_lock"]:
                                        terminal_info["output_buffer"].append(data.decode(get_system_encoding(), errors='replace'))
                                else:
                                    logger.debug("read_pty: EOF for %s", terminal_id)
                                    break
                        except select.error as e:
                            logger.debug("read_pty: select error for %s: %s", terminal_id, e)
                            break
                        except OSError as e:
                            logger.debug("read_pty: OSError for %s: %s", terminal_id, e)
                            break
                except Exception as e:
                    logger.error("read_pty: unexpected error for %s: %s", terminal_id, e)
                logger.debug("read_pty thread exiting for %s", terminal_id)
            threading.Thread(target=read_pty, daemon=True).start()
            
            return terminal_info
        except Exception as e:
            logger.debug("Failed to create pty terminal: %s", e)
            return None


def unregister_terminal_session(terminal_id: str) -> None:
    """Remove terminal session from registry."""
    with _terminal_sessions_lock:
        session = _terminal_sessions.pop(terminal_id, None)
        if session:
            # Signal read_pty thread to stop
            session["active"] = False
            
            # Kill the process
            if sys.platform == "win32":
                # Windows: use proc.terminate()
                proc = session.get("proc")
                if proc:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
            else:
                # Unix: use os.kill
                try:
                    os.kill(session["pid"], signal.SIGKILL)
                except OSError:
                    pass
                
                # Wait for process to exit (with timeout)
                try:
                    os.waitpid(session["pid"], os.WNOHANG)
                except (OSError, ChildProcessError):
                    pass
                
                # Close master fd
                try:
                    os.close(session["master_fd"])
                except OSError:
                    pass


def get_terminal_for_session(session_id: str) -> Optional[dict]:
    """Get terminal session for a given inference session_id."""
    with _terminal_sessions_lock:
        for tid, info in _terminal_sessions.items():
            if info["session_id"] == session_id:
                return info
    return None


def cleanup_expired_terminal_sessions() -> None:
    """Clean up terminal sessions that have been disconnected for too long."""
    while True:
        time.sleep(60)  # Check every minute
        now = time.monotonic()
        expired = []
        
        with _terminal_sessions_lock:
            for tid, info in list(_terminal_sessions.items()):
                disconnected_at = info.get("disconnected_at")
                if disconnected_at and (now - disconnected_at) > os.getenv("TERMINAL_SESSION_TIMEOUT", 3600):
                    expired.append(tid)
        
        for tid in expired:
            logger.info("Cleaning up expired terminal session: %s", tid)
            unregister_terminal_session(tid)


# Start cleanup thread
_cleanup_thread = threading.Thread(target=cleanup_expired_terminal_sessions, daemon=True)
_cleanup_thread.start()


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
    if _session_stream_debug_enabled():
        logging.getLogger("runtime.server").warning(
            "session_events publish sid=%s event=%s status=%s subscribers=%s",
            session_id, event_type, data.get("status"), len(subscribers_snapshot),
        )

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

