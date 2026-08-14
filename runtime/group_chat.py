"""Group chat @-mention mechanism for multi-agent conversations.

The core principle mimics human WeChat groups:
- @ mentions deliver immediately to the mentioned agent(s)
- Non-mentioned agents see messages later (delayed visibility — full history
  available when they are next @-mentioned)
- @all or no @ broadcast to all agents in the group
- All messages are transparently stored in conversation.json

Design:
    parse_mentions()     — extract @targets from text
    resolve_mentions()   — map mention strings to agent_ids
    assemble_agent_context() — build per-agent inference context with
                                delayed-visibility markers
    run_group_chat_stream()  — parallel multi-agent orchestration
"""

from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Optional

from runtime.common import set_request_context
from runtime.models import Message, InferenceRequest
from runtime.runtime import _get_infer_round_timeout

_logger = logging.getLogger("runtime.group_chat")

# SSE keep-alive interval for long group-chat waits. Gateways/proxies/
# firewalls commonly drop idle connections; a periodic comment frame keeps
# the stream alive so final frames (e.g. the timeout error) actually reach
# the browser instead of being silently lost.
_GROUP_CHAT_HEARTBEAT_INTERVAL = 25.0


def _get_group_chat_timeout(num_agents: int) -> Optional[float]:
    """Outer per-round deadline for a group-chat parallel wait.

    MODEL_INFER_ROUND_TIMEOUT caps a SINGLE tool-call round inside
    infer_stream (the cap resets per tool round), so one agent may
    legitimately take far longer than that across multiple tool rounds.
    Reusing the raw value as the outer group-chat deadline would kill
    multi-tool-round agents while they are still making progress, so the
    wait scales with the number of agents (excluding the user, who is
    never part of all_agent_ids): N agents get N x MODEL_INFER_ROUND_TIMEOUT
    per round.

    Returns None when the round-timeout guard is disabled (empty/<=0 env),
    mirroring _get_infer_round_timeout().
    """
    base = _get_infer_round_timeout()
    if base is None or num_agents <= 1:
        return base
    return base * num_agents


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
    "在 `talk_to` 工具中向某几位AI代理发送消息时，`agents` 须使用他们的 Agent ID 。"
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

        # ── Resolve effective role ──────────────────────────────────
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

        # ── system ────────────────────────────────────────────────
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

        # ── assistant (THIS agent only) ───────────────────────────
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

        # ── user (real users + other-agent output + other-agent tools) ──
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

        # ── tool (THIS agent's own tool result) ───────────────────
        else:
            normalized.append(msg)

    return normalized


def assemble_agent_context(
    agent_id: str,
    agent: dict,
    turns: list,
    current_user_msg: Optional[Message] = None,
    agents_markdown: str = "",
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
        agents_markdown: AGENTS markdown table listing all participants in
            the group chat. Always injected for group-chat contexts.
        summary_text: Rolling summary text (empty when compression has not
            been triggered).
        memory_entries: Optional list of structured MemoryEntry objects.
    """
    messages: list[Message] = []

    # Group-chat framing (prepend "\n\n" when merging with agent prompt).
    _GC_FRAMING = "\n\n" + _GC_DEFAULT_PROMPT

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
    sys_prompt: str = agent.get("system_prompt", "")
    template_id = agent.get("template_id")
    template_args = agent.get("template_arguments", {})

    if template_id:
        messages.append(Message(
            role="system", content="",
            prompt_template=template_id, arguments=template_args or {},
        ))
        # Template-based: inject AGENTS via arguments so {{AGENTS}} is rendered
        if agents_markdown:
            sys_msg = messages[-1]
            if sys_msg.arguments is None:
                sys_msg.arguments = {}
            if not sys_msg.arguments.get("AGENTS"):
                sys_msg.arguments["AGENTS"] = agents_markdown
            # Also inject the framing text (minus {{AGENTS}} which goes via arguments)
            if not sys_msg.arguments.get("GC_FRAMING"):
                sys_msg.arguments["GC_FRAMING"] = _GC_FRAMING.replace("{{AGENTS}}\n\n", "")
    elif sys_prompt:
        # Merge agent prompt + group-chat framing ({{AGENTS}} replaced in step 1b)
        if agents_markdown:
            merged = sys_prompt + _GC_FRAMING
            messages.append(Message(role="system", content=merged))
        else:
            messages.append(Message(role="system", content=sys_prompt))
    elif promoted_turn is not None:
        # Agent has no system prompt; promote the persisted group-chat prompt.
        sys_content = _message_from_turn(promoted_turn).content
        messages.append(Message(role="system", content=sys_content))
    elif agents_markdown:
        # No persisted prompt — generate the default with {{AGENTS}} placeholder.
        messages.append(Message(role="system", content=_GC_DEFAULT_PROMPT))

    # 1b. Inject AGENTS table into the system message (plain-text path only;
    #     template-based is already handled above)
    if agents_markdown:
        if messages and messages[-1].role == "system" and not messages[-1].prompt_template:
            sys_msg = messages[-1]
            content = sys_msg.content or ""
            if "{{AGENTS}}" in content:
                sys_msg.content = content.replace("{{AGENTS}}", agents_markdown)
        elif not messages:
            # Safety net: no system message at all
            messages.append(Message(
                role="system", content=agents_markdown,
            ))

    # 1c. Inject rolling summary and structured memory into the system message
    #     when context compression has been triggered.  This mirrors what
    #     ContextManager.assemble_context() does for single-agent sessions,
    #     but adapted for group-chat system message structure.
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
                # Template-based: inject via GC_FRAMING which is appended to the
                # template content during rendering.
                if sys_msg.arguments is None:
                    sys_msg.arguments = {}
                existing_framing = sys_msg.arguments.get("GC_FRAMING", "")
                sys_msg.arguments["GC_FRAMING"] = (
                    existing_framing + "\n\n" + extra_block
                    if existing_framing else extra_block
                )
            else:
                # Plain-text system message: append directly.
                sys_msg.content = (sys_msg.content or "") + "\n\n" + extra_block
        else:
            # No system message exists — create one.
            messages.insert(0, Message(role="system", content=extra_block))

    # 2. Catch-up note (if any) — carried as a USER message (marked with
    #    _CATCH_UP_TAG so _normalize_for_model skips the "**用户** (user): "
    #    identity prefix). Placed before the historical turns; it gets merged
    #    into the leading user message by _normalize_for_model.
    #    When a rolling summary exists (compression active), the summary
    #    already provides condensed historical context, so skip the catch-up
    #    note to avoid redundancy.
    if not has_summary:
        missed = _count_missed_rounds(agent_id, turns)
        if missed > 0:
            catch_up = (
                f"📢 群聊回追：在你未 @ 参与期间共有 {missed} 轮对话。"
                f"以下为完整历史记录，其中标注 [群聊] 的消息并非 @ 你，供参考上下文。"
            )
            messages.append(Message(role="user", content=catch_up, name=_CATCH_UP_TAG))
    else:
        # Summary exists: catch-up info is covered by the summary text.
        # Include a brief note so the agent knows some history was summarized.
        catch_up = "📢 群聊回追：部分早期对话已被压缩为摘要，详见上方 Summary。"
        messages.append(Message(role="user", content=catch_up, name=_CATCH_UP_TAG))
    # 3. Historical turns (all included, non-mentioned user messages marked)
    for turn in turns:
        msg = _message_from_turn(turn)
        # Skip the persisted group-chat system message if we already promoted
        # it to the agent's system prompt.
        if promoted_turn is not None and turn is promoted_turn:
            continue
        # Skip a user turn that duplicates current_user_msg (can happen when
        # pre-persistence saves the current round before inference starts and
        # run_group_chat_stream loads it back as existing_turns).
        if current_user_msg is not None and msg.role == "user" and msg.content == current_user_msg.content:
            continue
        # If this is a user message with explicit mentions that exclude this agent,
        # add a lightweight marker
        turn_mentions: Optional[list[str]] = msg.mentions
        if turn_mentions and msg.role == "user" and agent_id not in turn_mentions:
            msg.content = f"[群聊] {msg.content}"
        messages.append(msg)

    # 4. Current user message (if any — always addressed, the agent was @-mentioned)
    if current_user_msg is not None:
        messages.append(current_user_msg)

    # 5. Normalize from *agent_id*'s perspective: only its own assistant
    #    messages stay as assistant; other agents' output is re-roled to
    #    user so the model sees clean user/assistant alternation.
    return _normalize_for_model(messages, agent_id)


# ---------------------------------------------------------------------------
# Multi-agent parallel orchestration
# ---------------------------------------------------------------------------


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
    """Execute one round of group chat inference.

    Called from ``_handle_infer_stream`` when group-chat routing is active.

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

    # Load initial conversation history from disk once.
    existing_turns = []
    summary_text: str = ""
    memory_entries: list = []
    if session_id:
        try:
            existing_turns = context_manager.load_conversation(session_id)
        except (FileNotFoundError, ValueError):
            pass

        # --- Rolling summary & structured memory (context compression) ----
        # If compression has been triggered (summary.md exists), truncate the
        # historical turns to the most recent K turns and inject the summary
        # into the system message.  This mirrors ContextManager.assemble_context().
        summary_text, summary_fm = context_manager.get_summary(session_id)
        if summary_text.strip():
            memory_entries = context_manager.get_memory_entries(session_id)
            k = getattr(context_manager, '_recent_turns_k', 10)
            summarized_up_to = summary_fm.get("summarized_up_to_turn", -1)
            if isinstance(summarized_up_to, int) and summarized_up_to >= 0:
                # Truncate old turns that have been compressed into the summary.
                recent_start = max(0, len(existing_turns) - k)
                if recent_start <= summarized_up_to:
                    recent_start = min(summarized_up_to + 1, len(existing_turns))
                # Ensure we don't cut in the middle of a tool-call chain.
                while (recent_start < len(existing_turns)
                       and recent_start > 0
                       and existing_turns[recent_start].role == "tool"):
                    recent_start -= 1
                while (recent_start < len(existing_turns)
                       and recent_start > 0
                       and existing_turns[recent_start].role not in ("user", "system")):
                    recent_start -= 1
                existing_turns = existing_turns[recent_start:]

    all_collected: list[Message] = []
    processed_agent_ids: set[str] = set()
    pending_mentioned: list[str] = [
        aid for aid in mentioned_agent_ids if aid not in processed_agent_ids
    ]
    round_num = 0

    while pending_mentioned and round_num < max_rounds:
        round_num += 1
        # Populated at the END of each round: agent_id -> the assistant
        # replies that @-mentioned it (drives round 2+ as the "current
        # message", mirroring user @-mentions driving round 1).
        trigger_msgs: dict[str, list[Message]] = {}
        for aid in pending_mentioned:
            processed_agent_ids.add(aid)

        # --- per-agent runner (closes over this round's state) -------------
        def _run_one(agent_id: str) -> tuple[str, list[Message]]:
            set_request_context(context_manager=context_manager,
                                session_id=session_id,
                                agent_manager=agent_manager,
                                cancel_event=cancel_event,
                                all_agent_ids=all_agent_ids,
                                agent_id=agent_id,
                                sse_callback=sse_callback)
            agent = agent_manager.get(agent_id)
            if agent is None:
                return (agent_id, [])

            nickname: str = agent.get("nickname", agent_id)
            agent_model_id: str = agent.get("model_id", model_id)
            agent_tool_ids: list[str] = agent.get("tool_ids", tool_ids)

            # --- current message for THIS agent ----------------------------
            # Round 1: the user's message. Round 2+: the assistant replies
            # that @-mentioned this agent (presented like a user message, so
            # the agent knows exactly what pulled it in).
            trigger_list = trigger_msgs.get(agent_id) if round_num > 1 else None
            cur_user_msg: Optional[Message]
            if round_num == 1:
                cur_user_msg = first_user_msg
            elif trigger_list:
                cur_user_msg = _trigger_to_user_message(trigger_list)
            else:
                cur_user_msg = None

            # Exclude the trigger replies from the replayed history — they are
            # now the current message, so the agent must not see them twice.
            agent_turns = existing_turns
            if trigger_list:
                trigger_pairs = {(m.agent_id, m.content or "") for m in trigger_list}
                agent_turns = [
                    t for t in existing_turns
                    if not (t.get("role") == "assistant"
                            and (t.get("agent_id"), t.get("content") or "")
                            in trigger_pairs)
                ]

            agent_messages = assemble_agent_context(
                agent_id, agent, agent_turns, cur_user_msg,
                agents_markdown=agents_markdown,
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

            collected: list[Message] = []
            asst_buf: list[Message] = []  # incremental assistant chunks

            def _flush_asst() -> None:
                """Merge buffered incremental assistant chunks into ONE
                complete assistant Message (so @-mention parsing and the
                in-memory turns see full text, not token deltas)."""
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
                # Drop _index so merge_stream_messages treats them as complete.
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
                # Tag with valid @-mentions (same treatment as user messages:
                # only mentions that resolve to a participant count).
                if content:
                    parsed = parse_mentions(content)
                    if parsed:
                        resolved = resolve_mentions(
                            parsed, agent_manager, all_agent_ids)
                        if resolved:
                            full.mentions = resolved
                collected.append(full)
                asst_buf.clear()

            try:
                for msg in runtime.infer_stream(request,
                                                cancel_event=cancel_event):
                    msg.agent_id = agent_id
                    if msg.role == "assistant":
                        msg.name = nickname
                        asst_buf.append(msg)
                    else:
                        # tool/usage messages flush the pending assistant text
                        _flush_asst()
                        collected.append(msg)

                    # SSE: skip delegate/talk_to tool frames (self-managing).
                    if msg.role == "tool" and msg.name in ("delegate",
                                                            "talk_to"):
                        continue

                    if sse_callback:
                        frame = msg.to_dict()
                        frame["nickname"] = nickname
                        try:
                            sse_callback(frame)
                        except Exception:
                            pass

            except Exception as exc:
                _logger.error("agent %s inference failed: %s", agent_id, exc)
                error_msg = Message(
                    role="assistant",
                    content=f"Error: {exc}",
                    agent_id=agent_id,
                    name=nickname,
                )
                collected.append(error_msg)
                asst_buf.clear()
                if sse_callback:
                    try:
                        frame = error_msg.to_dict()
                        frame["nickname"] = nickname
                        sse_callback(frame)
                    except Exception:
                        pass

            _flush_asst()
            return (agent_id, collected)

        # --- parallel execution for this round ----------------------------
        round_collected: list[Message] = []

        def _append_error(agent_id: str, message: str) -> None:
            """Push an error assistant Message for a failed/timed-out agent,
            mirroring the per-agent error handling inside _run_one."""
            agent = agent_manager.get(agent_id)
            nickname = agent.get("nickname", agent_id) if agent else agent_id
            err = Message(
                role="assistant",
                content=f"Error: {message}",
                agent_id=agent_id,
                name=nickname,
            )
            round_collected.append(err)
            if sse_callback:
                try:
                    frame = err.to_dict()
                    frame["nickname"] = nickname
                    sse_callback(frame)
                except Exception:
                    pass

        executor = ThreadPoolExecutor(max_workers=max_workers)
        futures = {
            executor.submit(_run_one, aid): aid
            for aid in pending_mentioned
        }
        try:
            pending = set(futures)
            future_timeout = _get_group_chat_timeout(len(all_agent_ids))
            deadline = (time.monotonic() + future_timeout
                        if future_timeout is not None else None)
            while pending:
                if deadline is None:
                    wait_timeout = _GROUP_CHAT_HEARTBEAT_INTERVAL
                else:
                    wait_timeout = min(
                        _GROUP_CHAT_HEARTBEAT_INTERVAL,
                        max(0.0, deadline - time.monotonic()),
                    )
                done, pending = wait(
                    pending, timeout=wait_timeout,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    aid = futures[future]
                    try:
                        _aid, msgs = future.result()
                        round_collected.extend(msgs)
                    except Exception as exc:
                        _logger.error(
                            "group chat agent %s inference failed: %s",
                            aid, exc,
                        )
                        _append_error(aid, f"agent inference failed: {exc}")
                if not pending:
                    break
                # Keep long-wait SSE streams alive: gateways/proxies/firewalls
                # drop idle connections, which would silently lose the final
                # timeout-error frames (they ARE persisted, so history replay
                # shows them but the live view never does).
                if deadline is None or time.monotonic() < deadline:
                    if sse_heartbeat is not None:
                        try:
                            sse_heartbeat()
                        except Exception:
                            pass
                if deadline is not None and time.monotonic() >= deadline:
                    # At least one agent did not finish in time -- stop waiting
                    # and report it as timed out instead of hanging the whole
                    # group-chat round.  Signal cancellation so any orphaned
                    # worker threads terminate promptly (infer_stream checks
                    # cancel_event at each yield point).
                    if cancel_event is not None:
                        cancel_event.set()
                    for future in pending:
                        aid = futures[future]
                        future.cancel()
                        _logger.error(
                            "group chat agent %s timed out after %ss",
                            aid, f"{future_timeout:g}",
                        )
                        _append_error(
                            aid,
                            f"agent inference timed out after "
                            f"{future_timeout:g}s",
                        )
                    break
        finally:
            # Never block on a still-running worker: pending futures are
            # cancelled and running threads exit via cancel_event or their
            # own internal timeouts (infer_stream is self-terminating).
            executor.shutdown(wait=False, cancel_futures=True)

        all_collected.extend(round_collected)

        # Accumulate into in-memory turns so the next round sees them.
        # usage/stat messages are excluded — they are metadata for
        # merge_stream_messages, never conversation content.
        for msg in round_collected:
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
            })

        # Scan assistant messages (now complete, aggregated messages) for
        # @-mentions that target unprocessed agents — exactly like user
        # messages drive round 1.
        for msg in round_collected:
            if msg.role != "assistant" or not msg.content:
                continue
            parsed = parse_mentions(msg.content)
            if not parsed:
                continue
            resolved = resolve_mentions(parsed, agent_manager, all_agent_ids)
            for aid in resolved:
                if aid not in processed_agent_ids:
                    trigger_msgs.setdefault(aid, []).append(msg)

        pending_mentioned = list(trigger_msgs.keys())

    return all_collected
