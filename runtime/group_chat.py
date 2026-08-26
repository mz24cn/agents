"""Group chat @-mention mechanism for multi-agent conversations.

The core principle mimics human WeChat groups:
- @ mentions deliver immediately to the mentioned agent(s)
- Non-mentioned agents see messages later (delayed visibility — full history
  available when they are next @-mentioned)
- @all broadcasts to all agents in the group
- Without @, continue with the most recent responding agent; if nobody has
  responded yet, prefer participants labelled ``leader``, then fall back to all
- All messages are transparently stored in conversation.json

Design:
    parse_mentions()     — extract @targets from text
    resolve_mentions()   — map mention strings to agent_ids
    assemble_agent_context() — build per-agent inference context with
                                delayed-visibility markers
    run_group_chat_stream()  — parallel multi-agent orchestration (returns list)
    run_group_chat_stream_gen()  — generator version of the above (yields messages)

The generator version (run_group_chat_stream_gen) is the future; the list
version is kept for backward compatibility during the transition.
"""

from __future__ import annotations

import logging
import os
import queue
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Generator, Optional

from runtime.common import (
    get_request_context,
    restore_request_context,
    set_request_context,
    snapshot_request_context,
)
from runtime.models import Message, InferenceRequest

_logger = logging.getLogger("runtime.group_chat")

# SSE keep-alive interval for long group-chat waits. Gateways/proxies/
# firewalls commonly drop idle connections; a periodic comment frame keeps
# the stream alive so final frames (e.g. the timeout error) actually reach
# the browser instead of being silently lost.
_GROUP_CHAT_HEARTBEAT_INTERVAL = 25.0


@dataclass(frozen=True)
class _NestedStreamFrame:
    """Ephemeral nested-tool UI frame transported through the round queue.

    It is written to SSE in queue order but is never yielded, collected, or
    persisted in conversation history.
    """

    frame: dict


# ---------------------------------------------------------------------------
# AGENTS markdown table builder (shared between persist & inference)
# ---------------------------------------------------------------------------


def build_agents_markdown(
    agent_ids: list[str],
    agent_manager,
    *,
    exclude_agent_id: Optional[str] = None,
    include_user_row: bool = False,
) -> str:
    """Build a markdown table listing participants in a group chat.

    Two distinct scenarios:

    * Group-chat roster (``include_user_row=True``): lists ALL participating
      agents plus a fixed user row so agents know the user's role.
    * talk_to scheduling roster (``exclude_agent_id=<self>``): lists every
      agent EXCEPT the one whose prompt the table is injected into, so the
      model only sees schedulable targets for ``talk_to``.

    Returns an empty string if no agents can be resolved.
    """
    rows = []
    for aid in agent_ids:
        if exclude_agent_id and aid == exclude_agent_id:
            continue
        agent = agent_manager.get(aid) if agent_manager else None
        if agent is None:
            continue
        nickname = agent.get("nickname", aid)
        desc = (agent.get("description") or "").replace("\n", " ")
        rows.append((nickname, aid, desc))
    if not rows and not include_user_row:
        return ""
    lines = [
        "| Nickname | Agent ID | Description |",
        "| --- | --- | --- |",
    ]
    for nick, aid, desc in rows:
        lines.append(f"| {nick} | {aid} | {desc} |")
    if include_user_row:
        lines.append("| 用户 | user | 在群聊中代表用户。在角色扮演中属于第三人视角 |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Default group-chat system prompt (shared constant — single source of truth)
# ---------------------------------------------------------------------------

_GC_DEFAULT_PROMPT = (
    "这是一场群聊对话，以下是参与本次对话的AI代理：\n"
    "{{AGENTS}}\n\n"
    "在消息中使用 @ 符号提及某位AI代理时，可以使用它的昵称；"
    "在 `talk_to` 工具中向某几位AI代理发送消息时，`agents` 须使用他们的 Agent ID 。请勿通过 `talk_to` 工具给你自己发送消息。"
)


# ---------------------------------------------------------------------------
# @ mention parsing
# ---------------------------------------------------------------------------

_MENTION_RE = re.compile(r"@(\S+)")


def parse_mentions(text: str) -> list[str]:
    """Extract @-mention targets from *text*.

    Examples::

        parse_mentions("@alice @bob let's discuss")  # => ['alice', 'bob']
        parse_mentions("@all 大家来讨论")              # => ['all']
        parse_mentions("no mentions here")            # => []
        parse_mentions("@Alice_Bob-123")             # => ['Alice_Bob-123']
    """
    return _MENTION_RE.findall(text)


def resolve_mentions(
    mentions: list[str],
    agent_manager,
    all_agent_ids: list[str],
) -> list[str]:
    """Resolve mention strings to agent_id list.

    Rules:

    * ``all`` / ``everyone`` (case-insensitive) → return *all_agent_ids*
    * Otherwise: first try exact agent_id match, then nickname match
    * Only agents present in *all_agent_ids* are returned
    * De-duplicated, preserving first-occurrence order
    """
    if not mentions:
        return []

    # @all / @everyone → broadcast to everyone
    special = {"all", "everyone"}
    if any(m.lower() in special for m in mentions):
        return list(all_agent_ids)

    resolved: list[str] = []
    seen: set[str] = set()
    for name in mentions:
        agent = agent_manager.get(name) if agent_manager else None
        if agent and agent["agent_id"] in all_agent_ids and agent["agent_id"] not in seen:
            seen.add(agent["agent_id"])
            resolved.append(agent["agent_id"])
    return resolved


def route_group_chat_user_message(
    text: str,
    agent_manager,
    all_agent_ids: list[str],
    prior_turns: list,
) -> list[str]:
    """Choose the agents that should answer a group-chat user message.

    Routing priority is intentionally simple and conversational:

    1. An explicit valid ``@`` mention wins (including ``@all``).
    2. With no ``@``, route to the most recent participating assistant that
       responded in the existing conversation.
    3. If no participant has responded yet, route to every participant whose
       agent labels contain ``leader``.
    4. If there is no leader, retain the legacy fallback and broadcast to all.

    ``prior_turns`` may contain either persisted ``ConversationTurn`` objects
    or message dictionaries.  Unknown explicit mentions preserve the existing
    behavior: they resolve to no target here and are handled by the caller's
    compatibility fallback.
    """
    mentions = parse_mentions(text or "")
    if mentions:
        return resolve_mentions(mentions, agent_manager, all_agent_ids)

    participants = set(all_agent_ids)
    for turn in reversed(prior_turns or []):
        msg = _message_from_turn(turn)
        if msg.role == "assistant" and msg.agent_id in participants:
            return [msg.agent_id]

    leaders: list[str] = []
    for agent_id in all_agent_ids:
        agent = agent_manager.get(agent_id) if agent_manager else None
        if agent and "leader" in (agent.get("labels") or []):
            leaders.append(agent_id)
    return leaders or list(all_agent_ids)


# ---------------------------------------------------------------------------
# Delayed-visibility context assembly
# ---------------------------------------------------------------------------

def _count_missed_rounds(agent_id: str, turns: list) -> int:
    """Count how many user-message *rounds* this agent was not @-mentioned in.

    A "round" is a user message plus any ensuing assistant/tool messages.
    Broadcast messages (``mentions`` is None or empty) are NOT counted as missed.
    """
    missed = 0
    for turn in turns:
        msg = _message_from_turn(turn)
        if msg.role == "user":
            mentions = msg.mentions
            if mentions and agent_id not in mentions:
                missed += 1
    return missed


def _message_from_turn(turn) -> Message:
    """Convert a ConversationTurn (or dict) to a Message, copying key fields."""
    # Handle both ConversationTurn dataclass and plain dict
    if isinstance(turn, dict):
        return Message(
            role=turn.get("role", "user"),
            content=turn.get("content", ""),
            timestamp=turn.get("timestamp"),
            name=turn.get("name"),
            agent_id=turn.get("agent_id") or turn.get("assistant_id"),
            mentions=turn.get("mentions"),
            tool_calls=turn.get("tool_calls"),
            thinking=turn.get("thinking"),
            images=turn.get("images"),
            prompt_template=turn.get("prompt_template"),
            arguments=turn.get("arguments"),
            tool_id=turn.get("tool_id"),
            tool_use_id=turn.get("tool_use_id"),
        )
    else:
        return Message(
            role=turn.role,
            content=turn.content or "",
            timestamp=getattr(turn, "timestamp", None),
            name=getattr(turn, "name", None),
            agent_id=getattr(turn, "agent_id", None) or getattr(turn, "assistant_id", None),
            mentions=getattr(turn, "mentions", None),
            tool_calls=getattr(turn, "tool_calls", None),
            thinking=getattr(turn, "thinking", None),
            images=getattr(turn, "images", None),
            prompt_template=getattr(turn, "prompt_template", None),
            arguments=getattr(turn, "arguments", None),
            tool_id=getattr(turn, "tool_id", None),
            tool_use_id=getattr(turn, "tool_use_id", None),
        )


# Marker used on the catch-up note user message so _normalize_for_model can
# tell it apart from real user speech (it must NOT get a "**用户** (user): "
# identity prefix).
_CATCH_UP_TAG = "@catch_up"

# Marker used on round-2+ trigger messages (the assistant @-mention replies
# that pulled an agent into the conversation). They are re-presented as a
# user message with the *mentioning agent's* identity prefix already baked in,
# so _normalize_for_model must not prepend the "**用户** (user): " prefix.
_TRIGGER_TAG = "@trigger"


def _trigger_to_user_message(msgs: list[Message]) -> Message:
    """Build the 'current message' for a round-2+ participant from the
    assistant replies that @-mentioned it.

    Mirrors how a user message that @-mentions an agent drives round 1: the
    triggered agent sees exactly the replies that pulled it in, prefixed with
    each author's identity (``**nickname** (agent_id): ...``). Tagged with
    ``_TRIGGER_TAG`` so ``_normalize_for_model`` skips the user prefix.
    """
    parts = []
    for m in msgs:
        nick = getattr(m, "name", None) or getattr(m, "agent_id", "") or ""
        aid = getattr(m, "agent_id", "") or ""
        body = (m.content or "").strip()
        parts.append(f"**{nick}** ({aid}): {body}")
    return Message(role="user", content="\n\n".join(parts), name=_TRIGGER_TAG)


def _tool_call_name(tc) -> str:
    """Extract the tool/function name from a tool-call dict.

    Supports both the flat ``{"name": ...}`` shape used internally and the
    OpenAI-style ``{"function": {"name": ...}}`` shape coming from protocol
    parsers. Returns "" when the name cannot be resolved.
    """
    if not isinstance(tc, dict):
        return ""
    name = tc.get("name")
    if name:
        return str(name)
    fn = tc.get("function")
    if isinstance(fn, dict):
        return str(fn.get("name", "") or "")
    return ""


def _normalize_for_model(messages: list[Message], agent_id: str) -> list[Message]:
    """Normalize messages from the perspective of *agent_id*.

    The model API requires strict user/assistant alternation.  We re-role so
    that only *agent_id*'s own assistant output stays as ``assistant``;
    **everything else** (other agents' text, their tool-calls, their tool
    results) is collapsed into ``user`` messages.

    Rationale: tool-calls are "thinking", not visible output.  In a group
    chat the thinking of other participants is irrelevant — only their
    spoken words matter.  Tool results are normally summarised in the
    agent's follow-up text anyway; edge cases where information is lost
    (tool result not summarised) are rare and acceptable.

    Rules:

    1. System messages → merge consecutive into one.
    2. Assistant from *agent_id* → keep as ``assistant`` (merge consecutive).
    3. Assistant from OTHER agents → convert to ``user``, with
       ``**nickname** (agent_id): `` prefix.  If the message carries
        *tool_calls* we append a standalone line ``（调用了工具a,b,c）``
       at the end of the content (purely from the assistant message,
       no tool results needed).
    6. Real user messages get a ``**用户** (user): `` identity prefix
       (the catch-up note message is exempt).
    4. Tool messages → if the last assistant was another agent,
       fold into ``user``; otherwise pass through as ``tool``.
    5. User messages → merge consecutive.
    """
    if not messages:
        return []

    normalized: list[Message] = []
    last_asst_self: Optional[bool] = None  # was the last assistant self?

    for msg in messages:
        raw_role = msg.role

        # ── Resolve effective role ────────────────────────────────────────
        if raw_role == "assistant":
            last_asst_self = (getattr(msg, "agent_id", None) == agent_id)
            if last_asst_self:
                effective_role = "assistant"
            else:
                effective_role = "user"
        elif raw_role == "tool":
            # Tools inherit from the preceding assistant:
            # other-agent tools → user; self-agent tools → tool
            if last_asst_self is False:
                effective_role = "user"
            else:
                effective_role = "tool"
        else:
            effective_role = raw_role

        # ── system ─────────────────────────────────────────────────────────
        if effective_role == "system":
            if normalized and normalized[-1].role == "system":
                prev = normalized[-1]
                if prev.prompt_template:
                    if prev.arguments is None:
                        prev.arguments = {}
                    prev.arguments["GC_FRAMING"] = (
                        (prev.arguments.get("GC_FRAMING") or "")
                        + "\n\n" + (msg.content or "")
                    )
                else:
                    prev.content = (prev.content or "") + "\n\n" + (msg.content or "")
            else:
                normalized.append(msg)

        # ── assistant (THIS agent only) ───────────────────────────────────
        elif effective_role == "assistant":
            # Consecutive self-assistants are OK and get merged
            if normalized and normalized[-1].role == "assistant":
                prev = normalized[-1]
                prev.content = (prev.content or "") + "\n\n" + (msg.content or "")
                if msg.tool_calls:
                    prev_tc = prev.tool_calls or []
                    prev.tool_calls = prev_tc + msg.tool_calls
            elif normalized and normalized[-1].role in ("tool", "system", "user"):
                normalized.append(msg)
            else:
                normalized.append(msg)

        # ── user (real users + other-agent output + other-agent tools) ───
        elif effective_role == "user":
            if raw_role == "assistant":
                # Other agent's output — tag with identity
                agent_name = getattr(msg, "name", None) or ""
                orig_aid = getattr(msg, "agent_id", None) or ""
                prefix = f"**{agent_name}** ({orig_aid}): " if agent_name else ""
                content = prefix + (msg.content or "")

                # Append tool-call note as a standalone line at the end
                tool_calls = getattr(msg, "tool_calls", None)
                if tool_calls:
                    names = [_tool_call_name(tc) for tc in tool_calls]
                    names = [n for n in names if n]
                    if names:
                        content = content.rstrip()
                        content += f"\n（调用了工具{', '.join(names)}）"
            elif raw_role == "tool":
                # Tool result belonging to another agent — just the content
                content = msg.content or ""
            else:
                # Real user message — tag with the user identity declared in
                # the AGENTS markdown (用户/user). The catch-up note and the
                # round-2+ trigger message are exempt (they already carry
                # their own identity, or none).
                if getattr(msg, "name", None) in (_CATCH_UP_TAG, _TRIGGER_TAG):
                    content = msg.content or ""
                else:
                    raw_content = (msg.content or "").strip()
                    if raw_content and not raw_content.startswith("**用户** (user):"):
                        content = f"**用户** (user): {raw_content}"
                    else:
                        content = msg.content or ""

            if normalized and normalized[-1].role == "user":
                prev = normalized[-1]
                prev.content = (prev.content or "") + "\n\n" + content
            else:
                normalized.append(Message(role="user", content=content))

        # ── tool (THIS agent's own tool result) ─────────────────────────
        else:
            normalized.append(msg)

    return normalized


def _build_agent_system_messages(
    agent: dict,
    agent_id: str,
    agents_markdown: str,
    dispatch_agents_markdown: Optional[str],
    promoted_turn: Optional[object],
    summary_text: str,
    memory_entries: Optional[list],
) -> list[Message]:
    """Build system messages for a single agent in a group chat.

    Extracted from assemble_agent_context to support both list and generator
    paths with the same logic.
    """
    messages: list[Message] = []

    # Group-chat framing (prepend "\n\n" when merging with agent prompt).
    _GC_FRAMING = "\n\n" + _GC_DEFAULT_PROMPT

    sys_prompt: str = agent.get("system_prompt", "")
    template_id = agent.get("template_id")
    template_args = agent.get("template_arguments", {})

    if template_id:
        messages.append(Message(
            role="system", content="",
            prompt_template=template_id, arguments=dict(template_args or {}),
        ))
        sys_msg = messages[-1]
        if sys_msg.arguments is None:
            sys_msg.arguments = {}
        # AGENTS inside an agent-owned template is the *dispatch roster*:
        # current group participants minus this agent itself.  Runtime values
        # are authoritative, so always overwrite stale/literal values such as
        # "{{AGENTS}}" persisted in template_arguments.
        # AGENTS is a runtime-owned dynamic placeholder. Always provide a
        # value: the dispatch roster when talk_to is active, otherwise empty.
        # This prevents stale/literal template arguments from leaking.
        sys_msg.arguments["AGENTS"] = dispatch_agents_markdown or ""
        # GC_FRAMING describes the whole group and uses the full participant
        # roster (including user), not the dispatch roster.
        if agents_markdown:
            sys_msg.arguments["GC_FRAMING"] = _GC_FRAMING.replace(
                "{{AGENTS}}", agents_markdown,
            )
    elif sys_prompt:
        # AGENTS in the agent's own plain-text prompt is the dispatch roster.
        resolved_prompt = sys_prompt.replace(
            "{{AGENTS}}", dispatch_agents_markdown or "",
        )
        if agents_markdown:
            group_framing = _GC_DEFAULT_PROMPT.replace(
                "{{AGENTS}}", agents_markdown,
            )
            messages.append(Message(
                role="system",
                content=resolved_prompt + "\n\n" + group_framing,
            ))
        else:
            messages.append(Message(role="system", content=resolved_prompt))
    elif promoted_turn is not None:
        sys_content = _message_from_turn(promoted_turn).content
        # A persisted default group prompt may still contain a literal
        # placeholder from an older session; resolve it with the full roster.
        sys_content = (sys_content or "").replace("{{AGENTS}}", agents_markdown)
        messages.append(Message(role="system", content=sys_content))
    elif agents_markdown:
        messages.append(Message(
            role="system",
            content=_GC_DEFAULT_PROMPT.replace("{{AGENTS}}", agents_markdown),
        ))

    # 1b. Safety net: never send a literal AGENTS placeholder to the model.
    if messages and messages[-1].role == "system" and not messages[-1].prompt_template:
        sys_msg = messages[-1]
        if "{{AGENTS}}" in (sys_msg.content or ""):
            sys_msg.content = (sys_msg.content or "").replace(
                "{{AGENTS}}", dispatch_agents_markdown or agents_markdown,
            )
    elif not messages and agents_markdown:
        messages.append(Message(role="system", content=agents_markdown))

    # 1c. Inject rolling summary and structured memory
    has_summary = bool(summary_text.strip()) if summary_text else False
    has_memory = bool(memory_entries) if memory_entries else False
    if has_summary or has_memory:
        extra_parts: list[str] = []
        if has_summary:
            extra_parts.append(f"## Summary\n{summary_text}")
        if has_memory:
            memory_lines = [
                f"{entry.entry_type}: {entry.content}"
                for entry in memory_entries
            ]
            extra_parts.append("## Memory\n" + "\n".join(memory_lines))
        extra_block = "\n\n".join(extra_parts)

        if messages and messages[-1].role == "system":
            sys_msg = messages[-1]
            if sys_msg.prompt_template:
                if sys_msg.arguments is None:
                    sys_msg.arguments = {}
                existing_framing = sys_msg.arguments.get("GC_FRAMING", "")
                sys_msg.arguments["GC_FRAMING"] = (
                    existing_framing + "\n\n" + extra_block
                    if existing_framing else extra_block
                )
            else:
                sys_msg.content = (sys_msg.content or "") + "\n\n" + extra_block
        else:
            messages.insert(0, Message(role="system", content=extra_block))

    return messages


def assemble_agent_context(
    agent_id: str,
    agent: dict,
    turns: list,
    current_user_msg: Optional[Message] = None,
    agents_markdown: str = "",
    dispatch_agents_markdown: Optional[str] = None,
    summary_text: str = "",
    memory_entries: Optional[list] = None,
) -> list[Message]:
    """Build the inference context for one agent in a group chat.

    Delayed-visibility policy (WeChat model):

    * All historical messages are included (full transparency).
    * User messages where *agent_id* was NOT @-mentioned get a ``[群聊]``
      prefix so the agent can distinguish messages addressed to it from
      bystander conversation.
    * If the agent missed prior rounds, a catch-up system message is prepended.
    * When a rolling summary exists (context compression active), the summary
      and structured memory entries are injected into the system message so
      the agent has access to compressed historical context.

    Args:
        agent_id: The agent this context is being built for.
        agent: Agent record dict (with system_prompt, template_id, etc.).
        turns: ConversationTurn list from conversation.json (already truncated
            to recent K turns when compression is active).
        current_user_msg: The current user message that triggered this round.
        agents_markdown: Full participant roster (all group agents + user),
            used by the generic group-chat framing.
        dispatch_agents_markdown: Schedulable AGENTS roster for this agent's
            own prompt/template (all group agents except *agent_id*).
        summary_text: Rolling summary text (empty when compression has not
            been triggered).
        memory_entries: Optional list of structured MemoryEntry objects.
    """
    messages: list[Message] = []

    # 0. Detect a persisted group-chat system message in the turns *before*
    #    we build the agent system prompt, so we can always skip it during
    #    turn replay regardless of whether the agent has its own system_prompt.
    promoted_turn = None
    if agents_markdown:
        _GC_MARKER = "这是一场群聊对话"
        for turn in turns:
            turn_msg = _message_from_turn(turn)
            if turn_msg.role == "system" and _GC_MARKER in (turn_msg.content or ""):
                promoted_turn = turn
                break

    # 1. Agent's own system prompt (merged with group-chat framing when both exist)
    messages = _build_agent_system_messages(
        agent, agent_id, agents_markdown, dispatch_agents_markdown,
        promoted_turn, summary_text, memory_entries,
    )

    # 2. Catch-up note (if any)
    has_summary = bool(summary_text.strip()) if summary_text else False
    if not has_summary:
        missed = _count_missed_rounds(agent_id, turns)
        if missed > 0:
            catch_up = (
                f"📢 群聊回追：在你未 @ 参与期间共有 {missed} 轮对话。"
                f"以下为完整历史记录，其中标注 [群聊] 的消息并非 @ 你，供参考上下文。"
            )
            messages.append(Message(role="user", content=catch_up, name=_CATCH_UP_TAG))
    else:
        catch_up = "📢 群聊回追：部分早期对话已被压缩为摘要，详见上方 Summary。"
        messages.append(Message(role="user", content=catch_up, name=_CATCH_UP_TAG))

    # 3. Historical turns (all included, non-mentioned user messages marked)
    for turn in turns:
        msg = _message_from_turn(turn)
        if promoted_turn is not None and turn is promoted_turn:
            continue
        if current_user_msg is not None and msg.role == "user" and msg.content == current_user_msg.content:
            continue
        turn_mentions: Optional[list[str]] = msg.mentions
        if turn_mentions and msg.role == "user" and agent_id not in turn_mentions:
            msg.content = f"[群聊] {msg.content}"
        messages.append(msg)

    # 4. Current user message (if any)
    if current_user_msg is not None:
        messages.append(current_user_msg)

    # 5. Normalize from *agent_id*'s perspective
    return _normalize_for_model(messages, agent_id)


# ---------------------------------------------------------------------------
# Multi-agent parallel orchestration (list version — DEPRECATED)
# ---------------------------------------------------------------------------

# Keep the original run_group_chat_stream for backward compatibility until
# run_group_chat_stream_gen is proven in production.
# The generator version below is the future; this list version remains
# available during transition and is used by the non-streaming handler path.


def run_group_chat_stream(
    *,
    runtime,
    mentioned_agent_ids: list[str],
    all_agent_ids: list[str],
    original_messages: list[Message],
    base_request: InferenceRequest,
    cancel_event,
    sse_callback,
    sse_heartbeat=None,
    context_manager,
    session_id: str,
    agent_manager,
    model_id: str,
    tool_ids: list[str],
    max_rounds: int = 5,
) -> list[Message]:
    """Execute one round of group chat inference (returns list of Messages).

    DEPRECATED: Prefer run_group_chat_stream_gen() which behaves as a
    generator and supports incremental persistence. This list version is
    kept for the non-streaming handler path during the transition period.

    Called from ``_handle_infer`` (non-streaming) when group-chat routing is
    active, and from ``_handle_infer_stream`` as the legacy path.

    Parameters:
        runtime: Runtime instance for model inference.
        mentioned_agent_ids: Agent IDs that should respond this round.
        all_agent_ids: All agent IDs participating in this group chat
            (used to build the AGENTS markdown table).
        original_messages: Raw new messages from the request (no system prompt).
        base_request: The InferenceRequest built by ``_prepare_infer_request``
            (used as template for per-agent requests).
        cancel_event: threading.Event for abort.
        sse_callback: SSE write callback (frame dict → writes to client).
        sse_heartbeat: optional zero-argument callback emitting an SSE
            keep-alive comment frame while waiting on slow agents, so
            long-idle streams are not dropped by gateways/proxies.
        context_manager: ContextManager for loading conversation history.
        session_id: Group chat session ID.
        agent_manager: AgentManager for resolving agent configs.
        model_id: Default model ID (may be overridden per agent).
        tool_ids: Default tool IDs (may be overridden per agent).

    Returns:
        Collected Message objects from all agents, already tagged with
        ``agent_id`` and ``name``.
    """
    # Delegate to the generator version and consume it into a list.
    gen = _run_group_chat_stream_gen(
        runtime=runtime,
        mentioned_agent_ids=mentioned_agent_ids,
        all_agent_ids=all_agent_ids,
        original_messages=original_messages,
        base_request=base_request,
        cancel_event=cancel_event,
        sse_callback=sse_callback,
        sse_heartbeat=sse_heartbeat,
        context_manager=context_manager,
        session_id=session_id,
        agent_manager=agent_manager,
        model_id=model_id,
        tool_ids=tool_ids,
        max_rounds=max_rounds,
        stream_chunks=False,
    )
    collected: list[Message] = []
    for msg in gen:
        collected.append(msg)
    return collected


# ---------------------------------------------------------------------------
# Public generator interface (the unified future path)
# ---------------------------------------------------------------------------


def run_group_chat_stream_gen(
    *,
    runtime,
    mentioned_agent_ids: list[str],
    all_agent_ids: list[str],
    original_messages: list[Message],
    base_request: InferenceRequest,
    cancel_event=None,
    sse_callback=None,
    sse_heartbeat=None,
    context_manager,
    session_id: str,
    agent_manager,
    model_id: str,
    tool_ids: list[str],
    max_rounds: int = 5,
) -> Generator[Message, None, None]:
    """Generator version of group chat inference.

    Unlike the list-returning ``run_group_chat_stream``, this function
    yields Messages incrementally as they are produced by each agent's
    ``infer_stream``.  The caller (handler) can do unified SSE writing
    and incremental persistence without needing callback/notification.

    When ``len(all_agent_ids) <= 2`` (user + a single AI, or solo),
    the group-chat machinery normally used for multi-agent scenarios
    (AGENTS markdown, _normalize_for_model with role-rewriting, parallel
    ThreadPoolExecutor) is bypassed.  The degenerate case is handled by
    a direct single-agent inference path that behaves identically to
    ``runtime.infer_stream`` — same system prompt, same tool loop, same
    message normalization.

    Args:
        sse_callback: Optional callback retained for self-managing tools such
            as delegate/talk_to, which emit nested stream frames directly.
            Ordinary model messages are yielded and must be written by the
            caller, so they are not duplicated through this callback.
        sse_heartbeat: Optional zero-argument callback emitting an SSE
            keep-alive comment frame while waiting on slow agents in
            multi-agent mode (degenerate mode does not need it).
            Ignored in degenerate mode.

    Yields:
        Message objects tagged with ``agent_id`` and ``name``.
    """
    yield from _run_group_chat_stream_gen(
        runtime=runtime,
        mentioned_agent_ids=mentioned_agent_ids,
        all_agent_ids=all_agent_ids,
        original_messages=original_messages,
        base_request=base_request,
        cancel_event=cancel_event,
        sse_callback=sse_callback,
        sse_heartbeat=sse_heartbeat,
        context_manager=context_manager,
        session_id=session_id,
        agent_manager=agent_manager,
        model_id=model_id,
        tool_ids=tool_ids,
        max_rounds=max_rounds,
    )


# ---------------------------------------------------------------------------
# Multi-agent parallel orchestration (generator version)
# ---------------------------------------------------------------------------


def _run_group_chat_stream_gen(
    *,
    runtime,
    mentioned_agent_ids: list[str],
    all_agent_ids: list[str],
    original_messages: list[Message],
    base_request: InferenceRequest,
    cancel_event,
    sse_callback,
    sse_heartbeat=None,
    context_manager,
    session_id: str,
    agent_manager,
    model_id: str,
    tool_ids: list[str],
    max_rounds: int = 5,
    stream_chunks: bool = True,
) -> Generator[Message, None, None]:
    """Execute group chat inference as a generator of Messages.

    Generator version of ``run_group_chat_stream``. Yields each Message as
    it becomes available from the parallel agent runners, allowing the
    caller (handler) to do unified SSE writing and incremental persistence
    without needing a callback.

    When ``len(all_agent_ids) <= 2`` (i.e. just user + one AI, or solo),
    the group-chat machinery is bypassed and the function behaves identically
    to a direct call to ``runtime.infer_stream`` — the degenerate case.

    Parameters (same as ``run_group_chat_stream``):

    Yields:
        Message objects as they are produced by each agent's infer_stream.
        Each message is tagged with ``agent_id`` and ``name``.
        The final yield is always a ``usage`` message if any exists.
    """
    # Group-chat agents run in ThreadPoolExecutor workers.  Capture the HTTP
    # thread's complete request context before entering those workers; otherwise
    # session_dir / user_message_timestamp / workspace are absent and file tools
    # silently create a stateless (non-visible) journal.
    parent_request_context = snapshot_request_context()

    # ── Load conversation history (needed by both degenerate and full paths) ──
    existing_turns = []
    summary_text: str = ""
    memory_entries: list = []
    if session_id:
        try:
            existing_turns = context_manager.load_conversation(session_id)
        except (FileNotFoundError, ValueError):
            pass

        # Rolling summary & structured memory (context compression)
        summary_text, summary_fm = context_manager.get_summary(session_id)
        if summary_text.strip():
            memory_entries = context_manager.get_memory_entries(session_id)
            k = getattr(context_manager, '_recent_turns_k', 10)
            summarized_up_to = summary_fm.get("summarized_up_to_turn", -1)
            if isinstance(summarized_up_to, int) and summarized_up_to >= 0:
                recent_start = max(0, len(existing_turns) - k)
                if recent_start <= summarized_up_to:
                    recent_start = min(summarized_up_to + 1, len(existing_turns))
                while (recent_start < len(existing_turns)
                       and recent_start > 0
                       and existing_turns[recent_start].role == "tool"):
                    recent_start -= 1
                while (recent_start < len(existing_turns)
                       and recent_start > 0
                       and existing_turns[recent_start].role not in ("user", "system")):
                    recent_start -= 1
                existing_turns = existing_turns[recent_start:]

    # ── Degenerate case: single participant (user + 1 AI, or solo) ─────
    # When len(all_agent_ids) <= 2 (user + one AI), the group-chat
    # machinery is unnecessary.  We route directly through a single
    # infer_stream call (which already has its own tool loop) so the
    # behavior is identical to a non-group-chat single-agent session.
    # This ensures group_chat and infer_stream produce the same results
    # for the 1- or 2-participant case.
    if len(all_agent_ids) <= 2 and all_agent_ids and len(mentioned_agent_ids) <= 1:
        # In retry mode the retained roster may contain two agents while only
        # retry_agent_id is selected to answer.  Prefer the explicit target;
        # historical behavior for normal single-agent requests remains the
        # first roster entry.
        agent_id = mentioned_agent_ids[0] if mentioned_agent_ids else all_agent_ids[0]
        agent = agent_manager.get(agent_id)
        if agent is not None:
            nickname: str = agent.get("nickname", agent_id)
            agent_model_id: str = agent.get("model_id", model_id)
            agent_tool_ids: list[str] = agent.get("tool_ids", tool_ids)

            # Build context as a regular single-agent inference context
            # (no AGENTS markdown, no group-chat framing, no catch-up).
            # We reuse assemble_agent_context with agents_markdown="" to
            # get the standard single-agent system prompt + history.
            first_user_msg: Optional[Message] = None
            for m in reversed(original_messages):
                if m.role == "user":
                    first_user_msg = m
                    break
            if first_user_msg is None and original_messages:
                first_user_msg = Message(role="user", content="")

            dispatch_agents_markdown = None
            if "talk_to" in agent_tool_ids:
                dispatch_agents_markdown = build_agents_markdown(
                    all_agent_ids,
                    agent_manager,
                    exclude_agent_id=agent_id,
                )
            agent_messages = assemble_agent_context(
                agent_id, agent, existing_turns, first_user_msg,
                agents_markdown="",
                dispatch_agents_markdown=dispatch_agents_markdown,
                summary_text=summary_text,
                memory_entries=memory_entries,
            )

            degenerate_request = InferenceRequest(
                model_id=agent_model_id,
                tool_ids=agent_tool_ids,
                messages=agent_messages,
                stream=True,
                max_tool_rounds=base_request.max_tool_rounds,
            )

            for msg in runtime.infer_stream(degenerate_request,
                                            cancel_event=cancel_event):
                msg.agent_id = agent_id
                if msg.role == "assistant":
                    msg.name = nickname
                yield msg
            return

    agents_markdown = build_agents_markdown(
        all_agent_ids, agent_manager, include_user_row=True,
    )
    max_workers_env = os.environ.get(
        "MAX_GROUP_CHAT_WORKERS",
        os.environ.get("MAX_TALK_TO_WORKERS", "10"),
    )
    max_workers = int(max_workers_env)

    # Extract the initial user message (round 1 only).
    first_user_msg: Optional[Message] = None
    for m in reversed(original_messages):
        if m.role == "user":
            first_user_msg = m
            break
    if first_user_msg is None and original_messages:
        first_user_msg = Message(role="user", content="")

    all_collected: list[Message] = []
    processed_agent_ids: set[str] = set()
    pending_mentioned: list[str] = [
        aid for aid in mentioned_agent_ids if aid not in processed_agent_ids
    ]
    round_num = 0
    # Replies that triggered the agents participating in the current round.
    # Round 1 is driven by the user's message, so this starts empty.  Each
    # completed round builds a fresh dict for the following round.
    trigger_msgs: dict[str, list[Message]] = {}

    while pending_mentioned and round_num < max_rounds:
        round_num += 1
        current_trigger_msgs = trigger_msgs
        next_trigger_msgs: dict[str, list[Message]] = {}
        for aid in pending_mentioned:
            processed_agent_ids.add(aid)

        # --- per-agent runner (generator-based, feeds results through a queue) ---

        def _run_one_gen(
            agent_id: str,
            nested_stream_callback=None,
        ) -> Generator[Message, None, None]:
            """Generator that yields messages for a single agent and appends
            to existing_turns when done.  Also populates _round_trigger_msgs
            with any @-mentions found in the *merged* (post-_flush_asst)
            collected messages, so the main round loop can correctly detect
            mentions across chunk boundaries."""
            nonlocal existing_turns
            set_request_context(context_manager=context_manager,
                                session_id=session_id,
                                agent_manager=agent_manager,
                                cancel_event=cancel_event,
                                all_agent_ids=all_agent_ids,
                                agent_id=agent_id,
                                sse_callback=nested_stream_callback)
            agent = agent_manager.get(agent_id)
            if agent is None:
                return

            nickname: str = agent.get("nickname", agent_id)
            agent_model_id: str = agent.get("model_id", model_id)
            agent_tool_ids: list[str] = agent.get("tool_ids", tool_ids)
            agent_tool_scope = [
                tc for tid in agent_tool_ids
                if (tc := runtime._tool_registry.get(tid)) is not None
            ]
            set_request_context(
                tool_scope=agent_tool_scope,
                available_tool_ids=agent_tool_ids,
            )

            # Current message for THIS agent
            trigger_list = (current_trigger_msgs.get(agent_id)
                            if round_num > 1 else None)
            cur_user_msg: Optional[Message]
            if round_num == 1:
                cur_user_msg = first_user_msg
            elif trigger_list:
                cur_user_msg = _trigger_to_user_message(trigger_list)
            else:
                cur_user_msg = None

            # Exclude trigger replies from replayed history
            agent_turns = existing_turns
            if trigger_list:
                trigger_pairs = {(m.agent_id, m.content or "") for m in trigger_list}
                # ``existing_turns`` is loaded from ContextManager as
                # ConversationTurn objects, then receives dict entries produced
                # by earlier group-chat rounds.  Normalize both shapes before
                # inspecting them; calling ``.get`` directly on a persisted
                # ConversationTurn crashes every round-2+ participant.
                filtered_turns = []
                for turn in existing_turns:
                    turn_msg = _message_from_turn(turn)
                    is_trigger_reply = (
                        turn_msg.role == "assistant"
                        and (turn_msg.agent_id, turn_msg.content or "")
                        in trigger_pairs
                    )
                    if not is_trigger_reply:
                        filtered_turns.append(turn)
                agent_turns = filtered_turns

            dispatch_agents_markdown = None
            if "talk_to" in agent_tool_ids:
                dispatch_agents_markdown = build_agents_markdown(
                    all_agent_ids,
                    agent_manager,
                    exclude_agent_id=agent_id,
                )
            agent_messages = assemble_agent_context(
                agent_id, agent, agent_turns, cur_user_msg,
                agents_markdown=agents_markdown,
                dispatch_agents_markdown=dispatch_agents_markdown,
                summary_text=summary_text,
                memory_entries=memory_entries,
            )

            request = InferenceRequest(
                model_id=agent_model_id,
                tool_ids=agent_tool_ids,
                messages=agent_messages,
                stream=True,
                max_tool_rounds=base_request.max_tool_rounds,
            )

            asst_buf: list[Message] = []

            def _flush_asst(collected: list[Message]) -> None:
                if not asst_buf:
                    return
                content = "".join(m.content or "" for m in asst_buf)
                thinking = "".join(m.thinking or "" for m in asst_buf) or None
                tool_calls: list[dict] = []
                by_idx: dict[int, dict] = {}
                for m in asst_buf:
                    for tc in (m.tool_calls or []):
                        if not isinstance(tc, dict):
                            continue
                        idx = tc.get("_index")
                        if idx is None:
                            tool_calls.append(dict(tc))
                            continue
                        target = by_idx.setdefault(
                            int(idx), {"id": "", "name": "", "arguments": ""})
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
                                target["arguments"] = (target.get("arguments", "")
                                                       + tc["arguments"])
                for idx in sorted(by_idx):
                    tool_calls.append(by_idx[idx])
                for tc in tool_calls:
                    tc.pop("_index", None)

                ts = None
                for m in asst_buf:
                    if m.timestamp:
                        ts = m.timestamp
                        break
                full = Message(
                    role="assistant",
                    content=content,
                    thinking=thinking,
                    tool_calls=tool_calls or None,
                    timestamp=ts,
                    agent_id=agent_id,
                    name=nickname,
                )
                # Tag with valid @-mentions
                if content:
                    parsed = parse_mentions(content)
                    if parsed:
                        resolved = resolve_mentions(
                            parsed, agent_manager, all_agent_ids)
                        if resolved:
                            full.mentions = resolved
                collected.append(full)
                asst_buf.clear()

            collected: list[Message] = []
            try:
                for msg in runtime.infer_stream(request,
                                                cancel_event=cancel_event):
                    msg.agent_id = agent_id
                    if msg.role == "assistant":
                        msg.name = nickname
                        asst_buf.append(msg)
                    else:
                        _flush_asst(collected)
                        collected.append(msg)
                    if stream_chunks:
                        yield msg
            except Exception as exc:
                _logger.error("agent %s inference failed: %s", agent_id, exc)
                error_msg = Message(
                    role="assistant",
                    content=f"Error: {exc}",
                    agent_id=agent_id,
                    name=nickname,
                )
                collected.append(error_msg)
                if stream_chunks:
                    yield error_msg
                asst_buf.clear()

            _flush_asst(collected)

            # Append to in-memory turns (for next round's mention detection)
            for msg in collected:
                if msg.role == "usage":
                    continue
                existing_turns.append({
                    "role": msg.role,
                    "content": msg.content or "",
                    "timestamp": msg.timestamp,
                    "name": getattr(msg, "name", None),
                    "agent_id": getattr(msg, "agent_id", None),
                    "thinking": getattr(msg, "thinking", None),
                    "stat": getattr(msg, "stat", None),
                    "mentions": getattr(msg, "mentions", None),
                    "tool_calls": getattr(msg, "tool_calls", None),
                    "tool_id": getattr(msg, "tool_id", None),
                    "tool_use_id": getattr(msg, "tool_use_id", None),
                })

            # Detect @-mentions in merged (post-_flush_asst) assistant messages.
            # This must be done on `collected`, NOT on the fragmented chunks that
            # were yielded to the main thread (a mention like "@沙和尚" can span
            # multiple chunks and would be missed by chunk-level scanning).
            for msg in collected:
                if msg.role != "assistant" or not msg.content:
                    continue
                # _flush_asst has already parsed the complete content and
                # resolved valid participant mentions.  Reuse that result so
                # mentions split across streaming chunks remain detectable.
                for aid in (msg.mentions or []):
                    if aid not in processed_agent_ids:
                        next_trigger_msgs.setdefault(aid, []).append(msg)

            # The legacy list-returning API expects complete, merged turns;
            # the streaming API instead yields raw deltas above.
            if not stream_chunks:
                yield from collected

        # --- parallel execution for this round ----------------------------
        round_collected: list[Message] = []

        executor = ThreadPoolExecutor(max_workers=max_workers)
        # Submit each agent's generator via a queue so the main thread can
        # yield messages as they come in.  Include agent_id in every event so
        # timeout handling can distinguish completed from still-running agents.
        _msg_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        _agent_count = len(pending_mentioned)

        def _make_nested_stream_callback(owner_agent_id: str):
            """Route self-streaming tool frames through the same ordered queue
            as the owning agent's ordinary model/tool messages."""
            def _callback(frame: dict) -> None:
                normalized = dict(frame)
                normalized.setdefault("agent_id", owner_agent_id)
                _msg_queue.put((owner_agent_id, _NestedStreamFrame(normalized)))
            return _callback

        def _agent_worker(agent_id: str) -> None:
            """Run one agent's generator and feed its messages to the queue."""
            worker_context = dict(parent_request_context)
            # A manager is turn/thread-local mutable state.  Each parallel agent
            # gets its own instance, while all instances target the same parent
            # session + user turn and merge through the manifest lock.
            worker_context["file_journal_manager"] = None
            restore_request_context(worker_context)
            nested_stream_callback = _make_nested_stream_callback(agent_id)
            try:
                for msg in _run_one_gen(
                    agent_id,
                    nested_stream_callback=nested_stream_callback,
                ):
                    _msg_queue.put((agent_id, msg))
            except Exception as exc:
                _logger.error("group chat agent_worker %s failed: %s", agent_id, exc)
                err = Message(
                    role="assistant",
                    content=f"Error: agent inference failed: {exc}",
                    agent_id=agent_id,
                    name=agent_manager.get(agent_id).get("nickname", agent_id)
                    if agent_manager.get(agent_id) else agent_id,
                )
                _msg_queue.put((agent_id, err))
            finally:
                journal_manager = get_request_context("file_journal_manager")
                if journal_manager is not None:
                    try:
                        journal_manager.flush()
                    except Exception as exc:
                        _logger.warning(
                            "group chat agent %s failed to finalize file journal: %s",
                            agent_id, exc,
                        )
                restore_request_context({})
                _msg_queue.put((agent_id, None))  # sentinel: this agent is done

        futures = {
            executor.submit(_agent_worker, aid): aid
            for aid in pending_mentioned
        }

        try:
            remaining = _agent_count
            completed_agent_ids: set[str] = set()
            while remaining > 0:
                # Collect messages from queue until an agent finishes or
                # we hit the heartbeat interval (for keep-alive).
                got_any = False
                while True:
                    try:
                        event_agent_id, msg = _msg_queue.get(
                            timeout=_GROUP_CHAT_HEARTBEAT_INTERVAL)
                    except queue.Empty:
                        # Timeout or heartbeat interval reached
                        if cancel_event is not None and cancel_event.is_set():
                            remaining = 0
                        else:
                            if sse_heartbeat is not None:
                                try:
                                    sse_heartbeat()
                                except Exception:
                                    pass
                        break
                    else:
                        if isinstance(msg, _NestedStreamFrame):
                            got_any = True
                            if sse_callback is not None:
                                try:
                                    sse_callback(msg.frame)
                                except Exception:
                                    pass
                            continue
                        if msg is None:
                            if event_agent_id not in completed_agent_ids:
                                completed_agent_ids.add(event_agent_id)
                                remaining -= 1
                            if remaining <= 0:
                                got_any = True
                            break
                        got_any = True
                        round_collected.append(msg)
                        yield msg

                if remaining <= 0:
                    break

        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        all_collected.extend(round_collected)

        # Trigger_msgs already populated by _run_one_gen from merged assistant
        # messages (see the mention-detection block at the end of _run_one_gen).
        # Do NOT scan round_collected here — the yield-to-main-thread messages
        # are fragmented chunks that can miss cross-chunk @-mentions.
        trigger_msgs = next_trigger_msgs
        pending_mentioned = list(trigger_msgs.keys())

    # No additional yield needed; the queue already yielded everything.
