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

from __future__ import annotations

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

from runtime.common import (
    env_int,
    get_system_encoding,
    get_request_context,
    estimate_chat_message_tokens,
    estimate_chat_prompt_tokens,
    estimate_message_payload_tokens,
)
from runtime.context_manager import ConversationTurn

logger = logging.getLogger("runtime.server")


def _safe_token_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _message_estimated_tokens(message) -> int:
    """Estimate one message as it contributes to a subsequent model prompt."""
    return estimate_chat_message_tokens(message)


class StreamUsageEstimator:
    """Normalize provider usage and synthesize logical per-request estimates.

    A provider-reported usage object is authoritative, including an explicit
    0/0.  When usage is absent, prompt tokens are estimated from the complete
    context sent to the first model request.  Later tool-loop requests extend
    that context with the preceding assistant/tool messages.  Completion tokens
    describe only the current model response, while ``total_*`` fields accumulate
    all model requests in this HTTP inference.
    """

    def __init__(
        self,
        context_manager=None,
        session_id=None,
        original_messages=None,
        request_messages=None,
        tools=None,
    ):
        del context_manager, session_id  # Retained for call-site compatibility.
        initial_messages = (
            request_messages if request_messages is not None else original_messages
        ) or []
        self._default_prompt_tokens = estimate_chat_prompt_tokens(initial_messages, tools)
        self._prompt_by_agent: dict[str, int] = {}
        self._pending_context_by_agent: dict[str, int] = {}
        self._total_prompt_by_agent: dict[str, int] = {}
        self._total_completion_by_agent: dict[str, int] = {}
        self._open_by_agent: dict[str, bool] = {}
        self._error_by_agent: dict[str, bool] = {}
        self._model_by_agent: dict[str, Optional[str]] = {}
        self._content_by_agent: dict[str, str] = {}
        self._thinking_by_agent: dict[str, str] = {}
        self._tool_calls_by_agent: dict[str, dict[object, dict]] = {}

    @staticmethod
    def _agent_key(message) -> str:
        return (
            getattr(message, "agent_id", None)
            or getattr(message, "assistant_id", None)
            or ""
        )

    def _ensure_agent(self, key: str) -> None:
        self._prompt_by_agent.setdefault(key, self._default_prompt_tokens)
        self._pending_context_by_agent.setdefault(key, 0)
        self._total_prompt_by_agent.setdefault(key, 0)
        self._total_completion_by_agent.setdefault(key, 0)
        self._open_by_agent.setdefault(key, False)
        self._error_by_agent.setdefault(key, False)
        self._content_by_agent.setdefault(key, "")
        self._thinking_by_agent.setdefault(key, "")
        self._tool_calls_by_agent.setdefault(key, {})

    def _observe_assistant_output(self, key: str, message) -> None:
        self._content_by_agent[key] += getattr(message, "content", "") or ""
        self._thinking_by_agent[key] += getattr(message, "thinking", "") or ""
        calls = self._tool_calls_by_agent[key]
        for position, call in enumerate(getattr(message, "tool_calls", None) or []):
            if not isinstance(call, dict):
                continue
            index = call.get("_index", position)
            merged = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            for field in ("id", "name", "arguments"):
                value = call.get(field)
                if value is None:
                    continue
                if not isinstance(value, str):
                    try:
                        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    except (TypeError, ValueError):
                        value = str(value)
                merged[field] += value

    def _current_completion(self, key: str) -> int:
        payload = {
            "content": self._content_by_agent.get(key, ""),
            "thinking": self._thinking_by_agent.get(key, ""),
            "tool_calls": list(self._tool_calls_by_agent.get(key, {}).values()) or None,
        }
        # Completion usage excludes the assistant role/chat-template wrapper.
        return estimate_message_payload_tokens(payload)

    def _current_prompt(self, key: str) -> int:
        return (
            self._prompt_by_agent.get(key, self._default_prompt_tokens)
            + self._pending_context_by_agent.get(key, 0)
        )

    def _estimate(self, key: str, stat: Optional[dict] = None) -> dict:
        source = dict(stat or {})
        prompt = self._current_prompt(key)
        completion = self._current_completion(key)
        total_prompt = self._total_prompt_by_agent.get(key, 0) + prompt
        total_completion = self._total_completion_by_agent.get(key, 0) + completion
        source.update({
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_all_tokens": total_prompt + total_completion,
            "estimated": True,
            "usage_reported": False,
        })
        # Provider usage was absent, so cache counters are unknown even for
        # protocols that normally report numeric cache fields.
        source["cached_input_tokens"] = None
        source["new_token_cache"] = None
        source.pop("model_id", None)
        return source

    def _close_round(self, key: str, stat: dict) -> None:
        prompt = _safe_token_int(stat.get("prompt_tokens"))
        completion = _safe_token_int(stat.get("completion_tokens"))
        self._prompt_by_agent[key] = prompt
        if "total_prompt_tokens" in stat:
            self._total_prompt_by_agent[key] = _safe_token_int(stat.get("total_prompt_tokens"))
        else:
            self._total_prompt_by_agent[key] += prompt
        if "total_completion_tokens" in stat:
            self._total_completion_by_agent[key] = _safe_token_int(stat.get("total_completion_tokens"))
        else:
            self._total_completion_by_agent[key] += completion

        # If another tool-loop round follows, this assistant response and later
        # tool results become new prompt context.  Use observed text/JSON rather
        # than provider completion count because those exact messages are what
        # the next request serializes.
        self._pending_context_by_agent[key] = 4 + self._current_completion(key)
        self._content_by_agent[key] = ""
        self._thinking_by_agent[key] = ""
        self._tool_calls_by_agent[key] = {}
        self._open_by_agent[key] = False
        self._error_by_agent[key] = False

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
                self._observe_assistant_output(key, message)
            return
        if role == "tool":
            self._pending_context_by_agent[key] += _message_estimated_tokens(message)
            return
        if role != "usage":
            return

        try:
            stat = json.loads(getattr(message, "content", "") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            stat = {}
        if stat.get("usage_reported") is False:
            # Runtime normally fills logical estimates from the exact request.
            # Re-estimate only legacy/third-party 0/0 payloads that still carry
            # no usable fallback values.
            if (
                _safe_token_int(stat.get("prompt_tokens")) == 0
                and _safe_token_int(stat.get("completion_tokens")) == 0
            ):
                stat = self._estimate(key, stat)
            else:
                stat["estimated"] = True
                stat["usage_reported"] = False
                stat["cached_input_tokens"] = None
                stat["new_token_cache"] = None
            message.content = json.dumps(stat, ensure_ascii=False)
        if stat.get("model_id"):
            self._model_by_agent[key] = stat.get("model_id")
        self._close_round(key, stat)

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
            # directly instead of feeding it through observe() again.
            self._close_round(key, stat)
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
        if m.role == "user":
            # Interjected user turn (mid-run insertion): the inference loop
            # yields it at a round boundary, so the stream may contain a bare
            # user message between rounds. Flush any open assistant buffer
            # first, then persist the user turn itself — otherwise the saved
            # conversation would silently drop what the model actually saw.
            if assistant_text_buf or assistant_thinking_buf or pending_tool_calls:
                _flush_assistant(stat=current_stat or last_stat)
            turns.append(ConversationTurn(
                role="user",
                content=m.content or "",
                timestamp=m.timestamp or now_iso(),
                name=getattr(m, "name", None) or "",
                images=getattr(m, "images", None),
                agent_id=getattr(m, "agent_id", None),
                mentions=getattr(m, "mentions", None),
                meta=(
                    {"kind": "interjection"}
                    if getattr(m, "kind", None) == "interjection" else None
                ),
            ))
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
            if getattr(m, "discard_stream_output", False):
                # The model round is degenerate (no tool call, text only carried
                # XML markup) and is being re-rolled.  Nothing this attempt
                # streamed may become conversation history: drop the buffers so
                # the re-roll becomes the assistant turn instead of being
                # appended to the discarded attempt.
                assistant_text_buf = ""
                assistant_thinking_buf = ""
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


def pending_client_tool_calls(merged_turns: list, client_tool_names) -> list:
    """Tool calls of a run that only the caller can answer.

    A client-side tool (``tool_type='client_side'``) has no executable entity in
    the agent service, so the inference loop leaves its call declared but
    unanswered and ends normally.  Given the merged conversation turns of one run
    (as produced by :func:`merge_stream_messages`) and the names/ids of the
    session's client-side tools, this returns those handoff calls in declaration
    order as ``{"id", "name", "arguments"}``, so a caller knows exactly what to
    execute locally before resuming the same session.

    Calls that did receive a ``role="tool"`` result inside the same run are
    skipped: only a genuinely unanswered client-side call is a handoff.
    """
    names = set(client_tool_names or ())
    if not names:
        return []

    def _field(obj, key):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    answered = {
        _field(turn, "tool_use_id")
        for turn in merged_turns
        if _field(turn, "role") == "tool"
    }
    answered.discard(None)

    pending: list = []
    for turn in merged_turns:
        if _field(turn, "role") != "assistant":
            continue
        tool_calls = _field(turn, "tool_calls")
        for call in tool_calls or []:
            if not isinstance(call, dict):
                continue
            name = call.get("name", "")
            if name not in names:
                continue
            call_id = call.get("id") or call.get("tool_use_id")
            if call_id and call_id in answered:
                continue
            arguments = call.get("arguments", "{}")
            if not isinstance(arguments, str):
                try:
                    arguments = json.dumps(arguments, ensure_ascii=False)
                except (TypeError, ValueError):
                    arguments = "{}"
            pending.append({"id": call_id, "name": name, "arguments": arguments})
    return pending


def answered_client_tool_call_ids(merged_turns: list, client_tool_names) -> set:
    """Client-side calls this conversation already has a result for.

    The mirror image of :func:`pending_client_tool_calls`: of the calls that
    belong to the session's client-side tools, these are the ones a
    ``role="tool"`` turn already answers.

    A session can have several clients and one client-side call has exactly one
    result, so this is the durable half of the arbitration: whoever answered
    first is in the conversation, and any later answer for the same call is a
    duplicate.  Position decides it, not mere presence: a call id that was
    answered and then **declared again** (models are not always disciplined about
    id uniqueness across rounds) is a new call waiting for its first result, so
    an id counts as answered only when its last appearance in the history is the
    result.  Takes the same turn shapes as :func:`pending_client_tool_calls`
    (dicts or objects), so a persisted conversation can be passed straight in.
    """
    names = set(client_tool_names or ())
    if not names:
        return set()

    def _field(obj, key):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    # id -> (state, position of that state)
    last_seen: dict = {}
    for position, turn in enumerate(merged_turns):
        role = _field(turn, "role")
        if role == "assistant":
            for call in _field(turn, "tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                if call.get("name", "") not in names:
                    continue
                call_id = call.get("id") or call.get("tool_use_id")
                if call_id:
                    last_seen[str(call_id)] = ("declared", position)
        elif role == "tool":
            call_id = _field(turn, "tool_use_id")
            if call_id is not None and str(call_id) in last_seen:
                last_seen[str(call_id)] = ("answered", position)

    return {
        call_id for call_id, (state, _) in last_seen.items() if state == "answered"
    }


# One client-side call has exactly one result, whichever client produced it.
# ``answered_client_tool_call_ids`` reads that back out of conversation.json, but
# two clients can post their answer for the same call at the same moment --
# before either answer has reached disk, where the history can arbitrate.  A
# request that submits answers therefore also *claims* the call ids it answers
# for as long as its own generation runs: the client that claimed first is the
# one whose result counts, and the other is told so instead of starting a
# competing turn.
_client_answer_claims: dict[str, dict[str, float]] = {}
_client_answer_claims_lock = threading.Lock()

# A claim only has to outlive the request that took it.  The TTL is the safety
# net for a handler that dies before releasing: an id nobody could ever answer
# again would be worse than a duplicate, which the persisted history now
# rejects on its own.  Overridable for tests.
CLIENT_ANSWER_CLAIM_TTL_S = 900.0


def _client_answer_claim_ttl_s() -> float:
    raw = os.environ.get("CLIENT_ANSWER_CLAIM_TTL_S", "")
    if not raw.strip():
        return CLIENT_ANSWER_CLAIM_TTL_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return CLIENT_ANSWER_CLAIM_TTL_S


def claim_client_tool_answers(
    session_id, call_ids, ttl_s: Optional[float] = None,
) -> list:
    """Claim the right to answer these client-side calls of one session.

    Returns the ids that are already claimed -- answered and persisted, or
    answered by another request that is still in flight -- and claims nothing in
    that case.  All-or-nothing on purpose: a handoff batch belongs to one model
    round, so silently answering part of it would leave the session waiting for
    results that were thrown away.  The calls that stayed unanswered remain
    pending in the conversation, where any client can still answer them.
    """
    ids = [str(call_id) for call_id in (call_ids or []) if call_id]
    if not session_id or not ids:
        return []
    ttl = _client_answer_claim_ttl_s() if ttl_s is None else ttl_s
    now = time.monotonic()
    with _client_answer_claims_lock:
        claims = _client_answer_claims.setdefault(session_id, {})
        for call_id, deadline in list(claims.items()):
            if deadline <= now:
                claims.pop(call_id, None)      # the holder is gone; free the id
        conflicts = [call_id for call_id in ids if call_id in claims]
        if conflicts:
            return conflicts
        for call_id in ids:
            claims[call_id] = now + ttl
    return []


def release_client_tool_answers(session_id, call_ids=None) -> None:
    """Give back the claim on these calls: the generation that took it is over.

    Releasing is not what allows a second result -- once the first answer is in
    conversation.json the history check rejects later ones.  It only keeps the
    in-memory table from holding calls no client can answer any more.
    """
    if not session_id:
        return
    with _client_answer_claims_lock:
        claims = _client_answer_claims.get(session_id)
        if not claims:
            return
        if call_ids is None:
            _client_answer_claims.pop(session_id, None)
            return
        for call_id in call_ids:
            claims.pop(str(call_id), None)
        if not claims:
            _client_answer_claims.pop(session_id, None)


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


def build_turn_meta(
    model_id: Optional[str] = None,
    tool_ids: Optional[list] = None,
    agent_ids: Optional[list] = None,
    workspace: Optional[str] = None,
    extra_meta: Optional[dict] = None,
) -> Optional[dict]:
    """Snapshot the request context a user turn was sent with.

    The session-level ``meta`` block only ever holds the *latest* values, but
    a session can switch model / tool set / execution environment / agent /
    workspace between turns.  The snapshot is stored on the initiating user
    turn (``messages[i].meta``) so the settings each turn actually ran with
    stay readable after later turns overwrite the session block.

    Returns ``None`` when nothing is known, so no empty dict is persisted.
    """
    turn_meta: dict = {}
    if model_id:
        turn_meta["model_id"] = model_id
    if tool_ids is not None:
        turn_meta["tool_ids"] = list(tool_ids)
    if agent_ids:
        turn_meta["agent_ids"] = list(agent_ids)
    if workspace:
        turn_meta["workspace"] = workspace
    remote_env = (extra_meta or {}).get("remote_env")
    if remote_env:
        turn_meta["remote_env"] = remote_env
    return turn_meta or None


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

    会话级 meta 只保留「最新一次」的配置，而同一会话中途可能更换模型 /
    工具集 / 执行环境 / Agent / 工作区。因此这些当时的取值同时快照到发起
    本轮的用户消息上（``messages[i].meta``），历史轮次的设置不会被后续
    轮次覆盖；Continue 不携带新用户消息，则刷新最后一个用户 turn 的快照。

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
        # What this request was configured with, recorded on the user turn it
        # initiates (see ``build_turn_meta``).
        turn_meta = build_turn_meta(
            model_id=model_id,
            tool_ids=tool_ids,
            agent_ids=agent_ids,
            workspace=workspace,
            extra_meta=extra_meta,
        )
        new_user_turns = 0
        for m in (original_messages or []):
            # 复用 Message 自带的时间戳，无则 fallback 为当前时间
            ts = m.timestamp if m.timestamp else now_iso()
            turn = ConversationTurn(
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
            )
            if turn.role == "user" and turn_meta:
                turn.meta = dict(turn_meta)
                new_user_turns += 1
            new_turns.append(turn)
        if turn_meta and not new_user_turns:
            # Continue/retry sends no new user message: it re-runs the existing
            # user turn, possibly with a different model / tool set / agent /
            # environment. Refresh that turn's snapshot so it keeps describing
            # the settings the turn actually ran with. Idempotent across the
            # incremental persistences of one request.
            for turn in reversed(new_turns):
                if turn.role == "user":
                    merged_meta = dict(turn.meta or {})
                    merged_meta.update(turn_meta)
                    turn.meta = merged_meta
                    break
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
            context_manager.compress_context(
                session_id, new_turns, last_total_tokens=last_total_tokens,
                inference_model_id=model_id,
                trigger="post_turn",
            )
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

    def is_current(self, collected_messages: list) -> bool:
        """Return True while every message of *collected_messages* is on disk.

        Mid-loop context compression (:class:`InLoopContextCompressor`) rebuilds
        the in-flight message list from ``conversation.json``.  That is only
        correct while the round that just finished is already persisted —
        otherwise the rebuilt context would silently drop the tool results the
        model is waiting for.  False is also returned once this stream no longer
        owns the session (its persistence is deliberately skipped from then on).
        """
        if self._disabled or not self._active():
            return False
        return self.persisted_until >= len(collected_messages)


class InLoopContextCompressor:
    """Compress a session mid-tool-loop and rebuild the in-flight context.

    The session-completed trigger (``MAX_TOKENS_IN_CONTEXT``) only runs when an
    inference finishes, but a single inference can run hundreds of tool rounds
    while the whole message list is re-sent on every round.  That growth is what
    pushes a request past the model's window mid-loop — and for a provider whose
    hard limit is exactly the total of context + output it is invisible to the
    overflow self-healing, because nothing is ever rejected.

    ``Runtime.infer_stream`` invokes this callable after every completed tool
    round with ``(messages, round_total_tokens, max_context)``.  When the round's
    input + output tokens reach 90 % of the running model's context window
    (``ModelConfig.max_context``; ``0`` means the window is unknown, which
    disables the check) the session is compressed (summary + memory; incremental
    exactly like every other compression) and the in-flight message list is
    rebuilt the same way a brand-new turn is assembled:

      1. one merged system message (agent/session prompt + rolling summary +
         structured memory),
      2. the recent verbatim window, which still contains the round that just
         finished (the assistant tool call and its results),

    so the next round starts from a small context instead of the accumulated
    history.  Compression itself is incremental and keeps the most recent K
    turns unsummarized, which is what guarantees the pending tool round survives
    the rebuild.

    Rebuilding uses the same read path as a new user turn
    (``ContextManager.assemble_context``), so nothing about the request format
    changes — only the size of the context the loop carries.

    The rebuild reads the session from disk, so callers pass a ``ready``
    predicate (normally :meth:`IncrementalConversationPersister.is_current`)
    that must hold at call time; otherwise the hook is a no-op and the loop
    keeps its accumulated messages.

    A second entry point, :meth:`ensure_within_budget`, serves the pre-send
    guard of :meth:`Runtime.infer_stream`.  When the request that is about to be
    sent is measured larger than the model's window minus its output reserve,
    this hook may not decline: it compresses (incrementally, then deeply) and
    offloads oversized messages to files until the request fits, and otherwise
    reports that it did not fit so the caller refuses the send instead of
    burning a round-trip the provider would reject.  Both entry points share the
    same rebuild path and the same never-drop-the-pending-round invariants; see
    ``docs/plan-context-send-guard.md``.
    """

    def __init__(
        self,
        *,
        context_manager,
        session_id: Optional[str],
        ready: Optional[Callable[[], bool]] = None,
        inference_model_id: str = "",
    ) -> None:
        self.context_manager = context_manager
        self.session_id = session_id
        self.ready = ready
        # model_id of the model running this loop.  Compression bounds its
        # prompt by the compression model's window and, when the summary model is
        # unavailable or smaller, runs on this inference model (whose window is
        # provably sufficient since the trigger fires at 90 % of it).
        self.inference_model_id = inference_model_id or ""

    def __call__(
        self,
        messages: list,
        round_total_tokens: int,
        max_context: int = 0,
    ) -> Optional[list]:
        """Return a rebuilt message list, or None to keep the current one.

        Args:
            messages: In-flight message list of the inference loop (informational
                — the rebuild is derived from the persisted session).
            round_total_tokens: Prompt + completion tokens of the completed round.
            max_context: Context window of the model running this loop, in tokens
                (``ModelConfig.max_context``; 0 = unknown, which skips the check).
        """
        if self.context_manager is None or not self.session_id:
            return None
        if self.ready is not None and not self.ready():
            logger.info(
                "in-loop context compression: session %s stream is not fully "
                "persisted; keeping the accumulated context",
                self.session_id,
            )
            return None
        context_manager = self.context_manager
        session_id = self.session_id
        try:
            turns = context_manager.load_conversation(session_id)
        except (FileNotFoundError, ValueError):
            return None
        # Below the model's in-loop threshold nothing is sent to the summary model.
        if not context_manager.compress_context_in_loop(
            session_id, turns, round_total_tokens, max_context
        ):
            return None
        rebuilt = self._rebuild_from_disk()
        if rebuilt is None:
            return None
        _, front_matter = context_manager.get_summary(session_id)
        logger.info(
            "in-loop context compression: session %s rebuilt %d messages after a "
            "%d-token round (summary_version=%s, summarized_up_to_turn=%s)",
            session_id,
            len(rebuilt),
            round_total_tokens,
            front_matter.get("summary_version"),
            front_matter.get("summarized_up_to_turn"),
        )
        return rebuilt

    # ------------------------------------------------------------------
    # Pre-send guard: the request about to be sent does not fit the window
    # ------------------------------------------------------------------

    def ensure_within_budget(
        self,
        messages: list,
        input_budget: int,
        estimate: Callable[[list], int],
        max_context: int = 0,
    ) -> tuple[Optional[list], int]:
        """Compress / offload until the pending request fits *input_budget*.

        Called by the pre-send guard of :meth:`Runtime.infer_stream` when the
        measured prompt of the request that is ABOUT to be sent exceeds the
        model's window minus its output reserve.  Unlike :meth:`__call__` —
        which fires at 90 % of the window at the END of a completed round, on
        purpose, because compression then costs the user no waiting time — this
        one must act: the provider is about to reject the request.

        Steps, least lossy first (each re-measured, stopping as soon as the
        request fits):

        1. one incremental compression round.  Because the post-round
           compression already ran, its delta is just what came since: the last
           tool round and any user message injected mid-flight;
        2. if that is not enough, a DEEP compression round: everything except
           the pending round is summarised (oversized turns among them are
           offloaded to a file by the compression prompt builder, so their full
           text stays reachable);
        3. if the pending round ALONE does not fit, its oversized messages are
           offloaded to files in place, leaving an excerpt plus the path.

        ``estimate`` is the caller's prompt estimator (same estimator and tool
        schemas as the guard used, so the numbers stay comparable).

        Returns ``(replacement, tokens_after)``: ``replacement`` is ``None``
        when nothing could be changed, and ``tokens_after`` is the estimate of
        the list the caller should send, so the caller re-measures for free and
        can refuse the request instead of burning a rejected round-trip.
        """
        from runtime.common import estimate_llm_tokens  # local: hot-path module

        budget = int(input_budget or 0)
        current = list(messages)
        current_tokens = estimate(current)
        if (
            self.context_manager is None
            or not self.session_id
            or budget <= 0
            or current_tokens <= budget
        ):
            return None, current_tokens

        session_id = self.session_id
        context_manager = self.context_manager
        replaced = False

        # Rebuilding reads the session from disk, so it is only safe while this
        # stream owns the session and its frames are persisted (same rule as
        # __call__).  When it is not safe, skip straight to step 3, which works
        # on the in-flight list itself.
        if self.ready is None or self.ready():
            try:
                turns = context_manager.load_conversation(session_id)
            except (FileNotFoundError, ValueError):
                turns = []
            if turns:
                before = context_manager.get_summary(session_id)[1].get(
                    "summarized_up_to_turn"
                )
                for deep in (False, True):
                    previous_tokens = current_tokens
                    context_manager.compress_context(
                        session_id,
                        turns,
                        last_total_tokens=current_tokens,
                        trigger_threshold=budget,
                        inference_model_id=self.inference_model_id,
                        inference_max_context=max_context,
                        deep=deep,
                        trigger="pre_send",
                    )
                    after = context_manager.get_summary(session_id)[1].get(
                        "summarized_up_to_turn"
                    )
                    if after == before:
                        # Nothing new to summarise (or compression is disabled /
                        # in failure backoff): a deeper pass cannot help either.
                        break
                    before = after
                    rebuilt = self._rebuild_from_disk()
                    if rebuilt is None:
                        break
                    current, current_tokens, replaced = rebuilt, estimate(rebuilt), True
                    logger.info(
                        "pre-send context guard: session %s compressed (deep=%s) "
                        "before sending: %d -> %d estimated prompt tokens "
                        "(budget %d, %d messages)",
                        session_id, deep, previous_tokens, current_tokens,
                        budget, len(current),
                    )
                    if current_tokens <= budget:
                        break

        if current_tokens > budget:
            offloaded, current_tokens = self._offload_oversized(
                current, budget, estimate, estimate_llm_tokens,
            )
            if offloaded is not None:
                current, replaced = offloaded, True

        return (current if replaced else None), current_tokens

    def _offload_oversized(
        self,
        messages: list,
        budget: int,
        estimate: Callable[[list], int],
        estimate_content: Callable[[object], int],
    ) -> tuple[Optional[list], int]:
        """Replace the largest messages with file references until it fits.

        Only ever reached from the pre-send guard, i.e. only once compression
        has been decided — content is never offloaded because it is merely long.
        The biggest content is offloaded first and the loop stops as soon as the
        request fits, so the number of offloaded messages stays minimal.  The
        Message objects of the loop are never mutated: they are also held by the
        caller's persistence list, and the transcript must keep the verbatim
        tool output.

        ``system`` messages are never candidates: that message is where
        :meth:`ContextManager.assemble_context` carries the agent prompt, the
        rolling summary and the structured memory, so offloading it would spend
        the very compression the guard has just asked for (a real
        ``context guard: refusing to send`` ended with the summary/memory
        message itself spilled to a file, leaving a request that was both
        oversized and amnesic).

        When offloading every oversized message is still not enough — the rest
        of the overflow is spread over many medium turns, and only a handful of
        them ever reach the offload threshold — :meth:`_drop_oldest_for_budget`
        finishes the job instead of refusing the request.
        """
        from dataclasses import replace as dc_replace

        from runtime.context_manager import (
            is_spilled_content,
            spill_reference,
            _CONTEXT_SPILL_MIN_TOKENS,
        )

        candidates: list[tuple[int, int]] = []
        for index, message in enumerate(messages):
            if getattr(message, "images", None):
                continue
            if self._message_role(message) == "system":
                continue
            content = self._message_content(message)
            if not isinstance(content, str) or is_spilled_content(content):
                continue
            size = estimate_content(content)
            if size >= _CONTEXT_SPILL_MIN_TOKENS:
                candidates.append((size, index))
        candidates.sort(reverse=True)

        current = list(messages)
        current_tokens = estimate(current)
        offloaded = 0
        for size, index in candidates:
            if current_tokens <= budget:
                break
            content = self._message_content(current[index])
            label = (
                getattr(current[index], "name", None)
                or (current[index].get("name") if isinstance(current[index], dict) else None)
                or getattr(current[index], "role", None)
                or "content"
            )
            reference = spill_reference(self.session_id or "", content, label=str(label))
            if not reference or reference == content:
                continue
            if dc_replace is not None and not isinstance(current[index], dict):
                current[index] = dc_replace(current[index], content=reference)
            else:
                current[index] = {**current[index], "content": reference}
            offloaded += 1
            current_tokens = estimate(current)
            logger.info(
                "pre-send context guard: session %s offloaded a %d-token %s "
                "message to a file (excerpt of %d chars kept inline)",
                self.session_id, size, label, len(reference),
            )

        if current_tokens > budget:
            trimmed, trimmed_tokens = self._drop_oldest_for_budget(
                current, budget, current_tokens, estimate
            )
            if trimmed is not None:
                current, current_tokens, offloaded = trimmed, trimmed_tokens, offloaded + 1
        return (current if offloaded else None), current_tokens

    def _drop_oldest_for_budget(
        self,
        messages: list,
        budget: int,
        current_tokens: int,
        estimate: Callable[[list], int],
    ) -> tuple[Optional[list], int]:
        """Drop the oldest turns of the window until the request fits.

        The last step of the pre-send guard, reached only after compression
        (shallow and deep) and after every oversized message became a file
        reference.  What is left is a long run of medium messages — typically
        the unsummarized window of a session whose compression is unavailable or
        keeps failing — and no amount of offloading can clear it: a real request
        of 222 messages / ~189 000 tokens had exactly four messages above the
        offload threshold, so the guard offloaded four and refused to send.

        Dropping the oldest messages is lossy, but it is the lesser loss: the
        rolling summary (still in the system message) describes older content,
        ``conversation.json`` still holds the verbatim transcript, and the
        alternative is an inference that never runs at all.  The drop is written
        into the system message so the model is told what it is missing instead
        of silently seeing a shorter past.

        Protocol integrity is preserved: the window never starts on a ``tool``
        result whose ``assistant(tool_calls)`` was dropped, and it keeps at
        least one ``user`` message (providers such as Ollama reject a request
        without one) -- the same two invariants
        :meth:`ContextManager.select_unsummarized_turns` upholds.

        Returns ``(list_or_None, tokens)``: ``None`` when nothing could be
        dropped (a single-message request, or everything already dropped).
        """
        if len(messages) < 2:
            return None, current_tokens

        # A leading system message carries the prompt/summary/memory: it is
        # never dropped, and it is where the omission note is written.
        head = 1 if self._message_role(messages[0]) == "system" else 0

        from runtime.common import (
            estimate_chat_message_tokens,
            get_prompt_token_calibration,
        )

        sizes = [estimate_chat_message_tokens(m) for m in messages]
        measured = max(1, current_tokens)
        walked = max(1, sum(sizes))
        # ``measured`` comes from a walk that stops as soon as the budget is
        # passed, so it can be smaller than the list really is; the model's
        # measured estimate->factor is the other way to know the per-message
        # walk is not yet in the model's own units.
        scale = max(measured / walked, get_prompt_token_calibration(self.inference_model_id))

        def _clean_start(cut: int) -> int:
            """First index >= *cut* that may legally open the window."""
            while cut < len(messages) and self._message_role(messages[cut]) == "tool":
                cut += 1
            return cut

        limit = len(messages) - 1  # the newest message is never dropped
        # Phase 1: walk the per-message estimates, scaled to the model's units,
        # until the projection fits.  Cheap: no full re-estimate per step.
        cut = _clean_start(head)
        dropped_raw = 0.0
        while cut < limit and (walked * scale - dropped_raw) > budget:
            dropped_raw += sizes[cut] * scale
            cut = _clean_start(cut + 1)
        cut = min(cut, limit)

        rebuilt = self._with_omission_note(messages, head, cut)
        tokens = estimate(rebuilt)
        # Phase 2: the walk above is an approximation; correct it with the
        # caller's own estimator, which is the number the request is judged by.
        # The step is proportional to what is still over, so a window of
        # hundreds of messages is cleared in a handful of re-measures.
        average = max(1, int(sum(sizes[head:]) * scale // max(1, len(messages) - head)))
        while tokens > budget and cut < limit:
            step = min(limit - cut, max(1, (tokens - budget) // average))
            cut = min(_clean_start(cut + step), limit)
            rebuilt = self._with_omission_note(messages, head, cut)
            tokens = estimate(rebuilt)
        if tokens > current_tokens or len(rebuilt) >= len(messages):
            return None, current_tokens

        logger.warning(
            "pre-send context guard: session %s dropped %d of its oldest messages "
            "(~%d tokens) to reach the %d-token budget: compression did not free "
            "enough, and the full transcript stays in conversation.json",
            self.session_id, len(messages) - len(rebuilt),
            sum(sizes[head:cut]), budget,
        )
        return rebuilt, tokens

    def _with_omission_note(self, messages: list, head: int, cut: int) -> list:
        """Rebuild ``messages`` without its oldest *cut*, and say so.

        The two protocol invariants are applied BEFORE the note is written, and
        a rebuild that they cancel out (the whole window was user anchors, so
        nothing actually left the context) is returned verbatim: a note costing
        tokens for a drop that did not happen would only push the request
        further over its budget.
        """
        from dataclasses import replace as dc_replace

        kept = messages[0:head] + list(messages[cut:])

        # Never leave a request without a user message (see the docstring):
        # put back the newest dropped one, right after the system message.
        if not any(self._message_role(m) == "user" for m in kept):
            for index in range(cut - 1, head - 1, -1):
                if self._message_role(messages[index]) == "user":
                    kept.insert(1, messages[index])
                    break

        dropped = len(messages) - len(kept)
        if dropped <= 0:
            return list(messages)

        note = (
            f"\n\n[{dropped} oldest message(s) of this session were dropped from "
            f"the context to fit the model's window; the rolling summary above "
            f"covers them, and the verbatim transcript remains in "
            f"conversation.json of session {self.session_id}]"
        )
        if head == 1:
            first = kept[0]
            content = self._message_content(first) or ""
            if isinstance(first, dict):
                kept[0] = {**first, "content": f"{content}{note}"}
            else:
                kept[0] = dc_replace(first, content=f"{content}{note}")
        else:
            kept.insert(0, {"role": "system", "content": note.strip()})
        return kept

    @staticmethod
    def _message_role(message: object) -> str:
        if isinstance(message, dict):
            return str(message.get("role") or "")
        return str(getattr(message, "role", "") or "")

    @staticmethod
    def _message_content(message: object) -> object:
        if isinstance(message, dict):
            return message.get("content")
        return getattr(message, "content", None)

    def _rebuild_from_disk(self) -> Optional[list]:
        """Re-assemble the in-flight context the way a new turn is assembled."""
        try:
            assembled = self.context_manager.assemble_context(self.session_id, [])
        except OSError as exc:
            logger.warning(
                "in-loop context compression: failed to rebuild the context for "
                "session %s: %s",
                self.session_id,
                exc,
            )
            return None
        if not assembled:
            return None
        from runtime.models import Message

        return [Message.from_dict(m) for m in assembled]




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
# Maps session_id -> status string: "streaming" | "paused" | "done_success_unread" | "done_error_unread"
# "paused" is a resting soft-stop state reached at an inference round boundary (see
# take_infer_boundary_action); the conversation on disk is complete and a later
# inference continues from it like any other next round.
# "idle" state means session is neither active nor unread (i.e. viewed or just completed-and-read).
_session_statuses: dict[str, str] = {}
# Set of session_ids that have completed but whose result has not been "viewed" yet.
# Entries are status strings: "done_success_unread" or "done_error_unread".
_unread_sessions: dict[str, str] = {}
# List of subscriber callbacks for session-event SSE connections.
# Each entry is a callable(data: dict) -> bool; returns False if write failed (caller removes).
_session_event_subscribers: list = []
# Optional callback (registered by runtime/server.py once the SessionManager is
# ready): session_id -> {"title": ..., "title_given": ...} | None.
# Terminal done_* status events use it to attach the canonical title from
# index.json. By the time a done status is broadcast, final persistence
# (including any auto title generation) has already completed, so the index
# title is the final title and clients can restore the sidebar title from the
# event alone — no extra list request needed.
_session_title_provider: Optional[Callable[[str], Optional[dict]]] = None
# Lock protecting all the above shared state.
_session_state_lock = threading.Lock()


def set_session_title_provider(provider: Optional[Callable[[str], Optional[dict]]]) -> None:
    """Register the callback used to attach canonical session titles to SSE events."""
    global _session_title_provider
    _session_title_provider = provider


def snapshot_session_titles(session_ids) -> dict:
    """Return ``{sid: {"title": ..., "title_given": ...}}`` for the given sessions.

    Only sessions present in the index with a non-empty title are returned.
    Returns an empty dict when no provider is registered yet.
    """
    titles: dict = {}
    if _session_title_provider is None:
        return titles
    for sid in session_ids:
        try:
            info = _session_title_provider(sid)
        except Exception:
            info = None
        if isinstance(info, dict) and str(info.get("title") or ""):
            titles[sid] = {"title": str(info["title"]), "title_given": bool(info.get("title_given"))}
    return titles

# Per-session inference message broker. Frames are retained for the lifetime of
# the inference and fanned out to every browser that has opened the session.
#
# An inference belongs to the session, never to a connection. It ends when the
# user stops it, when the work finishes, or when this process shuts down -- and
# never because a socket went away. Which connections happen to be open decides
# who *sees* the frames, nothing else. There is deliberately no "cancel because
# nobody is watching" path: every interruption incident of the past was such a
# path, because a browser dropping a long-lived request is routine (tab hidden,
# proxy restart, HTTP/1.1 per-origin limit, reload) and reads identical to a
# client that has left for good.
_session_stream_lock = threading.RLock()
_session_streams: dict[str, dict] = {}


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


#: Persistence hook for the "this session is inferring" marker, wired by
#: ``RuntimeHTTPServer`` to ``SessionManager.set_session_inferring``. The broker
#: deliberately does not know where index.json lives; it only announces that a
#: generation started or reached its terminal state, and the storage layer writes
#: it down. That marker is what lets a fresh instance tell "stopped on shutdown"
#: apart from "killed mid-run" (see ``fail_inferring_sessions``).
_inferring_sink = None


def set_inferring_sink(sink) -> None:
    """Install the persistence hook: ``sink(session_id, inferring: bool)``."""
    global _inferring_sink
    _inferring_sink = sink


def _note_inferring(session_id: str, inferring: bool) -> None:
    """Announce a generation boundary to the persistence hook (never fatal)."""
    sink = _inferring_sink
    if sink is None or not session_id:
        return
    try:
        sink(session_id, inferring)
    except Exception as exc:  # noqa: BLE001 - persistence is never the run's problem
        logging.getLogger("runtime.server").warning(
            "session_stream inferring_marker_failed sid=%s inferring=%s: %s",
            session_id, inferring, exc,
        )


def seed_interrupted_status(session_ids) -> None:
    """Give sessions an unclean instance left behind a terminal status.

    A generation cannot outlive its process, so a session whose persisted marker
    still says "inferring" has failed. Marking it ``done_error_unread`` is what
    makes the sidebar show a completed-with-error session instead of one that is
    forever streaming, and what keeps the resume gate from treating it as active.
    """
    if not session_ids:
        return
    with _session_state_lock:
        for session_id in session_ids:
            _session_statuses[session_id] = "done_error_unread"
            _unread_sessions[session_id] = "done_error_unread"


def begin_session_stream(session_id: str, cancel_event=None):
    """Open a new inference generation, unless the session already has a live one.

    This is the moment the session becomes "推理中" in the durable sense: from
    here on the run is owned by this process and recorded in index.json, whether
    or not any browser is attached.

    A session has exactly one generation at a time, and the way to end one is to
    ask it to end: the user's stop or pause, letting it run to completion, or
    process shutdown. Arriving with another request is not a way to end one.  So
    while a generation is live -- an owner event that has neither finished nor
    been asked to stop -- this call refuses and leaves that generation completely
    untouched: its broker, retained frames, ``active_infers`` slot and durable
    推理中 marker all stay its.

    Refusing is the non-destructive half of what superseding used to do, and it
    matters more than the cancelling it replaces. Replacing a live broker takes
    over the one ``active_infers`` slot the session has, so from then on
    ``/v1/infer/abort`` -- stop, force, pause, interjection -- can only ever
    reach the newcomer; the run it displaced keeps issuing model requests and
    executing tools (which mutate the workspace) for its whole remaining round
    budget, invisible and unreachable, because only its writes to
    conversation.json were gated by generation ownership. A session that looked
    idle was therefore still spending model calls and editing files, with nothing
    left to stop it.

    Returns ``None`` when this call opened the generation, or the live
    generation's ``cancel_event`` when the request was refused -- the caller must
    answer without having taken anything.  A finished broker, or one whose run
    has already been asked to stop, is replaced silently: nothing gets
    interrupted, and the outgoing run's own cleanup is gated on its identity, so
    it cannot write over this one.
    """
    with _session_stream_lock:
        previous = _session_streams.get(session_id)
        if previous is not None:
            previous_event = previous.get("cancel_event")
            if (
                previous_event is not None
                and not previous.get("done")
                and not previous_event.is_set()
            ):
                logging.getLogger("runtime.server").warning(
                    "session_stream busy_refused sid=%s: this generation owns "
                    "the session and was left running; the request that reached "
                    "it was refused instead of taking the session over (stop or "
                    "pause first if that was the intent)",
                    session_id,
                )
                return previous_event
        previous_subscribers = list(previous["subscribers"]) if previous else []
        _session_streams[session_id] = {
            "next_seq": 1,
            "persisted_seq": 0,
            "frames": [],
            "subscribers": [],
            "cancel_event": cancel_event,
            "done": False,
            # Set by the tool loop at the moment it decides to finish with an
            # empty interjection buffer; the interject endpoint refuses
            # anything after that point instead of buffering it forever.
            "loop_closing": False,
        }
    _note_inferring(session_id, True)
    # Wake retained GET handlers attached to the generation this one replaced --
    # only ever a finished or already-stopping one, since a live generation
    # refuses the replacement above.  Their UI observes the authoritative
    # streaming status and reconnects to this broker.
    for send_fn in previous_subscribers:
        try:
            send_fn(None)
        except Exception:
            pass
    return None


def session_stream_is_live(session_id: str) -> bool:
    """True while a generation owns this session and can still be stopped.

    The predicate serves the request gate: a new request that reached a session
    whose run is live would take that session over and leave the run unstoppable,
    so the gate answers 409 for it -- streamed and non-streamed alike. A broker
    nobody can stop -- no owner event at all, or one already set -- is not a live
    run, and holding a session shut for it would leave no way out but a restart.
    """
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is None or stream.get("done"):
            return False
        event = stream.get("cancel_event")
        return event is not None and not event.is_set()


def publish_session_stream_frame(
    session_id: Optional[str],
    frame: dict,
    owner_event=None,
    event: Optional[str] = None,
    create_if_absent: bool = True,
) -> Optional[int]:
    """Publish a retained frame for the current inference generation.

    ``session_id`` can be reused immediately after a force-abort. In that case
    the old handler may still unwind while the replacement inference is already
    producing output. ``owner_event`` prevents that stale handler from
    publishing into the replacement stream's broker.

    ``create_if_absent=False`` refuses to invent a generation: the frame is
    dropped (``None``) when no live broker exists for the session.  Control
    frames emitted from outside an inference (e.g. a manual summary
    regeneration) use that, because a phantom stream would report a starter
    that never connected and keep its frames retained for nothing.

    The same refusal applies to an ownerless frame even when
    ``create_if_absent`` is left on. A broker is the record of a *generation*,
    so it needs the run's identity to be bound to: without an ``owner_event`` it
    cannot be stopped (abort reaches a run through its event), its frames are
    retained for nobody, and the session is marked "推理中" in index.json for a
    run that does not exist -- which the next instance would read as a crash.
    """
    if not session_id:
        return 0
    debug_info = None
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is None:
            if not create_if_absent or owner_event is None:
                return None
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
            # Capture the fields under the lock but log only after release:
            # this is the per-frame hot path and logging may block on a slow
            # stderr; it must not hold the per-frame lock.
            debug_info = (
                seq,
                stream["persisted_seq"],
                len(subscribers),
                _session_stream_frame_summary(frame),
            )
    if debug_info is not None:
        logging.getLogger("runtime.server").warning(
            "session_stream publish sid=%s seq=%s persisted=%s subscribers=%s %s",
            session_id, *debug_info,
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
                if len(current["subscribers"]) != previous_count and _session_stream_debug_enabled():
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


#: ``status`` value of the context-compression control frame.  ``role=status``
#: keeps it out of the conversation (no consumer treats it as a turn, and it is
#: never persisted), exactly like the client-side-tool handoff frame.
COMPRESSION_STATUS = "context_compression"


def compression_stream_frame(session_id: str, payload: dict) -> dict:
    """Build the SSE control frame for one compression lifecycle event."""
    return {
        "role": "status",
        "source": "system",
        "status": COMPRESSION_STATUS,
        "session_id": session_id,
        **payload,
    }


def broadcast_compression_event(session_id: str, payload: dict) -> None:
    """Deliver one context-compression lifecycle event to this session's clients.

    Installed as the compression event sink of
    :class:`runtime.context_manager.ContextManager`
    (:meth:`ContextManager.set_compression_event_sink`), which calls it twice
    per attempt that reaches the model: ``payload["phase"]`` ``"started"`` just
    before the request goes out, ``"completed"`` with the outcome afterwards.

    Routing follows the way a tool call reaches its clients, because to the
    user a compression is exactly that -- something inside their running
    generation that takes seconds and has to appear between the frames before
    and after it, in order.  Three situations, in that spirit:

    1. the compression runs on the thread of the inference that owns the
       session (the ordinary case: post-turn, mid-loop and pre-send triggers):
       use that handler's SSE callback, which publishes into the retained
       broker and mirrors to the starter connection;
    2. a worker thread of a live generation whose own callback belongs to a
       different session (a group-chat agent, a nested tool): publish into the
       session's existing broker, and never create one;
    3. no generation at all (``regenerate-summary``, forking an oversized
       history): fall back to the global session-events channel, which every
       tab already watches for status and title updates.

    Best effort in every case: an undeliverable event must never disturb the
    compression it describes, and a stateless inference (no ``session_id``)
    simply has nowhere to deliver to.
    """
    if not session_id:
        return
    frame = compression_stream_frame(session_id, payload)
    callback = get_request_context("sse_callback")
    if callback is not None and get_request_context("session_id") == session_id:
        try:
            # ``False`` means this handler was superseded by a newer inference:
            # its generation is no longer retained, and pushing the event into
            # the replacement stream would date that one with an event of the
            # old run.  Drop it; the replacement compresses on its own terms.
            callback(frame)
            return
        except Exception as exc:  # noqa: BLE001 - observability is never fatal
            logger.debug(
                "compression event: starter callback failed for session %s: %s",
                session_id, exc,
            )
    if publish_session_stream_frame(
        session_id, frame, create_if_absent=False
    ) is not None:
        return
    _broadcast_session_event(session_id, "compression", dict(payload))


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
    # The generation reached its end here (completed, stopped, or force-aborted):
    # the durable "推理中" marker goes away with it, so a later instance can tell
    # a finished run apart from one whose process was killed.
    _note_inferring(session_id, False)
    return True


def transition_session_stream_status(
    session_id: str,
    owner_event,
    status: str,
) -> bool:
    """Set/broadcast status only for the current inference generation.

    The owner check, state mutation and event enqueue are serialized against
    ``begin_session_stream`` (all under ``_session_stream_lock``). This
    guarantees that an old request cannot enqueue ``done_error_unread`` after a
    newer request for the same session has already announced ``streaming``.

    The SSE frame itself — canonical title lookup (a disk read of index.json
    for done_* statuses) plus JSON encoding — is built *before* the locks are
    taken, and the enqueue is a non-blocking queue put, so the critical
    section performs no I/O and no serialization work.
    """
    # Built without holding any lock: for done_* statuses this reads the
    # session title from index.json. Persistence (including auto title
    # generation) has already completed before this transition is reached, so
    # the title is final whether it is read before or after the mutation.
    frame = _build_session_status_frame(session_id, status)
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
        # send_fn implementations are non-blocking queue puts, so no I/O
        # happens here.
        _enqueue_session_event_frame(frame)
        return True


def get_session_status(session_id: str) -> Optional[str]:
    """Return the live in-memory status of a session, if any.

    Values: ``streaming`` / ``paused`` / ``done_success_unread`` /
    ``done_error_unread`` / ``idle`` (set by mark-read), else ``None``.
    Exposed so list APIs can hand the frontend an authoritative per-session
    status instead of leaving the UI to infer it from SSE timing alone.
    """
    with _session_state_lock:
        return _session_statuses.get(session_id)


def seed_paused_status(session_id: str, agent_ids=None) -> bool:
    """Re-arm the resting ``paused`` status from a persisted pause sidecar.

    A soft pause persists ``resume_state.json`` in the session directory, so
    the resting state survives an agentsvc restart without any startup scan:
    the status is recognised LAZILY, one session at a time, when that session
    is actually used (opened in the UI via /v1/infer/pause-status, ended via
    abort action=end; resume reads the sidecar directly).

    No generation can own the session in these paths' resting assumption, but
    a live stream or a ``streaming`` status still wins (return False) so this
    can never resurrect a paused marker over an active generation. On success
    the standard status event is broadcast like a natural pause exit.
    """
    frame = _build_session_status_frame(session_id, "paused")
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is not None and not stream.get("done"):
            return False
        with _session_state_lock:
            if _session_statuses.get(session_id) == "streaming":
                return False
            already = _session_statuses.get(session_id) == "paused"
            _session_statuses[session_id] = "paused"
            _unread_sessions.pop(session_id, None)
        if not already:
            _enqueue_session_event_frame(frame)
    return True


def end_paused_session_status(session_id: str) -> bool:
    """Formally finish a paused session without resuming it.

    A soft pause is a resting state, not a completed answer; the user must
    also be able to abandon it. Ending keeps the persisted conversation
    exactly as it is and moves the session status ``paused`` ->
    ``done_success_unread`` (history complete == natural completion), then
    broadcasts the event like any terminal transition. Returns False when the
    session is not paused (already resumed/ended) or when a live generation
    currently owns the session.
    """
    frame = _build_session_status_frame(session_id, "done_success_unread")
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is not None and not stream.get("done"):
            # A generation is (re)running: its own terminal transition wins.
            return False
        with _session_state_lock:
            if _session_statuses.get(session_id) != "paused":
                return False
            _session_statuses[session_id] = "done_success_unread"
            _unread_sessions[session_id] = "done_success_unread"
        if stream is not None:
            # Drop any stale pause/interjection control with the resting state.
            stream["pause_requested"] = False
            if stream.get("pending_interjections"):
                stream["pending_interjections"].clear()
    _enqueue_session_event_frame(frame)
    return True


# ---------------------------------------------------------------------------
# Inference pause / interjection control (soft-stop at round boundaries)
# ---------------------------------------------------------------------------


def request_pause_infer(session_id: str, owner_event) -> bool:
    """Ask the current inference generation to stop at its next round boundary.

    The generator exits cleanly after the previous round fully completed and
    was incrementally persisted, so conversation.json remains a valid context
    for a later continuation. Returns False when no generation owned by
    ``owner_event`` is running (unknown/superseded/finished stream) — callers
    treat that as a no-op (the inference already ended on its own).
    """
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if (
            stream is None
            or stream.get("cancel_event") is not owner_event
            or stream["done"]
        ):
            return False
        stream["pause_requested"] = True
        return True


# Hard cap on buffered interjections per generation. Messages are memory-only
# until consumed at a round boundary; the cap only guards against unbounded
# buffering. Individual message length is intentionally unrestricted.
_MAX_PENDING_INTERJECTIONS = 10


def set_session_stream_agents(session_id: str, owner_event, agent_ids) -> None:
    """Record the participating agents of the current generation.

    Pause and interjection are single-agent features in phase 1; the abort
    and interject endpoints run in different requests and need the roster
    that belongs to the running generation, not to some later request.
    """
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is None or (owner_event is not None and stream.get("cancel_event") is not owner_event):
            return
        stream["agent_ids"] = list(agent_ids or [])


def queue_infer_interjection(
    session_id: str, owner_event, message: dict
) -> Optional[str]:
    """Buffer one user message for the current generation's next boundary.

    Returns "queued", "full", "closing", or None when there is no live
    generation owned by ``owner_event``. The message is *not* published to the
    broker here; ``take_infer_boundary_action`` consumes it and the inference
    loop yields it, so publishing, persistence ordering and multi-agent stream
    merging all keep their existing invariants.

    ``"closing"`` means the generation's tool loop already reached its end
    decision with an empty buffer (see ``try_close_infer_loop``): no boundary
    will ever come, so the caller must refuse the message and let the client
    re-send it as a normal turn rather than accept a turn that never runs.
    """
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if (
            stream is None
            or stream.get("cancel_event") is not owner_event
            or stream["done"]
        ):
            return None
        if owner_event is not None and owner_event.is_set():
            # Abort wins over interjection: a cancelling generation has no
            # next boundary either.
            return None
        if stream.get("loop_closing"):
            return "closing"
        pending = stream.setdefault("pending_interjections", [])
        if len(pending) >= _MAX_PENDING_INTERJECTIONS:
            return "full"
        pending.append(message)
        return "queued"


def take_infer_boundary_action(session_id: str, owner_event) -> Optional[dict]:
    """Atomically consume pause/buffer state at an inference round boundary.

    Returns ``None`` when this generation has nothing to apply, otherwise
    ``{"append": [message dicts], "stop": bool}``. A cancel-pending generation
    always returns ``None``: abort wins over pause. Buffered user messages
    (interjections, M2) are consumed in arrival order and continue the loop;
    the soft ``stop`` is only handed out when the pause request is not
    immediately satisfied by such buffered input. When ``stop`` is True the
    exit reason ``"paused"`` is recorded for the handler's finalization.
    """
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if (
            stream is None
            or stream.get("cancel_event") is not owner_event
            or stream["done"]
        ):
            return None
        if owner_event is not None and owner_event.is_set():
            return None
        had_pause = bool(stream.get("pause_requested"))
        buffered = list(stream.get("pending_interjections") or [])
        if not had_pause and not buffered:
            return None
        stream["pause_requested"] = False
        if stream.get("pending_interjections"):
            stream["pending_interjections"].clear()
        stop = had_pause and not buffered
        if stop:
            stream["exit_reason"] = "paused"
        return {"append": buffered, "stop": stop}


def take_infer_interjections(session_id: str, owner_event) -> list:
    """Consume buffered interjections only (never the soft-pause flag).

    Used by the group-chat *trailing* drain after its wave loop has ended:
    interjections still join the shared transcript, but a generation that
    already finished on its own must not be flipped into "paused" this late
    (natural completion wins the race, same decision as the single-agent
    path). Wave-boundary consumption inside the loop uses
    ``take_infer_boundary_action`` instead, which honours pause as well.
    Returns an empty list when there is nothing buffered for this generation.
    """
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if (
            stream is None
            or stream.get("cancel_event") is not owner_event
            or stream["done"]
        ):
            return []
        if owner_event is not None and owner_event.is_set():
            return []
        buffered = list(stream.get("pending_interjections") or [])
        if buffered and stream.get("pending_interjections") is not None:
            stream["pending_interjections"].clear()
        return buffered


def try_close_infer_loop(session_id: str, owner_event) -> bool:
    """Ask whether the tool loop may finish; atomic against an interjection.

    Called by the inference loop at the exact point where it would end — the
    last model round produced no tool call.  Exactly one of two outcomes is
    decided under ``_session_stream_lock``, so a message that arrives in the
    same instant as the loop's final round is never lost:

    * ``False`` — a user message is already buffered.  The turn is not over:
      the loop must run one more round, and the message is left in place for
      that round's boundary gate (``take_infer_boundary_action``) to consume
      like any other interjection.  Nothing is marked, so later messages stay
      acceptable.
    * ``True`` — the buffer is empty.  The generation is marked closing, which
      makes every later interjection return ``"closing"`` and get refused by
      the endpoint (the client then sends it as a normal turn).  A soft-pause
      still pending here is moot — the answer completed — and is dropped with
      the resting state.

    Unknown / superseded / finished generations report ``True``: the loop may
    end, and the endpoint already refuses those cases.
    """
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if (
            stream is None
            or stream.get("cancel_event") is not owner_event
            or stream["done"]
        ):
            return True
        if stream.get("pending_interjections"):
            return False
        stream["loop_closing"] = True
        stream["pause_requested"] = False
        return True


def close_infer_loop(session_id: str, owner_event) -> None:
    """Mark a generation closed to interjections, unconditionally.

    Asked once the generation's message source is fully exhausted, before
    finalization (a context compression there can take seconds): a message
    arriving in that window is refused and re-sent as a normal turn instead
    of being accepted into a buffer nothing will ever drain. Loop-exit
    decisions that CAN continue on buffered input use ``try_close_infer_loop``
    instead — both the single-agent tool loop and the group-chat wave loop do.
    """
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if (
            stream is None
            or stream.get("cancel_event") is not owner_event
            or stream["done"]
        ):
            return
        stream["loop_closing"] = True
        stream["pause_requested"] = False


def get_infer_exit_reason(session_id: str, owner_event) -> Optional[str]:
    """Return the recorded terminal reason ("paused") for this generation."""
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is None or stream.get("cancel_event") is not owner_event:
            return None
        return stream.get("exit_reason")


def clear_infer_control(session_id: str, owner_event) -> None:
    """Drop a pending pause/buffer for this generation (abort supersedes it)."""
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is None or stream.get("cancel_event") is not owner_event:
            return
        stream["pause_requested"] = False
        buffered = stream.get("pending_interjections")
        if buffered:
            buffered.clear()


def clear_session_stream_state(session_id: str) -> None:
    """Remove all in-memory stream bookkeeping for a deleted session.

    Drops the broker (including any pending pause / interjection buffers),
    the status and unread markers, and wakes retained SSE handlers. A running
    generation owned by this session is cancelled so its generator unwinds
    instead of persisting into a directory that no longer exists.
    """
    with _session_stream_lock:
        stream = _session_streams.pop(session_id, None)
    with _session_state_lock:
        _session_statuses.pop(session_id, None)
        _unread_sessions.pop(session_id, None)
    if stream is None:
        return
    cancel_event = stream.get("cancel_event")
    if cancel_event is not None:
        cancel_event.set()
    for send_fn in list(stream.get("subscribers") or []):
        try:
            send_fn(None)
        except Exception:
            pass


def get_session_stream_snapshot(session_id: str, after_seq: int = -1) -> dict:
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is None:
            return {"active": False, "done": True, "persisted_seq": 0, "latest_seq": 0, "frames": []}
        # Negative means "conversation.json is the baseline": replay only frames
        # produced after the latest successful incremental persistence.
        effective_after = stream["persisted_seq"] if after_seq < 0 else after_seq
        active = not stream["done"]
        done = stream["done"]
        persisted_seq = stream["persisted_seq"]
        latest_seq = stream["next_seq"] - 1
        # Pointer snapshot only: retained frames are append-only and envelopes
        # are never mutated in place, so the per-frame copies can safely be
        # built outside the lock.
        frames_snapshot = list(stream["frames"])
    return {
        "active": active,
        "done": done,
        "persisted_seq": persisted_seq,
        "latest_seq": latest_seq,
        "frames": [item.copy() for item in frames_snapshot if item["seq"] > effective_after],
    }


def subscribe_session_stream(session_id: str, send_fn) -> bool:
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is None or stream["done"]:
            return False
        stream["subscribers"].append(send_fn)
        return True


def unsubscribe_session_stream(session_id: str, send_fn) -> None:
    """Drop one subscriber. Releasing the last one changes nothing else.

    The inference keeps running: an observer left, the session did not end.
    """
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is not None:
            try:
                stream["subscribers"].remove(send_fn)
            except ValueError:
                return


def register_session_stream_with_snapshot(session_id: str, send_fn, after_seq: int = -1) -> tuple[bool, dict]:
    """Atomically register a subscriber and take its replay snapshot.

    Registration and the frame *pointer* snapshot are atomic under the lock so
    no frame can fall into the replay/live hand-off gap. The per-frame dict
    copies are built afterwards, outside the lock: retained frames are
    append-only and envelopes are never mutated in place, so a pointer copy
    taken at registration time is a stable snapshot.

    Ordering is a separate matter, and this path only gets it half-way: a frame
    published after registration can reach the writer before the replay has been
    written. The frame channel stages those frames and therefore guarantees
    ``sub_init`` -> replay -> live (see ``frame_channel_subscribe``); this legacy
    per-session reader does not, so a client of it that watches the sequence
    numbers has to re-connect with its cursor when a number jumps -- which is the
    same repair the browser tab does, one connection at a time.
    """
    with _session_stream_lock:
        stream = _session_streams.get(session_id)
        if stream is None:
            return False, {"active": False, "done": True, "persisted_seq": 0, "latest_seq": 0, "frames": []}
        registered = not stream["done"]
        if registered:
            stream["subscribers"].append(send_fn)
        effective_after = stream["persisted_seq"] if after_seq < 0 else after_seq
        done = stream["done"]
        persisted_seq = stream["persisted_seq"]
        latest_seq = stream["next_seq"] - 1
        frames_snapshot = list(stream["frames"])
        if _session_stream_debug_enabled():
            debug_info = (after_seq, effective_after, registered, persisted_seq,
                          latest_seq, len(stream["subscribers"]))
        else:
            debug_info = None
    replay = [item.copy() for item in frames_snapshot if item["seq"] > effective_after]
    if debug_info is not None:
        logging.getLogger("runtime.server").warning(
            "session_stream register sid=%s requested_after=%s effective_after=%s active=%s "
            "persisted=%s latest=%s replay=%s subscribers=%s",
            session_id, *debug_info, len(replay),
        )
    return registered, {
        "active": registered,
        "done": done,
        "persisted_seq": persisted_seq,
        "latest_seq": latest_seq,
        "frames": replay,
    }


# ---------------------------------------------------------------------------
# Per-tab multiplexed frame channels (connection budget)
# ---------------------------------------------------------------------------
# A browser tab used to hold one HTTP connection per streaming session. Together
# with the status hub and the terminal WebSockets that easily exhausted the
# browser's 6-connections-per-origin budget, after which every ordinary request
# queued *in the browser* and the backend merely looked hung.
#
# A channel carries the retained + live frames of *every* session one tab
# watches, over a single SSE connection. It registers one subscriber per watched
# session with the existing per-session broker. The channel is purely a delivery
# path: losing it stops delivery for that tab and has no effect on the inference
# itself, which keeps running and keeps its frames retained for the next reader.
#
# ``conn`` ids are minted by the browser tab. ``get_or_create_frame_channel`` /
# ``attach_frame_channel`` reuse the existing channel when the id is already
# known, so a reconnect re-attaches to the same channel instead of creating a
# second one, and the per-session subscriptions survive the blip.
#
# Lock ordering: ``_frame_channels_lock`` may be taken while then taking
# ``_session_stream_lock`` (subscribe/unsubscribe), never the other way round.
# Channel ``send_fn``s are only ever invoked by the broker *outside*
# ``_session_stream_lock``, so they can safely take this lock.

# How long a *detached* channel object is kept so a reconnecting reader can reuse
# it (same subscriptions, no new generation). Pure resource housekeeping: a
# dropped channel releases its per-session subscriptions, and the reconnecting
# reader re-subscribes with its own cursor and replays the gap from the broker.
# Nothing an inference does depends on this window.
FRAME_CHANNEL_GRACE_S = 2.0
FRAME_CHANNEL_QUEUE_MAXSIZE = 8192
FRAME_CHANNEL_MAX = 64

_frame_channels: dict[str, dict] = {}
_frame_channels_lock = threading.RLock()

# Every attach is handed a fresh number from this process-wide counter rather
# than a per-channel tally that restarts at 1. A channel object is dropped and a
# new one built under the same ``conn`` once its grace expires -- and a reader of
# the *previous* incarnation used to match the new channel's generation 1, mark
# the new connection disconnected and release the subscriptions of a tab that
# was streaming just fine. A token that is never reused makes "is this reader
# the current one" decidable for good.
_frame_channel_generation = 0


def _next_frame_channel_generation() -> int:
    """Take the next channel-generation token (called under the channel lock)."""
    global _frame_channel_generation
    _frame_channel_generation += 1
    return _frame_channel_generation


def _frame_channel_stage(channel: dict, session_id: str) -> list:
    """Start holding this session's frames instead of delivering them."""
    return channel.setdefault("staging", {}).setdefault(session_id, [])


def _frame_channel_flush(channel: dict, session_id: str) -> list:
    """Stop holding this session's frames and return everything that was held.

    Every exit of a (re-)subscribe calls it: a gate left behind would hold that
    session's frames in a list nobody ever reads.
    """
    with _frame_channels_lock:
        staged = channel.get("staging", {}).pop(session_id, None)
    return staged or []


def _frame_channel_push(channel: dict, item: dict) -> bool:
    """Enqueue one channel item. Returns False when the queue is overfull."""
    import queue as _queue
    try:
        channel["queue"].put_nowait(item)
        return True
    except _queue.Full:
        # The broker retains every frame, so a dropped live frame is not lost
        # data -- but it *is* a hole in what this tab sees, and the tab closes
        # it itself: it tracks the sequence it received per session, and a jump
        # re-subscribes that session from the last sequence it applied. Counting
        # the drops here is what makes that repair rate measurable. Returning
        # False would mark this subscriber dead and could cancel an inference
        # that is merely being read slowly, so report success instead.
        channel["dropped"] = int(channel.get("dropped", 0)) + 1
        logging.getLogger("runtime.server").warning(
            "frame_channel queue_full conn=%s sid=%s seq=%s qsize=%s dropped=%s",
            channel.get("id"), item.get("session_id"), item.get("seq"),
            channel["queue"].qsize(), channel["dropped"],
        )
        return True


def _make_frame_channel_send_fn(channel: dict, session_id: str):
    """Bridge one broker subscriber to this channel's outgoing queue.

    While a subscription for this session is being armed, the channel is not
    allowed to deliver for it yet: frames that race in during that hand-off are
    staged instead of overtaking their own ``sub_init`` (see
    :func:`frame_channel_subscribe`).
    """
    def _send(envelope) -> bool:
        if envelope is None:
            item = {"type": "done", "session_id": session_id}
        else:
            item = {
                "type": "frame",
                "session_id": session_id,
                "seq": envelope["seq"],
                "event": envelope.get("event"),
                "frame": envelope["frame"],
            }
        with _frame_channels_lock:
            if envelope is None:
                # The broker uses None to retire a generation / finish the stream.
                channel["sessions"].pop(session_id, None)
            staged = channel.get("staging", {}).get(session_id)
            if staged is not None:
                staged.append(item)
                return True
        return _frame_channel_push(channel, item)
    return _send


def _new_frame_channel_locked(conn_id: str) -> dict:
    import queue as _queue
    channel = {
        "id": conn_id,
        "queue": _queue.Queue(maxsize=FRAME_CHANNEL_QUEUE_MAXSIZE),
        "sessions": {},
        "generation": 0,
        "connected": False,
        "grace_timer": None,
        "created": time.monotonic(),
        "last_seen": time.monotonic(),
        # An attached SSE reader. One conn can legitimately have more than one
        # (two tabs sharing a sessionStorage conn id, or a proxy that keeps the
        # upstream of a replaced request open). Whichever one dies must not
        # declare the tab gone while another is still streaming frames.  It is
        # also the definition of "this channel can deliver": subscriptions are
        # refused while it is zero.
        "handlers": 0,
        # Set when the channel is removed from the registry, so the reader that
        # still holds a reference to this object stops writing instead of
        # pumping keepalives into a browser that takes them for a live channel.
        "closed": False,
        # session_id -> frames held back while that subscription is being armed.
        # Empty except during the hand-off of a (re-)subscribe.
        "staging": {},
    }
    _frame_channels[conn_id] = channel
    # Bound the registry: evict the least recently seen disconnected channels.
    if len(_frame_channels) > FRAME_CHANNEL_MAX:
        stale = sorted(
            (c for c in _frame_channels.values() if not c["connected"]),
            key=lambda c: c["last_seen"],
        )
        for victim in stale[: len(_frame_channels) - FRAME_CHANNEL_MAX]:
            _drop_frame_channel_locked(victim["id"], victim, cause="channel_limit")
    # No grace timer at birth: only an attaching reader creates a channel now, so
    # there is no half-built channel that could leak a subscription nobody reads.
    return channel


def _frame_channel_grace_s() -> float:
    raw = os.environ.get("SESSION_FRAME_CHANNEL_GRACE_S", "")
    if not raw.strip():
        return FRAME_CHANNEL_GRACE_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return FRAME_CHANNEL_GRACE_S


def _arm_frame_channel_grace(conn_id: str, generation: int) -> None:
    """Schedule teardown of a detached channel unless it re-attaches."""
    channel = _frame_channels.get(conn_id)
    if channel is None:
        return
    timer = channel.get("grace_timer")
    if timer is not None:
        timer.cancel()
        channel["grace_timer"] = None

    def _expire() -> None:
        with _frame_channels_lock:
            current = _frame_channels.get(conn_id)
            if current is None or current["connected"]:
                return
            if current["generation"] != generation:
                return
            if current["handlers"] > 0:
                return              # another reader of this conn is still alive
            _drop_frame_channel_locked(conn_id, current)

    timer = threading.Timer(_frame_channel_grace_s(), _expire)
    timer.daemon = True
    channel["grace_timer"] = timer
    timer.start()


def _drop_frame_channel_locked(conn_id: str, channel: dict, cause: str = "grace_expired") -> None:
    """Remove the channel and every per-session subscription it holds."""
    timer = channel.get("grace_timer")
    if timer is not None:
        timer.cancel()
        channel["grace_timer"] = None
    # Tell the reader to stop first. Left to its own devices it exits only when a
    # write fails, so it used to go on pumping keepalives out of an object that is
    # no longer the registry's channel -- and the browser reads those bytes as a
    # healthy connection (observed: 26-29s, once 20 minutes). The flag also covers
    # the case of a full queue dropping the wake-up.
    channel["closed"] = True
    _frame_channel_push(channel, {"type": "close", "generation": channel["generation"],
                                  "reason": cause})
    # Release every per-session subscription this channel held. The inferences
    # behind them are untouched: they keep running and keep their frames
    # retained, which is what makes "open that conversation again" work.
    channel["connected"] = False
    released = sorted(channel["sessions"])
    # How long the transport had already been gone: the difference between
    # "the browser never came back" and "the page asked us to stop".
    detached_for = time.monotonic() - float(channel.get("last_seen") or time.monotonic())
    for session_id, send_fn in list(channel["sessions"].items()):
        channel["sessions"].pop(session_id, None)
        try:
            unsubscribe_session_stream(session_id, send_fn)
        except Exception:
            pass
    if _frame_channels.get(conn_id) is channel:
        _frame_channels.pop(conn_id, None)
    # Without this line a dropped channel is invisible in the log, and its
    # released subscriptions look like a spontaneous inference cancellation.
    logging.getLogger("runtime.server").info(
        "frame_channel dropped conn=%s generation=%s cause=%s handlers=%s "
        "released=%s detached_for=%.1fs",
        conn_id, channel.get("generation"), cause, channel.get("handlers"),
        released, detached_for,
    )


def get_or_create_frame_channel(conn_id: str) -> dict:
    """Return the channel for ``conn_id``; create it when it does not exist."""
    with _frame_channels_lock:
        channel = _frame_channels.get(conn_id)
        if channel is None:
            channel = _new_frame_channel_locked(conn_id)
        else:
            channel["last_seen"] = time.monotonic()
        return channel


def attach_frame_channel(conn_id: str) -> tuple[dict, int]:
    """Attach an SSE connection to a channel, creating it when absent.

    Re-attaching with the same ``conn`` id reuses the channel and its
    subscriptions. Frames queued while detached are discarded: the client
    re-subscribes every session with its own cursor and the broker replays the
    gap, which is both simpler and authoritative.
    """
    with _frame_channels_lock:
        channel = _frame_channels.get(conn_id)
        if channel is None:
            channel = _new_frame_channel_locked(conn_id)
        reattach = bool(channel["generation"] > 0 or channel["sessions"])
        channel["generation"] = generation = _next_frame_channel_generation()
        channel["connected"] = True
        channel["handlers"] = int(channel.get("handlers", 0)) + 1
        channel["last_seen"] = time.monotonic()
        timer = channel.get("grace_timer")
        if timer is not None:
            timer.cancel()
            channel["grace_timer"] = None
        # Every attach is logged: a cancellation blamed on "nobody is watching"
        # is only credible once the log can show whether the tab ever came back
        # (and how many times) after it first connected.
        logging.getLogger("runtime.server").info(
            "frame_channel attach conn=%s generation=%s reattached=%s "
            "handlers=%s sessions=%s",
            conn_id, generation, reattach, channel["handlers"],
            sorted(channel["sessions"]),
        )
    if reattach:
        import queue as _queue
        drained = 0
        while True:
            try:
                channel["queue"].get_nowait()
                drained += 1
            except _queue.Empty:
                break
        # Wake the previous connection so it stops writing without waiting for
        # its next frame; it recognises its own generation and exits.
        try:
            channel["queue"].put_nowait({"type": "attach_flush", "generation": generation})
        except _queue.Full:
            pass
        if drained:
            logging.getLogger("runtime.server").info(
                "frame_channel reattached conn=%s generation=%s discarded=%s",
                conn_id, generation, drained,
            )
    return channel, generation


def detach_frame_channel(conn_id: str, generation: int, channel: dict | None = None) -> bool:
    """Mark the channel's SSE connection as gone; keep it for the grace window.

    ``channel`` is the object this reader was attached to. Passing it lets a
    reader of a *dropped* incarnation bow out quietly: when the registry's entry
    under the same ``conn`` is a different object, this connection belongs to a
    channel that has already been retired, and decrementing the counters of the
    tab that replaced it would cut a streaming connection short.

    Subscriptions deliberately stay registered for ``FRAME_CHANNEL_GRACE_S`` so a
    reconnect (same tab, same ``conn`` id) lands on the same channel and replays
    its gap instead of re-subscribing from scratch. Releasing them after the
    window is resource housekeeping only: the inferences they carry keep running
    either way, and no cancellation depends on this window.
    """
    with _frame_channels_lock:
        current = _frame_channels.get(conn_id)
        if current is None:
            return False
        if channel is not None and current is not channel:
            logging.getLogger("runtime.server").info(
                "frame_channel detach ignored conn=%s generation=%s "
                "(that channel was retired; %s is the live one)",
                conn_id, generation, current.get("generation"),
            )
            return False
        channel = current
        channel["handlers"] = max(0, int(channel.get("handlers", 0)) - 1)
        owned = channel["generation"] == generation
        if owned:
            channel["connected"] = False
            channel["last_seen"] = time.monotonic()
        if channel["handlers"] > 0:
            # Another reader of this same conn is still streaming (two tabs that
            # share a sessionStorage conn id, or a proxy holding the upstream of
            # a replaced request open). The tab has not left, so the countdown
            # that ends in releasing its subscriptions must not start.
            logging.getLogger("runtime.server").info(
                "frame_channel detached conn=%s generation=%s handlers_left=%s "
                "(another reader still attached) sessions=%s",
                conn_id, generation, channel["handlers"], sorted(channel["sessions"]),
            )
            return False
        logging.getLogger("runtime.server").info(
            "frame_channel detached conn=%s generation=%s grace=%ss sessions=%s",
            conn_id, channel["generation"], _frame_channel_grace_s(),
            sorted(channel["sessions"]),
        )
        # Even when this handler was stale, it may be the last reader: the channel
        # is unattended now and the clock has to run for the current generation.
        _arm_frame_channel_grace(conn_id, channel["generation"])
        return owned


def frame_channel_subscribe(conn_id: str, session_id: str, after_seq: int = -1) -> tuple[bool, dict]:
    """Watch ``session_id`` on this channel and replay its gap frames in order.

    Three things have to reach the reader in exactly this order: ``sub_init``,
    the replay of whatever the tab missed, then live frames. Snapshotting
    atomically is not sufficient on its own -- ``register_session_stream_with_snapshot``
    did that already, and each sequence still arrived exactly once -- but a frame
    published during the hand-off overtook its own ``sub_init`` and landed
    *before* the replay, so the reader saw sequence 7, then 1, 2, … 7. A tab that
    tracks what it has applied (which is how it repairs holes) then mistakes the
    replay for a duplicate and drops the frames it is missing. So frames that
    race in while the hand-off runs are *staged* on the channel and flushed after
    the replay, in sequence order.
    """
    with _frame_channels_lock:
        channel = _frame_channels.get(conn_id)
        # A subscription is a promise that frames will be delivered, and only an
        # attached SSE reader can deliver one. A channel without a reader used to
        # be created on demand here and answer ``registered: true``, which is the
        # answer the client's hand-off gate reads as permission to release the
        # POST that *is* the observer -- and seconds later the detach grace
        # revoked that subscription and started the abandonment window. So a conn
        # is subscribable exactly while its reader is attached, and the client
        # has to open the GET first (``registered: false`` says so).
        attached = channel is not None and channel["handlers"] > 0
        if attached:
            channel["last_seen"] = time.monotonic()
            previous = channel["sessions"].pop(session_id, None)
            _frame_channel_stage(channel, session_id)
    if not attached:
        logging.getLogger("runtime.server").info(
            "frame_channel subscribe refused conn=%s sid=%s (no attached reader)",
            conn_id, session_id,
        )
        snapshot = get_session_stream_snapshot(session_id)
        snapshot["reason"] = "no_reader"
        return False, snapshot
    if previous is not None:
        # A fresh cursor replaces the old subscription (re-subscribe after a
        # retired generation, or a duplicate subscribe from the same tab).
        unsubscribe_session_stream(session_id, previous)

    send_fn = _make_frame_channel_send_fn(channel, session_id)
    try:
        registered, snapshot = register_session_stream_with_snapshot(session_id, send_fn, after_seq)
        with _frame_channels_lock:
            current = _frame_channels.get(conn_id)
            if current is not channel:
                # Torn down concurrently: undo and report as not watched.
                try:
                    unsubscribe_session_stream(session_id, send_fn)
                except Exception:
                    pass
                return False, snapshot
            current["sessions"][session_id] = send_fn
            current["last_seen"] = time.monotonic()
            _frame_channel_push(current, {
                "type": "sub_init",
                "session_id": session_id,
                "active": snapshot["active"],
                "persisted_seq": snapshot["persisted_seq"],
                "latest_seq": snapshot["latest_seq"],
            })
            replayed_through = 0
            for item in snapshot["frames"]:
                _frame_channel_push(current, {
                    "type": "frame",
                    "session_id": session_id,
                    "seq": item["seq"],
                    "event": item.get("event"),
                    "frame": item["frame"],
                })
                replayed_through = max(replayed_through, int(item["seq"]))
            # Everything that was published while the hand-off was running, in
            # sequence order, minus the frames the replay has just carried.
            raced = [item for item in _frame_channel_flush(channel, session_id) if not (
                item.get("type") == "frame" and int(item.get("seq") or 0) <= replayed_through
            )]
            for item in sorted(raced, key=lambda i: int(i.get("seq") or 0)
                               if i.get("type") == "frame" else float("inf")):
                _frame_channel_push(current, item)
            if not registered:
                # Mirrors the immediate ``[DONE]`` of GET /v1/sessions/{id}/stream:
                # this session is not streaming, so the client may finalize now
                # instead of waiting for a frame that will never come.
                _frame_channel_push(current, {"type": "done", "session_id": session_id})
    finally:
        # Whatever happened, this session is delivering again.
        _frame_channel_flush(channel, session_id)
    return registered, snapshot


def frame_channel_unsubscribe(conn_id: str, session_id: str) -> bool:
    """Stop watching one session on this channel (releases its subscription)."""
    with _frame_channels_lock:
        channel = _frame_channels.get(conn_id)
        if channel is None:
            return False
        send_fn = channel["sessions"].pop(session_id, None)
        _frame_channel_flush(channel, session_id)
        channel["last_seen"] = time.monotonic()
    if send_fn is None:
        return False
    unsubscribe_session_stream(session_id, send_fn)
    return True


def close_frame_channel(conn_id: str, reason: str = "close_command") -> bool:
    """Tear the channel down now (the tab closed, hid, or ran out of work).

    ``reason`` is only for the log: it says which page command ended the delivery.
    Tearing a channel down never touches the inference behind it.
    """
    with _frame_channels_lock:
        channel = _frame_channels.get(conn_id)
        if channel is None:
            return False
        # Reached only on an explicit browser command (POST /v1/sessions/frames/close):
        # the page asked to stop carrying these sessions without releasing their
        # subscriptions first. The drop line below reports the reason with
        # detached_for ~= 0 (the transport was still live), which separates that
        # from a transport that died and simply never came back.
        _drop_frame_channel_locked(conn_id, channel, cause=reason)
        return True


def frame_channel_snapshot() -> dict:
    """Diagnostics / test helper: channel count, sessions and connection state."""
    with _frame_channels_lock:
        return {
            conn_id: {
                "sessions": sorted(channel["sessions"]),
                "connected": channel["connected"],
                "generation": channel["generation"],
                "queued": channel["queue"].qsize(),
                # Live frames this tab was too slow to take; the tab closes the
                # resulting hole by its cursor, this says how often it has to.
                "dropped": int(channel.get("dropped", 0)),
            }
            for conn_id, channel in _frame_channels.items()
        }


def frame_channel_reset() -> None:
    """Drop every channel (test isolation)."""
    with _frame_channels_lock:
        for conn_id, channel in list(_frame_channels.items()):
            _drop_frame_channel_locked(conn_id, channel)


# ---------------------------------------------------------------------------
# Terminal Sessions – in-memory state
# ---------------------------------------------------------------------------
# Maps terminal_id -> { master_fd, pid, sock, session_id, output_buffer, buffer_lock }
# terminal_id format: "{session_id}" or "{session_id}:{assistant_id}"
_terminal_sessions: dict[str, dict] = {}
_terminal_sessions_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Terminal output buffer: read with cursors, never by draining
# ---------------------------------------------------------------------------
# ``output_buffer`` is the single fan-out target of the terminal's one PTY
# reader (see get_or_create_terminal's read_pty).  exec_cli collectors used to
# ``drain`` it (join + clear), which is destructive: with two calls collecting
# at once, whichever polled first consumed the other's bytes and the loser
# never saw its own output.  Readers therefore take a *cursor* (an absolute
# chunk sequence) and get only what was appended after it:
#
#   output_seq   chunks ever appended (absolute sequence of the buffer end)
#   output_base  sequence of the first chunk still kept in the list
#   read_seq     cursor of the last progress read (empty-command / leftovers)
#   output_chars characters currently retained (trim bound)
#
TERMINAL_BUFFER_MAX_CHUNKS = 4000
TERMINAL_BUFFER_MAX_CHARS = 4_000_000


def _terminal_buffer_fields() -> dict:
    """Fresh output buffer plus its cursor bookkeeping for a terminal entry."""
    return {
        "output_buffer": [],   # decoded PTY chunks, appended by the one reader
        "buffer_lock": threading.Lock(),
        "output_seq": 0,
        "output_base": 0,
        "read_seq": 0,
        "output_chars": 0,
    }


def append_terminal_output(terminal_info: dict, text: str) -> None:
    """Append one decoded PTY chunk and advance the buffer's sequence.

    The list is trimmed from the front instead of being cleared by readers, so
    a collector's cursor keeps pointing at its own bytes; ``output_base`` keeps
    the trimmed list aligned with the absolute sequence.  Nothing clears the
    buffer any more, so the retained window is bounded here (chunk count and
    total characters).
    """
    max_chunks = max(1, env_int("TERMINAL_BUFFER_MAX_CHUNKS",
                                TERMINAL_BUFFER_MAX_CHUNKS))
    max_chars = max(1024, env_int("TERMINAL_BUFFER_MAX_CHARS",
                                  TERMINAL_BUFFER_MAX_CHARS))
    with terminal_info["buffer_lock"]:
        buf = terminal_info.setdefault("output_buffer", [])
        buf.append(text)
        terminal_info["output_seq"] = terminal_info.get("output_seq", 0) + 1
        chars = terminal_info.get("output_chars", 0) + len(text)
        drop = 0
        while drop < len(buf) and (
                len(buf) - drop > max_chunks or chars > max_chars):
            chars -= len(buf[drop])
            drop += 1
        if drop:
            del buf[:drop]
            terminal_info["output_base"] = (
                terminal_info.get("output_base", 0) + drop)
        terminal_info["output_chars"] = chars


def terminal_buffer_end(terminal_info: dict) -> int:
    """Sequence just past the last buffered chunk (a cursor start point)."""
    with terminal_info["buffer_lock"]:
        seq = terminal_info.get("output_seq")
        if seq is None:
            # Hand-built terminal dicts (tests) that only carry a list.
            seq = terminal_info.get("output_base", 0) + len(
                terminal_info.get("output_buffer") or [])
        return seq


def take_terminal_output(terminal_info: dict, cursor: int) -> tuple[str, int]:
    """Return ``(text appended after cursor, new cursor)``.

    Non-destructive for other readers: nothing is cleared, so a concurrent
    collector still gets every chunk that belongs to its own cursor range.
    """
    with terminal_info["buffer_lock"]:
        base = terminal_info.get("output_base", 0)
        buf = terminal_info.get("output_buffer") or []
        seq = terminal_info.get("output_seq")
        if seq is None:
            seq = base + len(buf)
        start = max(int(cursor), base) - base
        text = "".join(buf[start:]) if 0 <= start < len(buf) else ""
        return text, seq


def read_terminal_tail(terminal_info: dict, chunks: int = 200) -> str:
    """Join the last *chunks* buffered chunks (cheap "what's on screen" view)."""
    with terminal_info["buffer_lock"]:
        buf = terminal_info.get("output_buffer") or []
        return "".join(buf[-max(1, chunks):])


# ---------------------------------------------------------------------------
# Terminal lifecycle: usage clock, liveness, reclamation
# ---------------------------------------------------------------------------
# Every session that ever ran one exec_cli call keeps a PTY registered, so the
# registry needs a clock that moves and a reaper that reads it.  It had neither
# for ``{session_id}:auto`` terminals: cleanup only looked at
# ``disconnected_at``, which only the browser-attach path ever sets, so 172
# trials left 38 live PTYs (and their scroll buffers) behind.  Worse, the one
# comparison that did run --
#     now - disconnected_at > os.getenv("TERMINAL_SESSION_TIMEOUT", 3600)
# -- raises TypeError as soon as the variable *is* set (getenv hands back a
# str, now / disconnected_at are floats), which killed the reaper thread for
# the remaining life of the process, after which nothing was ever reclaimed.

#: quiet this long -> reclaimed (an exec_cli terminal never "disconnects")
TERMINAL_IDLE_TIMEOUT = 1800
#: a browser terminal stays registered this long after its tab detached
TERMINAL_SESSION_TIMEOUT = 3600
#: hard ceiling on simultaneously live PTYs, whatever the clocks say
TERMINAL_MAX_SESSIONS = 64
#: never evict over the ceiling a terminal used more recently than this
TERMINAL_EVICTION_GRACE = 120


def _env_timeout(name: str, default: int) -> int:
    """``int(os.getenv(name))``; an unusable value warns and falls back."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; falling back to %s", name, raw, default)
        return default
    if value <= 0:
        logger.warning("Invalid %s=%r (must be > 0); falling back to %s",
                       name, raw, default)
        return default
    return value


def _terminal_lifecycle_fields() -> dict:
    """Fresh usage-clock / exit bookkeeping for a terminal entry."""
    now = time.monotonic()
    return {
        "created_at": now,
        "last_used_at": now,      # moved by exec_cli calls and terminal input
        "exit_code": None,        # set once the shell is known to have exited
        "pty_eof": False,         # the single PTY reader hit EOF / an error
    }


def touch_terminal(terminal_info: Optional[dict]) -> None:
    """Move the terminal's idle clock (call it on every use of the PTY)."""
    if isinstance(terminal_info, dict):
        terminal_info["last_used_at"] = time.monotonic()


def _exit_code_from_status(status: int) -> int:
    """Shell-style exit code from a wait() status word (negative: signalled)."""
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return 0


def _reap_terminal_child(terminal_info: dict) -> tuple[bool, Optional[int]]:
    """Non-blocking wait for the PTY child; records exit_code when it died."""
    pid = terminal_info.get("pid")
    if not pid:
        return True, None
    try:
        waited, status = os.waitpid(int(pid), os.WNOHANG)
    except (ChildProcessError, OSError):
        # Not our child (a test double's pid) or already reaped elsewhere: no
        # evidence, so do not declare a healthy session dead on a guess.
        return True, None
    if waited != int(pid):
        return True, None                     # still running
    code = _exit_code_from_status(status)
    terminal_info["exit_code"] = code
    terminal_info["pty_eof"] = True
    logger.debug("Terminal shell pid=%s exited, code=%s", pid, code)
    return False, code


def terminal_liveness(terminal_info: Optional[dict]) -> tuple[bool, Optional[int]]:
    """Return ``(alive, exit_code)`` for the terminal's shell.

    Cheap on purpose -- one ``waitpid(WNOHANG)``, or ``isalive()`` on Windows --
    because exec_cli runs it before and after every call: a shell that ran
    ``exit`` (or crashed, or lost its container) must surface as an explicit
    ``terminal_exited`` instead of an empty screen once the caller's timeout
    has been spent.

    An inconclusive answer counts as *alive*: hand-built / faked terminal dicts
    carry a pid that was never our child, and there is no point in killing a
    session over a signal we cannot support.
    """
    if not isinstance(terminal_info, dict):
        return False, None

    recorded = terminal_info.get("exit_code")
    if recorded is not None:
        return False, recorded
    if terminal_info.get("pty_eof"):
        # The stream ended; reap the child for its status if nobody has yet.
        _reap_terminal_child(terminal_info)
        return False, terminal_info.get("exit_code")

    proc = terminal_info.get("proc")          # winpty PtyProcess
    if proc is not None:
        try:
            if proc.isalive():
                return True, None
        except AttributeError:
            return True, None
        except Exception as exc:              # cannot tell -> assume alive
            logger.debug("terminal liveness (proc) failed: %s", exc)
            return True, None
        code = getattr(proc, "exit_code", None)
        terminal_info["exit_code"] = code
        terminal_info["pty_eof"] = True
        return False, code

    if not terminal_info.get("pid"):
        return True, None                     # nothing to poll
    return _reap_terminal_child(terminal_info)


def mark_terminal_eof(terminal_info: Optional[dict]) -> None:
    """The PTY stream ended: remember it, and grab the exit code if possible."""
    if not isinstance(terminal_info, dict):
        return
    terminal_info["pty_eof"] = True
    if terminal_info.get("exit_code") is None:
        _reap_terminal_child(terminal_info)


def get_terminal_id_for_session(session_id: str) -> Optional[str]:
    """Registry key of *session_id*'s terminal.

    Retiring a terminal needs its key, not just its dict -- which is why
    ``get_or_create_terminal`` cannot simply overwrite an exited entry.
    """
    with _terminal_sessions_lock:
        for tid, info in _terminal_sessions.items():
            if info.get("session_id") == session_id:
                return tid
    return None


def terminal_is_busy(terminal_info: dict) -> bool:
    """True while an exec_cli call owns this terminal (duck-typed queue)."""
    queue = terminal_info.get("command_queue")
    try:
        return bool(queue and queue.busy())
    except Exception:
        return False


def prune_dead_terminals() -> list[str]:
    """Unregister terminals whose shell has exited; return their ids.

    ``GET /v1/terminals`` answers with this first: an exited PTY must not stay
    in the registry, because the next caller reads it as "a command that
    produced nothing" and waits out its whole timeout.
    """
    dead: list[tuple[str, Optional[int]]] = []
    with _terminal_sessions_lock:
        for tid, info in list(_terminal_sessions.items()):
            alive, code = terminal_liveness(info)
            if not alive:
                dead.append((tid, code))
    for tid, code in dead:
        logger.info("Terminal %s shell exited (exit_code=%s); unregistering",
                    tid, code)
        unregister_terminal_session(tid)
    return [tid for tid, _ in dead]


def unregister_terminals_for_session(session_id: str) -> list[str]:
    """Destroy every terminal that belongs to *session_id* (session destroyed).

    A terminal outlives one inference run but not its session: deleting the
    session must not leave a PTY -- workspace, environment and scroll buffer
    included -- registered until the idle timeout happens to fire.
    """
    if not session_id:
        return []
    ids: list[str] = []
    with _terminal_sessions_lock:
        for tid, info in list(_terminal_sessions.items()):
            if (info.get("session_id") == session_id or tid == session_id
                    or tid.startswith(f"{session_id}:")):
                ids.append(tid)
    for tid in ids:
        unregister_terminal_session(tid)
    return ids


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
            **_terminal_buffer_fields(),
            **_terminal_lifecycle_fields(),
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

    A terminal whose shell has exited (``exit``, a crash, a container that went
    away) is never handed back: it is unregistered -- so ``GET /v1/terminals``
    stops advertising an entry that can only produce empty timeouts -- and a
    fresh PTY is spawned for the same session.  ``rebuilt_from`` on the returned
    dict records that replacement, and the session's initialisation is replayed
    (workspace directory, environment, and the last ``cd`` the terminal asked
    for).
    """
    # Try to find existing terminal -- and check that its shell is still there.
    terminal_info = get_terminal_for_session(session_id)
    replay: Optional[dict] = None
    if terminal_info:
        alive, exit_code = terminal_liveness(terminal_info)
        if alive:
            touch_terminal(terminal_info)
            if send_output is not None:
                terminal_info["send_output"] = send_output
            return terminal_info
        terminal_id = get_terminal_id_for_session(session_id) or f"{session_id}:auto"
        logger.info(
            "Terminal %s shell exited (exit_code=%s); rebuilding it",
            terminal_id, exit_code)
        replay = {
            "from": terminal_id,
            "cwd": terminal_info.get("last_cwd") or "",
            "cols": terminal_info.get("cols") or cols,
            "rows": terminal_info.get("rows") or rows,
            "send_output": terminal_info.get("send_output"),
        }
        unregister_terminal_session(terminal_id)
        if terminal_info.get("sock") is not None:
            # A browser tab was watching this PTY: tell it the channel is gone
            # so a later attach re-opens it instead of writing into a corpse.
            send_fn = terminal_info.get("send_output")
            if callable(send_fn):
                try:
                    send_fn("\r\n[shell exited"
                            f"{'' if exit_code is None else f', exit code {exit_code}'}]\r\n")
                except Exception:
                    pass
    if replay:
        cols, rows = replay["cols"], replay["rows"]
        if send_output is None:
            send_output = replay["send_output"]
    else:
        terminal_id = f"{session_id}:auto"
    session_id_short = session_id.split(":")[0] if session_id else session_id
    # One terminal per session (the key is "{session_id}:auto"), but sessions
    # come and go and every one of them keeps a PTY: keep the registry inside
    # its ceiling instead of letting 172 trials leave 38 shells behind.
    enforce_terminal_capacity()
    
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
            if not os.path.isdir(workspace_dir):
                # Session workspace missing on this host (e.g. a stale
                # parent-side path): start in the inherited cwd instead of
                # failing to spawn. Mirrors the Unix PTY (chdir fallback).
                workspace_dir = os.getcwd()
            proc = PtyProcess.spawn("powershell.exe", dimensions=(rows, cols), cwd=workspace_dir)
            with _terminal_sessions_lock:
                _terminal_sessions[terminal_id] = {
                    "proc": proc,
                    "sock": None,
                    "session_id": session_id_short,
                    "active": True,
                    "disconnected_at": None,
                    **_terminal_buffer_fields(),
                    **_terminal_lifecycle_fields(),
                    "cols": cols,
                    "rows": rows,
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
                                append_terminal_output(terminal_info, data)
                            else:
                                logger.debug("read_pty: EOF for %s (win32)", terminal_id)
                                mark_terminal_eof(terminal_info)
                                break
                        except EOFError:
                            logger.debug("read_pty: EOFError for %s (win32)", terminal_id)
                            mark_terminal_eof(terminal_info)
                            break
                except Exception as e:
                    logger.error("read_pty: unexpected error for %s (win32): %s", terminal_id, e)
                # Reaching here means nobody is draining this PTY any more; if
                # that was because the shell died, say so instead of letting
                # exec_cli wait out its timeout on an empty screen.
                mark_terminal_eof(terminal_info)
            threading.Thread(target=read_pty, daemon=True).start()
            
            _apply_terminal_replay(terminal_info, replay)
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
                    **_terminal_buffer_fields(),
                    **_terminal_lifecycle_fields(),
                    "cols": cols,
                    "rows": rows,
                    "shell_kind": os.path.basename(shell or "/bin/bash"),
                    "send_output": send_output,
                }
            
            # Start background thread to drain PTY output.  This must be the
            # *only* reader of master_fd: os.read() on a shared PTY master
            # hands each chunk to whichever thread wins the race, so a second
            # reader (started on WebSocket attach) silently swallows (or is
            # swallowed by) this one and output stops reaching the browser.
            # Fan the single stream out to the attached browser and to
            # exec_cli's output_buffer, mirroring the win32 reader.
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
                                    text = data.decode(get_system_encoding(), errors='replace')
                                    send_output = terminal_info.get("send_output")
                                    if send_output:
                                        try:
                                            send_output(text)
                                        except Exception:
                                            pass
                                    append_terminal_output(terminal_info, text)
                                else:
                                    logger.debug("read_pty: EOF for %s", terminal_id)
                                    mark_terminal_eof(terminal_info)
                                    break
                        except select.error as e:
                            logger.debug("read_pty: select error for %s: %s", terminal_id, e)
                            mark_terminal_eof(terminal_info)
                            break
                        except OSError as e:
                            logger.debug("read_pty: OSError for %s: %s", terminal_id, e)
                            mark_terminal_eof(terminal_info)
                            break
                except Exception as e:
                    logger.error("read_pty: unexpected error for %s: %s", terminal_id, e)
                # Whoever was reading this PTY is gone.  If the shell ended,
                # record it (with its exit code) so exec_cli can report a dead
                # terminal instead of an empty screen after its timeout.
                mark_terminal_eof(terminal_info)
            threading.Thread(target=read_pty, daemon=True).start()
            
            _apply_terminal_replay(terminal_info, replay)
            return terminal_info
        except Exception as e:
            logger.debug("Failed to create pty terminal: %s", e)
            return None


def _apply_terminal_replay(terminal_info: dict, replay: Optional[dict]) -> None:
    """Tag a rebuilt terminal and replay what the dead one had been asked for.

    Replacing a shell is only useful if the replacement starts where the old
    one left off: the creation path already replays the session's workspace
    directory and environment, and the last ``cwd`` an exec_cli call used is
    replayed here as a startup command.  Best effort -- a fresh shell that
    cannot take the line yet must not fail the rebuild.
    """
    if not replay:
        return
    terminal_info["rebuilt_from"] = replay.get("from") or ""
    cwd = replay.get("cwd") or ""
    if not cwd:
        return
    try:
        if sys.platform == "win32":
            proc = terminal_info.get("proc")
            if proc:
                proc.write(f"Set-Location -LiteralPath '{cwd}'\r")
        else:
            import shlex

            master_fd = terminal_info.get("master_fd")
            if master_fd is not None:
                os.write(master_fd, f"cd {shlex.quote(cwd)}\n".encode("utf-8"))
        terminal_info["last_cwd"] = cwd
        logger.info("Rebuilt terminal %s in %s",
                    terminal_info.get("rebuilt_from"), cwd)
    except Exception as exc:
        logger.debug("Could not replay cwd %r into the rebuilt terminal: %s",
                     cwd, exc)


def enforce_terminal_capacity() -> list[str]:
    """Evict least-recently-used terminals once the ceiling is passed.

    Without a ceiling the registry only ever grows: sessions come and go, and
    every one of them keeps a PTY, its buffers and its prompt state until the
    idle timeout happens to fire.  Attached (browser) and busy (exec_cli)
    terminals, and anything used within the last grace period, are never
    chosen -- so the bound costs nothing to a session that is actually using
    its terminal.
    """
    max_live = _env_timeout("TERMINAL_MAX_SESSIONS", TERMINAL_MAX_SESSIONS)
    grace = _env_timeout("TERMINAL_EVICTION_GRACE", TERMINAL_EVICTION_GRACE)
    now = time.monotonic()
    victims: list[str] = []
    with _terminal_sessions_lock:
        excess = len(_terminal_sessions) - max_live
        if excess > 0:
            idle_candidates: list[tuple[float, str]] = []
            young_candidates: list[tuple[float, str]] = []
            for tid, info in _terminal_sessions.items():
                if info.get("sock") is not None or terminal_is_busy(info):
                    continue
                last_used = info.get("last_used_at") or info.get("created_at") or now
                if now - last_used < grace:
                    young_candidates.append((last_used, tid))
                else:
                    idle_candidates.append((last_used, tid))
            idle_candidates.sort()
            young_candidates.sort()
            victims = [tid for _, tid in idle_candidates[:excess]]
            # A burst of new sessions must not push the registry past the ceiling
            # merely because every entry is young: after the idle ones, the least
            # recently used go too.  Without that, "one trial, one terminal"
            # accumulates one PTY per trial forever -- the leak being fixed here.
            victims += [tid for _, tid in young_candidates[:excess - len(victims)]]
    for tid in victims:
        logger.info("Evicting idle terminal %s (registry over %s terminals)",
                    tid, max_live)
        unregister_terminal_session(tid)
    return victims


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
                # Unix: use os.kill.  Tolerate entries that never finished
                # wiring up (no pid / master_fd): a partially created terminal
                # must still be removable from the registry.
                pid = session.get("pid")
                if isinstance(pid, int):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass

                    # Wait for process to exit (with timeout)
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except (OSError, ChildProcessError):
                        pass

                # Close master fd
                try:
                    master_fd = session.get("master_fd")
                    if isinstance(master_fd, int):
                        os.close(master_fd)
                except OSError:
                    pass


def get_terminal_for_session(session_id: str) -> Optional[dict]:
    """Get terminal session for a given inference session_id."""
    with _terminal_sessions_lock:
        for tid, info in _terminal_sessions.items():
            if info["session_id"] == session_id:
                return info
    return None


def _terminal_reap_interval_s() -> float:
    """How often to sweep: often enough for a 60 s timeout to be honoured."""
    timeouts = [
        _env_timeout("TERMINAL_IDLE_TIMEOUT", TERMINAL_IDLE_TIMEOUT),
        _env_timeout("TERMINAL_SESSION_TIMEOUT", TERMINAL_SESSION_TIMEOUT),
    ]
    return max(5.0, min(60.0, min(timeouts) / 4.0))


def _iso_from_monotonic(value: Optional[float], now_mono: float) -> Optional[str]:
    """Wall-clock stamp for a monotonic reading (ops-facing output)."""
    if value is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S",
                         time.localtime(time.time() - (now_mono - value)))


def terminal_snapshot() -> list[dict]:
    """Ops view of the registry: liveness, usage clock and prompt state.

    ``GET /v1/terminals`` answers with this, so a leak can be spotted from the
    outside -- how long a terminal has been idle, which shell pid holds it, and
    whether that pid is still alive.
    """
    now_mono = time.monotonic()
    snapshot: list[dict] = []
    with _terminal_sessions_lock:
        for tid, info in _terminal_sessions.items():
            alive, exit_code = terminal_liveness(info)
            last_used = info.get("last_used_at")
            snapshot.append({
                "terminal_id": tid,
                "session_id": info.get("session_id", ""),
                "shell_pid": info.get("pid"),
                "alive": alive,
                "exit_code": exit_code,
                "shell_kind": info.get("shell_kind", ""),
                "prompt_pattern": info.get("prompt_pattern") or "",
                "prompt_interpreter": info.get("prompt_interpreter") or "",
                "attached": info.get("sock") is not None,
                "busy": terminal_is_busy(info),
                "created_at": _iso_from_monotonic(info.get("created_at"), now_mono),
                "last_used_at": _iso_from_monotonic(last_used, now_mono),
                "idle_seconds": (round(now_mono - last_used, 1)
                                 if last_used is not None else None),
            })
    return snapshot


def reap_terminal_sessions(now: Optional[float] = None) -> dict:
    """Retire exited, idle, detached and over-capacity terminals.

    Three separate clocks matter and the old sweep only knew one of them:

      * the shell died (``exit``, a crash, a lost container) -> retire at once;
      * nobody touched the terminal for ``TERMINAL_IDLE_TIMEOUT`` seconds --
        the clock an exec_cli (``{session_id}:auto``) terminal answers to, since
        it is never "disconnected" and so used to be reclaimed *never*;
      * a browser terminal whose tab detached for ``TERMINAL_SESSION_TIMEOUT``
        seconds -> retire, as before.

    On top of that the registry is kept inside ``TERMINAL_MAX_SESSIONS``.
    """
    now = time.monotonic() if now is None else now
    idle_timeout = _env_timeout("TERMINAL_IDLE_TIMEOUT", TERMINAL_IDLE_TIMEOUT)
    session_timeout = _env_timeout("TERMINAL_SESSION_TIMEOUT",
                                   TERMINAL_SESSION_TIMEOUT)
    retired: dict[str, list[str]] = {"exited": [], "idle": [], "detached": []}

    with _terminal_sessions_lock:
        for tid, info in list(_terminal_sessions.items()):
            alive, exit_code = terminal_liveness(info)
            if not alive:
                retired["exited"].append(tid)
                continue
            last_used = info.get("last_used_at") or info.get("created_at") or now
            disconnected_at = info.get("disconnected_at")
            if now - last_used > idle_timeout:
                retired["idle"].append(tid)
            elif (disconnected_at
                  and now - disconnected_at > session_timeout):
                retired["detached"].append(tid)

    for reason, ids in retired.items():
        for tid in ids:
            logger.info("Reaping terminal session %s (%s)", tid, reason)
            unregister_terminal_session(tid)
    retired["over_capacity"] = enforce_terminal_capacity()
    return retired


def cleanup_expired_terminal_sessions() -> None:
    """Reclaim terminals that are dead, idle, detached or over the ceiling.

    A pass may not take the thread down: one bad comparison here used to stop
    reclamation for the rest of the process' life (``os.getenv`` returns a str,
    so comparing it against ``now - disconnected_at`` raised TypeError), which
    is how PTYs accumulated across hundreds of trials.  Timeouts are parsed as
    ints and the pass is wrapped.
    """
    while True:
        time.sleep(_terminal_reap_interval_s())
        try:
            reap_terminal_sessions()
        except Exception:
            logger.exception("Terminal reaper pass failed")


# Start cleanup thread
_cleanup_thread = threading.Thread(target=cleanup_expired_terminal_sessions, daemon=True)
_cleanup_thread.start()


def _build_session_event_frame(session_id: str, event_type: str, data: dict) -> str:
    """Build the SSE wire format for a session event (pure CPU work)."""
    payload = {"event": event_type, "session_id": session_id, **data}
    event_payload = json.dumps(payload, ensure_ascii=False)
    return f"data: {event_payload}\n\n"


def _build_session_status_frame(session_id: str, status: str) -> str:
    """Build the SSE frame for a session status change.

    Terminal ``done_*`` events additionally carry the canonical title
    (``title`` / ``title_given``) from index.json.  Persistence (including
    any auto title generation) has already completed before these statuses
    are broadcast, so the frontend can restore the sidebar title directly
    from the event.

    This may perform disk I/O (title lookup) and must therefore be called
    *without* holding ``_session_stream_lock`` / ``_session_state_lock``.
    """
    data = {"status": status}
    if status in ("done_success_unread", "done_error_unread"):
        info = snapshot_session_titles([session_id]).get(session_id)
        if info is not None:
            data["title"] = info["title"]
            data["title_given"] = info["title_given"]
    return _build_session_event_frame(session_id, "message", data)


def _enqueue_session_event_frame(frame: str) -> None:
    """Enqueue a pre-built SSE frame to every global event subscriber.

    The subscriber list is snapshotted under ``_session_state_lock``; the
    ``send_fn`` calls are non-blocking queue puts, so this helper performs no
    I/O and is safe (and cheap) to call while the caller holds
    ``_session_stream_lock`` — the status transition path relies on that to
    keep the enqueue ordered against ``begin_session_stream``.  Dead
    subscribers are removed afterwards, outside the enqueue.
    """
    # Take snapshot of subscriber list under lock
    with _session_state_lock:
        subscribers_snapshot = list(_session_event_subscribers)
    if _session_stream_debug_enabled():
        logging.getLogger("runtime.server").warning(
            "session_events publish frame_len=%s subscribers=%s",
            len(frame), len(subscribers_snapshot),
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


def _broadcast_session_status(session_id: str, status: str) -> None:
    """Broadcast a session status change to all SSE subscribers.

    The frame (including any disk-backed title lookup) is built before the
    subscriber snapshot is taken, so no I/O runs while any session lock is
    held.
    """
    _enqueue_session_event_frame(_build_session_status_frame(session_id, status))


def _broadcast_session_event(session_id: str, event_type: str, data: dict) -> None:
    """Broadcast an arbitrary session event to all SSE subscribers.

    Args:
        session_id: The session this event belongs to.
        event_type: Event type string (e.g. 'message', 'title_update').
        data: Extra key-value pairs merged into the event payload.
    """
    _enqueue_session_event_frame(_build_session_event_frame(session_id, event_type, data))

