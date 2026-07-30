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

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from runtime.common import set_request_context
from runtime.models import Message, InferenceRequest

# ---------------------------------------------------------------------------
# AGENTS markdown table builder (shared between persist & inference)
# ---------------------------------------------------------------------------


def build_agents_markdown(
    agent_ids: list[str],
    agent_manager,
) -> str:
    """Build a markdown table listing all participants in a group chat.

    Returns an empty string if no agents can be resolved.
    """
    rows = []
    for aid in agent_ids:
        agent = agent_manager.get(aid) if agent_manager else None
        if agent is None:
            continue
        nickname = agent.get("nickname", aid)
        desc = (agent.get("description") or "").replace("\n", " ")
        rows.append((nickname, aid, desc))
    if not rows:
        return ""
    lines = [
        "| Nickname | Agent ID | Description |",
        "| --- | --- | --- |",
    ]
    for nick, aid, desc in rows:
        lines.append(f"| {nick} | {aid} | {desc} |")
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
        if turn.role == "user":
            mentions: Optional[list[str]] = getattr(turn, "mentions", None)
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
        *tool_calls* we append a brief ``(调用了: foo, bar)`` note
       (purely from the assistant message, no tool results needed).
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

                # Append brief tool-call note if any
                tool_calls = getattr(msg, "tool_calls", None)
                if tool_calls:
                    names = [tc.get("function", {}).get("name", "?")
                             for tc in tool_calls]
                    content += f" _(调用了: {', '.join(names)})_"
            elif raw_role == "tool":
                # Tool result belonging to another agent — just the content
                content = msg.content or ""
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
) -> list[Message]:
    """Build the inference context for one agent in a group chat.

    Delayed-visibility policy (WeChat model):

    * All historical messages are included (full transparency).
    * User messages where *agent_id* was NOT @-mentioned get a ``[群聊]``
      prefix so the agent can distinguish messages addressed to it from
      bystander conversation.
    * If the agent missed prior rounds, a catch-up system message is prepended.

    Args:
        agent_id: The agent this context is being built for.
        agent: Agent record dict (with system_prompt, template_id, etc.).
        turns: ConversationTurn list from conversation.json.
        current_user_msg: The current user message that triggered this round.
        agents_markdown: AGENTS markdown table listing all participants in
            the group chat. Always injected for group-chat contexts.
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

    # 2. Catch-up note (if any) — merged into the first system message
    #    so the model always receives exactly one system message.
    missed = _count_missed_rounds(agent_id, turns)
    if missed > 0:
        catch_up = (
            f"📢 群聊回溯：在你未 @ 参与期间共有 {missed} 轮对话。"
            f"以下为完整历史记录，其中标注 [群聊] 的消息并非 @ 你，供参考上下文。"
        )
        if messages and messages[0].role == "system":
            if messages[0].prompt_template:
                # Template-based: inject via arguments so the template can render
                # {{CATCH_UP}} if it chooses; otherwise merge into GC_FRAMING.
                if messages[0].arguments is None:
                    messages[0].arguments = {}
                messages[0].arguments["CATCH_UP"] = catch_up
                # Also append to GC_FRAMING as fallback in case template lacks {{CATCH_UP}}
                existing_framing = messages[0].arguments.get("GC_FRAMING") or ""
                messages[0].arguments["GC_FRAMING"] = existing_framing + "\n\n" + catch_up
            else:
                # Plain-text system prompt: direct content merge
                messages[0].content = (messages[0].content or "") + "\n\n" + catch_up
        else:
            messages.insert(0, Message(role="system", content=catch_up))

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
        turn_mentions: Optional[list[str]] = getattr(turn, "mentions", None)
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
        context_manager: ContextManager for loading conversation history.
        session_id: Group chat session ID.
        agent_manager: AgentManager for resolving agent configs.
        model_id: Default model ID (may be overridden per agent).
        tool_ids: Default tool IDs (may be overridden per agent).

    Returns:
        Collected Message objects from all agents, already tagged with
        ``agent_id`` and ``name``.
    """
    import logging

    _logger = logging.getLogger("runtime.group_chat")

    agents_markdown = build_agents_markdown(all_agent_ids, agent_manager)
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
    if session_id:
        try:
            existing_turns = context_manager.load_conversation(session_id)
        except (FileNotFoundError, ValueError):
            pass

    all_collected: list[Message] = []
    processed_agent_ids: set[str] = set()
    pending_mentioned: list[str] = [
        aid for aid in mentioned_agent_ids if aid not in processed_agent_ids
    ]
    round_num = 0

    while pending_mentioned and round_num < max_rounds:
        round_num += 1
        for aid in pending_mentioned:
            processed_agent_ids.add(aid)

        # --- current-user message for this round ---------------------------
        cur_user_msg: Optional[Message]
        if round_num == 1 and first_user_msg:
            cur_user_msg = first_user_msg
        else:
            # Round 2+: the trigger is assistant @-mentions, no user message.
            cur_user_msg = None

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

            agent_messages = assemble_agent_context(
                agent_id, agent, existing_turns, cur_user_msg,
                agents_markdown=agents_markdown,
            )

            request = InferenceRequest(
                model_id=agent_model_id,
                tool_ids=agent_tool_ids,
                messages=agent_messages,
                stream=True,
                max_tool_rounds=base_request.max_tool_rounds,
            )

            collected: list[Message] = []
            try:
                for msg in runtime.infer_stream(request,
                                                cancel_event=cancel_event):
                    msg.agent_id = agent_id
                    if msg.role == "assistant":
                        msg.name = nickname

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
                if sse_callback:
                    try:
                        frame = error_msg.to_dict()
                        frame["nickname"] = nickname
                        sse_callback(frame)
                    except Exception:
                        pass

            return (agent_id, collected)

        # --- parallel execution for this round ----------------------------
        round_collected: list[Message] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_run_one, aid): aid
                for aid in pending_mentioned
            }
            for future in as_completed(futures):
                _aid, msgs = future.result()
                round_collected.extend(msgs)

        all_collected.extend(round_collected)

        # Accumulate into in-memory turns so the next round sees them.
        for msg in round_collected:
            existing_turns.append({
                "role": msg.role,
                "content": msg.content or "",
                "timestamp": msg.timestamp,
                "name": getattr(msg, "name", None),
                "agent_id": getattr(msg, "agent_id", None),
                "thinking": getattr(msg, "thinking", None),
                "stat": getattr(msg, "stat", None),
            })

        # Scan assistant messages for @-mentions that target unprocessed agents.
        new_mentions: list[str] = []
        for msg in round_collected:
            if msg.role != "assistant" or not msg.content:
                continue
            parsed = parse_mentions(msg.content)
            if not parsed:
                continue
            resolved = resolve_mentions(parsed, agent_manager, all_agent_ids)
            for aid in resolved:
                if aid not in processed_agent_ids:
                    new_mentions.append(aid)

        pending_mentioned = new_mentions

    return all_collected
