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

from runtime.models import Message, InferenceRequest

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
            assistant_id=turn.get("assistant_id"),
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
            assistant_id=getattr(turn, "assistant_id", None),
            mentions=getattr(turn, "mentions", None),
            tool_calls=getattr(turn, "tool_calls", None),
            thinking=getattr(turn, "thinking", None),
            images=getattr(turn, "images", None),
            prompt_template=getattr(turn, "prompt_template", None),
            arguments=getattr(turn, "arguments", None),
            tool_id=getattr(turn, "tool_id", None),
            tool_use_id=getattr(turn, "tool_use_id", None),
        )


def assemble_agent_context(
    agent_id: str,
    agent: dict,
    turns: list,
    current_user_msg: Message,
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
        agents_markdown: Optional AGENTS markdown table to inject into the
            system message (when ``talk_to`` tool is active).
    """
    messages: list[Message] = []

    # 1. Agent's own system prompt
    sys_prompt: str = agent.get("system_prompt", "")
    template_id = agent.get("template_id")
    template_args = agent.get("template_arguments", {})
    if template_id:
        messages.append(Message(
            role="system", content="",
            prompt_template=template_id, arguments=template_args or {},
        ))
    elif sys_prompt:
        messages.append(Message(role="system", content=sys_prompt))

    # 1b. Inject AGENTS placeholder if talk_to is active
    if agents_markdown:
        if messages and messages[-1].role == "system":
            sys_msg = messages[-1]
            if sys_msg.arguments is None:
                sys_msg.arguments = {}
            if not sys_msg.arguments.get("AGENTS"):
                sys_msg.arguments["AGENTS"] = agents_markdown
        else:
            messages.append(Message(
                role="system", content="",
                arguments={"AGENTS": agents_markdown},
            ))

    # 2. Catch-up note (if any)
    missed = _count_missed_rounds(agent_id, turns)
    if missed > 0:
        messages.append(Message(role="system", content=(
            f"📢 群聊回溯：在你未 @ 参与期间共有 {missed} 轮对话。"
            f"以下为完整历史记录，其中标注 [群聊] 的消息并非 @ 你，供参考上下文。"
        )))

    # 3. Historical turns (all included, non-mentioned user messages marked)
    for turn in turns:
        msg = _message_from_turn(turn)
        # If this is a user message with explicit mentions that exclude this agent,
        # add a lightweight marker
        turn_mentions: Optional[list[str]] = getattr(turn, "mentions", None)
        if turn_mentions and msg.role == "user" and agent_id not in turn_mentions:
            msg.content = f"[群聊] {msg.content}"
        messages.append(msg)

    # 4. Current user message (always addressed — the agent was @-mentioned in it)
    messages.append(current_user_msg)

    return messages


# ---------------------------------------------------------------------------
# Multi-agent parallel orchestration
# ---------------------------------------------------------------------------


def run_group_chat_stream(
    *,
    runtime,
    mentioned_agent_ids: list[str],
    original_messages: list[Message],
    base_request: InferenceRequest,
    cancel_event,
    sse_callback,
    context_manager,
    session_id: str,
    agent_manager,
    model_id: str,
    tool_ids: list[str],
) -> list[Message]:
    """Execute one round of group chat inference.

    Called from ``_handle_infer_stream`` when group-chat routing is active.

    Parameters:
        runtime: Runtime instance for model inference.
        mentioned_agent_ids: Agent IDs that should respond this round.
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
        ``assistant_id`` and ``name``.
    """
    # Load conversation history
    existing_turns = []
    if session_id:
        try:
            existing_turns = context_manager.load_conversation(session_id)
        except (FileNotFoundError, ValueError):
            pass

    # Build AGENTS markdown table when talk_to is active
    agents_markdown = ""
    if "talk_to" in tool_ids:
        rows = []
        for aid in mentioned_agent_ids:
            agent = agent_manager.get(aid) if agent_manager else None
            if agent is None:
                continue
            nickname = agent.get("nickname", aid)
            desc = (agent.get("description") or "").replace("\n", " ")
            rows.append((nickname, aid, desc))
        if rows:
            lines = [
                "| Nickname | Agent ID | Description |",
                "| --- | --- | --- |",
            ]
            for nick, aid, desc in rows:
                lines.append(f"| {nick} | {aid} | {desc} |")
            agents_markdown = "\n".join(lines)

    # Find the last user message from original_messages
    current_user_msg: Optional[Message] = None
    for m in reversed(original_messages):
        if m.role == "user":
            current_user_msg = m
            break

    if current_user_msg is None:
        # No user message — fall back to empty context
        current_user_msg = Message(role="user", content="")

    all_collected: list[Message] = []

    def run_one(agent_id: str) -> tuple[str, list[Message]]:
        """Run inference for a single agent and return collected messages."""
        agent = agent_manager.get(agent_id)
        if agent is None:
            return (agent_id, [])

        nickname: str = agent.get("nickname", agent_id)
        agent_model_id: str = agent.get("model_id", model_id)
        agent_tool_ids: list[str] = agent.get("tool_ids", tool_ids)

        # Build per-agent context
        agent_messages = assemble_agent_context(
            agent_id, agent, existing_turns, current_user_msg,
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
            for msg in runtime.infer_stream(request, cancel_event=cancel_event):
                # Tag output with agent identity
                if msg.role == "assistant":
                    msg.assistant_id = agent_id
                    msg.name = nickname

                collected.append(msg)

                # SSE: skip delegate/talk_to tool frames (they self-manage)
                if msg.role == "tool" and msg.name in ("delegate", "talk_to"):
                    continue

                if sse_callback:
                    frame = msg.to_dict()
                    frame["agent_id"] = agent_id
                    frame["nickname"] = nickname
                    try:
                        sse_callback(frame)
                    except Exception:
                        pass

        except Exception as exc:
            import logging
            logger = logging.getLogger("runtime.group_chat")
            logger.error("agent %s inference failed: %s", agent_id, exc)
            error_msg = Message(
                role="assistant",
                content=f"Error: {exc}",
                assistant_id=agent_id,
                name=nickname,
            )
            collected.append(error_msg)
            if sse_callback:
                try:
                    frame = error_msg.to_dict()
                    frame["agent_id"] = agent_id
                    frame["nickname"] = nickname
                    sse_callback(frame)
                except Exception:
                    pass

        return (agent_id, collected)

    # Run all mentioned agents in parallel
    max_workers_env = os.environ.get(
        "MAX_GROUP_CHAT_WORKERS",
        os.environ.get("MAX_TALK_TO_WORKERS", "10"),
    )
    max_workers = int(max_workers_env)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(run_one, aid): aid
            for aid in mentioned_agent_ids
        }
        for future in as_completed(futures):
            _aid, msgs = future.result()
            all_collected.extend(msgs)

    return all_collected
