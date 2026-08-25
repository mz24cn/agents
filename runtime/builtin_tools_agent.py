"""Built-in agent-invocation tools for the Agent Service.

This module holds the agent-invocation built-in tools:

  - delegate: dispatch a sub-task to an independent SubAgent
  - talk_to:   send a private message to one or more agents
  - resolve_tool_ids: name -> tool_id resolution used by delegate

The tool configs and factory functions are consumed by
``register_builtin_tools`` in ``runtime.builtin_tools`` (the facade module).
"""

import logging
import os
import re

from runtime.models import InferenceRequest, Message, ToolConfig
from runtime.common import session_timestamp, snapshot_request_context, restore_request_context
from runtime.group_chat import build_agents_markdown, _GC_DEFAULT_PROMPT

logger = logging.getLogger("runtime.builtin_tools")

_MISSING = object()


def _restore_thread_attr(thread_local, name: str, previous) -> None:
    """Restore a thread-local attribute, including its prior absence."""
    if previous is _MISSING:
        try:
            delattr(thread_local, name)
        except AttributeError:
            pass
    else:
        setattr(thread_local, name, previous)

DELEGATE_TOOL_CONFIG = ToolConfig(
    tool_id="delegate",
    tool_type="function",
    name="delegate",
    description=(
        "将子任务委派给独立的 SubAgent 执行。SubAgent 使用指定的模型和工具集，"
        "独立完成任务后返回最终文本结果。适用于任务分解、专用模型调用、并行子任务等场景。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "model_id": {
                "type": "string",
                "description": "SubAgent 使用的模型 ID。缺省值用 `default`。",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "（可选）SubAgent 可用的工具名称列表。",
            },
            "task": {
                "type": "string",
                "description": "委派给 SubAgent 的任务描述，作为 user 角色消息。",
            },
            "context": {
                "type": "string",
                "description": "（可选）SubAgent 的系统提示词，作为 system 角色消息插入对话首条。如果是继续前一次 SubAgent 会话，传入 `[continue]`。",
            },
            "images": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "（可选）传递给 SubAgent 的图片列表。"
                    "每个元素可以是：本地文件路径（/path/to/img.png）、"
                    "HTTP/HTTPS URL（https://...）、或 base64 编码字符串。"
                    "适用于需要 VLM 处理图片的场景。"
                ),
            },
        },
        "required": ["model_id", "task"],
    },
    builtin=True,
    labels=["long-execution"],
)

TALK_TO_TOOL_CONFIG = ToolConfig(
    tool_id="talk_to",
    tool_type="function",
    name="talk_to",
    description=(
        "向一个或多个 Agent 发送私密消息并获取回复。"
        "每个目标 Agent 独立处理消息后返回文本结果。"
        "适用于需要与其他 Agent 一对一/一对多私下沟通的场景。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "agents": {
                "type": "array",
                "items": {"type": "string"},
                "description": "目标 Agent 的 ID 列表，如 ['alice', 'bob']。",
            },
            "message": {
                "type": "string",
                "description": "要发送给目标 Agent 的消息内容。",
            },
        },
        "required": ["agents", "message"],
    },
    builtin=True,
    labels=["long-execution"],
)
def resolve_tool_ids(tools: list[str] | str, scope: list) -> list[str]:
    """将工具 name 列表解析为 tool_id 列表，仅在 scope 内查找。

    大模型生成的工具名来自 ToolConfig.name，而 InferenceRequest 需要 tool_id。
    本函数只在当前请求的 scope（即发送给模型的工具集）内按 name 匹配，
    避免全局 registry 中同名工具导致的歧义。
    找不到对应工具的 name 会被跳过并记录警告。

    兼容大模型输出不稳定的情况：
    - tools 可能是字符串而非列表，如 "exec_cli, ppt-master" 或 "[exec_cli, ppt-master]"
    - 单个工具名可能携带多余的括号、引号、空格等噪声字符

    Args:
        tools: 工具 name 列表，或逗号/空格分隔的工具名字符串
        scope: 当前请求的 ToolConfig 列表（即 infer_stream 构建的 tools）

    Returns:
        对应的 tool_id 列表（顺序与输入一致，跳过未找到的项）
    """
    # 兼容：tools 整体是字符串（大模型未按 array 格式输出）
    if isinstance(tools, str):
        # 去掉首尾的 [ ] 括号，再按逗号或空格分割
        tools = tools.strip().strip("[]")
        tools = [t for t in re.split(r"[,\s]+", tools) if t]

    name_to_id = {tc.name: tc.tool_id for tc in scope}
    tool_ids = []
    for name in tools:
        # 兼容：单个工具名携带多余的括号、引号、空格等噪声字符
        name = re.sub(r"[^\w\-]", "", str(name).strip())
        if not name:
            continue
        if name in name_to_id:
            tool_ids.append(name_to_id[name])
        else:
            raise ValueError(f"工具 {name!r} 在当前 scope 中不存在，请检查工具名称是否正确")
    return tool_ids
def _make_delegate_fn(runtime, thread_local):
    """创建 delegate 工具的可调用函数。

    Args:
        runtime: Runtime 实例，用于执行 SubAgent 推理
        thread_local: threading.local 实例，用于读取上下文信息

    Returns:
        delegate 可调用函数
    """
    def delegate(model_id: str, tools: list[str], task: str, context: str = "", images: list[str] | None = None) -> str:
        tool_use_id = getattr(thread_local, "tool_use_id", None)
        session_id = getattr(thread_local, "session_id", None)
        current_depth = getattr(thread_local, "depth", 0)
        sse_callback = getattr(thread_local, "sse_callback", None)
        tool_scope = getattr(thread_local, "tool_scope", [])
        context_manager = getattr(thread_local, "context_manager", None)
        sub_session_manager = getattr(thread_local, "session_manager", None)

        try:
            resolved_ids = resolve_tool_ids(tools, tool_scope)
            messages = []
            # 生成子 session_id：{parent_session_id}-sub_YYMMDD_HHmmss
            sub_session_id = None
            if '[continue]' in context and getattr(thread_local, 'last_session_id', None):
                sub_session_id = thread_local.last_session_id
                if context_manager is not None:
                    try:
                        messages = context_manager.load(sub_session_id)
                    except Exception as load_err:
                        logger.warning("delegate: 恢复 SubAgent 会话历史失败: %s", load_err)
            elif session_id is not None:
                sub_ts = session_timestamp()
                sub_session_id = f"{session_id}-sub_{sub_ts}"
                if context:
                    messages.append(Message(role="system", content=context))

            messages.append(Message(role="user", content=task, images=images))
            request = InferenceRequest(
                model_id=model_id,
                tool_ids=resolved_ids,
                messages=messages,
                max_tool_rounds=int(os.environ.get("MAX_TOOL_ROUNDS", 200))
            )

            # 保存旧值，切换到子 session 上下文
            old_depth = current_depth
            old_session_id = session_id
            old_session_dir = getattr(thread_local, "session_dir", None)
            old_file_journal_manager = getattr(thread_local, "file_journal_manager", None)
            old_user_message_timestamp = getattr(thread_local, "user_message_timestamp", None)
            old_journal_session_id = getattr(thread_local, "file_journal_session_id", _MISSING)
            old_journal_session_dir = getattr(thread_local, "file_journal_session_dir", _MISSING)
            old_journal_timestamp = getattr(thread_local, "file_journal_user_message_timestamp", _MISSING)
            journal_session_id = (session_id if old_journal_session_id is _MISSING
                                  else old_journal_session_id)
            journal_session_dir = (old_session_dir if old_journal_session_dir is _MISSING
                                   else old_journal_session_dir)
            journal_timestamp = (old_user_message_timestamp if old_journal_timestamp is _MISSING
                                 else old_journal_timestamp)
            thread_local.depth = current_depth + 1
            if sub_session_id is not None:
                thread_local.session_id = sub_session_id
                if old_session_dir is not None and session_id and sub_session_id.startswith(f"{session_id}-"):
                    thread_local.session_dir = os.path.join(old_session_dir, sub_session_id[len(session_id) + 1:])
                else:
                    thread_local.session_dir = None
                thread_local.file_journal_manager = None
                # Keep delegate's conversation isolated in the sub-session, but
                # attribute all file changes to the initiating parent user turn.
                thread_local.file_journal_session_id = journal_session_id
                thread_local.file_journal_session_dir = journal_session_dir
                thread_local.file_journal_user_message_timestamp = journal_timestamp

            chunks = []
            collected_msgs = []
            try:
                cancel_event = getattr(thread_local, "cancel_event", None)
                for msg in runtime.infer_stream(request, cancel_event=cancel_event):
                    collected_msgs.append(msg)
                    if msg.role == "assistant" and msg.content:
                        chunks.append(msg.content)
                        # 推送流式增量帧
                        if sse_callback is not None:
                            try:
                                sse_callback({
                                    "role": "tool",
                                    "name": "delegate",
                                    "tool_use_id": tool_use_id,
                                    "streaming": True,
                                    "delta": msg.content,
                                    "depth": current_depth + 1,
                                })
                            except Exception:
                                pass  # SSE 写入失败不中断推理
            finally:
                sub_journal_manager = getattr(thread_local, "file_journal_manager", None)
                if sub_journal_manager is not None:
                    try:
                        sub_journal_manager.flush()
                    except Exception as journal_err:
                        logger.warning("delegate: failed to finalize file journal: %s", journal_err)
                # 恢复 depth、tool_scope、session_id、session_dir 和用户消息时间戳
                thread_local.depth = old_depth
                thread_local.session_id = old_session_id
                thread_local.session_dir = old_session_dir
                thread_local.file_journal_manager = old_file_journal_manager
                thread_local.user_message_timestamp = old_user_message_timestamp
                _restore_thread_attr(thread_local, "file_journal_session_id", old_journal_session_id)
                _restore_thread_attr(thread_local, "file_journal_session_dir", old_journal_session_dir)
                _restore_thread_attr(thread_local, "file_journal_user_message_timestamp", old_journal_timestamp)
                if sub_session_id is not None:
                    thread_local.last_session_id = sub_session_id

            result = "".join(chunks)

            # 推送结束帧：通知前端流式消息框已完成，并重置 assistant 消息索引
            # 不携带 content（内容已通过流式增量帧完整推送），仅作状态信号
            # 注意：切勿添加 content 字段，即使是空字符串也会被前端展开覆盖已累积的内容
            if sse_callback is not None:
                try:
                    sse_callback({
                        "role": "tool",
                        "name": "delegate",
                        "tool_use_id": tool_use_id,
                        "streaming": False,
                    })
                except Exception:
                    pass

            # 持久化 SubAgent Session
            persistence_warning = ""
            if context_manager is not None and sub_session_id is not None:
                try:
                    from runtime.server import persist_conversation
                    # 子 session 目录路径：chats_dir/<parent>/<sub_session_id 中 - 替换为 />
                    sub_session_dir = os.path.join(
                        context_manager._chats_dir,
                        sub_session_id.replace("-", os.sep),
                    )
                    os.makedirs(sub_session_dir, exist_ok=True)
                    import copy as _copy
                    sub_cm = _copy.copy(context_manager)
                    # sub_cm 的 _chats_dir 指向父 session 目录，sub_session_id 只含最后一段
                    sub_cm._chats_dir = os.path.join(
                        context_manager._chats_dir,
                        session_id.replace("-", os.sep),
                    )
                    sub_cm._memory_store = {}  # 隔离 memory 缓存，避免污染父 context_manager
                    # persist 时使用不含父路径前缀的短 ID（最后一段）
                    short_sub_id = sub_session_id[len(session_id) + 1:]  # "sub_YYMMDD_HHmmss"
                    sub_agent_ids = getattr(thread_local, "agent_ids", None) or None
                    sub_model_id = model_id or getattr(thread_local, "model_id", None) or None
                    exc = persist_conversation(
                        context_manager=sub_cm,
                        session_id=short_sub_id,
                        original_messages=messages,
                        collected_messages=collected_msgs,
                        session_manager=None,  # sub session 不更新顶层 index
                        tool_ids=resolved_ids,
                        agent_ids=sub_agent_ids,
                        model_id=sub_model_id,
                        extra_meta={"parent_session_id": session_id},
                    )
                    if exc is not None:
                        raise exc
                except Exception as persist_err:
                    logger.warning("delegate: 持久化 SubAgent Session 失败: %s", persist_err)
                    persistence_warning = f" [Warning: session persistence failed: {persist_err}]"

            return result + persistence_warning
        except Exception as e:
            return f"Error: delegate failed: {e}"

    return delegate

def _make_talk_to_fn(runtime, thread_local):
    """创建 talk_to 工具的可调用函数。

    talk_to 是对 delegate 的简化，通过 Agent 的 ID 或 labels 定位目标，
    向其发送私密消息并获取回复。支持同时向多个 Agent 并行发送。

    与 delegate 的核心区别：
    - 不需要手动指定 model_id / tools / context —— 从 Agent 注册信息自动获取
    - 不需要 images 参数 —— 视觉处理由 Agent 自己的工具完成
    - 多个目标 Agent 并行推理，子会话各自独立持久化
    - 返回格式：每个 Agent 的结果用 **nickname** (agent_id): 前缀标注

    Args:
        runtime: Runtime 实例，用于执行 SubAgent 推理
        thread_local: threading.local 实例，用于读取上下文信息

    Returns:
        talk_to 可调用函数
    """
    def talk_to(agents: list[str], message: str) -> str:
        tool_use_id = getattr(thread_local, "tool_use_id", None)

        # 捕获父线程上下文（子线程中 thread_local 是隔离的，需要显式传递）
        parent_session_id = getattr(thread_local, "session_id", None)
        parent_depth = getattr(thread_local, "depth", 0)
        parent_sse_callback = getattr(thread_local, "sse_callback", None)
        agent_manager = getattr(thread_local, "agent_manager", None)
        context_manager = getattr(thread_local, "context_manager", None)
        cancel_event = getattr(thread_local, "cancel_event", None)
        all_agent_ids: list[str] = getattr(thread_local, "all_agent_ids", None) or []
        caller_agent_id: str = getattr(thread_local, "agent_id", None) or ""
        parent_request_context = snapshot_request_context()
        journal_session_id = parent_request_context.get(
            "file_journal_session_id", parent_session_id)
        journal_session_dir = parent_request_context.get(
            "file_journal_session_dir", parent_request_context.get("session_dir"))
        journal_timestamp = parent_request_context.get(
            "file_journal_user_message_timestamp",
            parent_request_context.get("user_message_timestamp"),
        )

        if agent_manager is None:
            return "Error: talk_to requires an AgentManager. Ensure agent_manager is set in the request context."

        # 第一阶段：解析所有 agent 名称（保持原始顺序）
        resolved: list[tuple[str, dict | None]] = []
        for name in agents:
            agent = agent_manager.get(name)
            if agent is not None:
                resolved_agent_id = agent.get("agent_id")
                if all_agent_ids and resolved_agent_id not in all_agent_ids:
                    agent = None
            resolved.append((name, agent))

        # 用于收集并行结果
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def run_one(agent_name: str, agent: dict | None):
            """在子线程中运行单个 agent 的推理，完成后持久化。"""
            if agent is None:
                return (
                    agent_name,
                    agent_name,
                    "Error: specified agent does not exist in the current "
                    "conversation or has left.",
                    None,
                )

            agent_id = agent["agent_id"]
            model_id = agent.get("model_id", "default")
            agent_tool_ids = agent.get("tool_ids", [])
            system_prompt = agent.get("system_prompt", "")
            nickname = agent.get("nickname", agent_id)

            # Never recurse into the calling agent itself. Prompt-side rosters
            # normally exclude self, but group-chat framing lists all
            # participants and models can still emit a self-targeting call.
            # Enforce the invariant at execution time as the final guard.
            if caller_agent_id and agent_id == caller_agent_id:
                return (
                    agent_id,
                    nickname,
                    "Error: talk_to cannot target the calling agent itself.",
                    None,
                )

            # 生成子 session_id（每个 agent 独立）
            sub_session_id = None
            if parent_session_id is not None:
                sub_ts = session_timestamp()
                sub_session_id = f"{parent_session_id}-talk_{sub_ts}_{agent_id}"

            # Inherit the parent's complete request context.  In particular,
            # keep session_dir and the parent's user_message_timestamp so file
            # changes made by talk_to are journaled on the visible parent turn.
            # Conversation persistence still uses the independent sub_session_id
            # below; only the file journal belongs to the initiating user turn.
            child_context = dict(parent_request_context)
            tool_registry = getattr(runtime, "_tool_registry", None)
            target_tool_scope = (
                [
                    tc for tid in agent_tool_ids
                    if (tc := tool_registry.get(tid)) is not None
                ]
                if tool_registry is not None
                else []
            )
            child_context.update({
                "depth": parent_depth + 1,
                "session_id": sub_session_id,
                "session_dir": None,
                "file_journal_manager": None,
                "user_message_timestamp": None,
                # Nested tool calls belong to the target agent, not to the
                # parent that initiated this talk_to call.
                "agent_id": agent_id,
                "tool_scope": target_tool_scope,
                "available_tool_ids": list(agent_tool_ids),
                "file_journal_session_id": journal_session_id,
                "file_journal_session_dir": journal_session_dir,
                "file_journal_user_message_timestamp": journal_timestamp,
            })
            restore_request_context(child_context)
            # tool_scope comes from the target agent's current tool list in the
            # restored child context; only shared managers/callbacks are copied.
            thread_local.agent_manager = agent_manager
            thread_local.context_manager = context_manager
            thread_local.sse_callback = parent_sse_callback
            thread_local.cancel_event = cancel_event
            thread_local.all_agent_ids = all_agent_ids

            # 构建消息
            messages = []
            # 若子 agent 持有 talk_to 工具且外层提供了 all_agent_ids，
            # 则注入 AGENTS 调度清单（排除子 agent 自己，只列可 talk_to 的目标），
            # 确保子 agent 能正确使用 agent_id
            agents_md = ""
            if "talk_to" in agent_tool_ids and all_agent_ids:
                agents_md = build_agents_markdown(
                    all_agent_ids, agent_manager, exclude_agent_id=agent_id,
                )

            # 检查 template_id（提示词模板 ID+参数）
            template_id = agent.get("template_id")
            template_args = agent.get("template_arguments", {})

            if template_id:
                # 提示词模板方式：使用 prompt_template + arguments
                sys_msg = Message(
                    role="system", content="",
                    prompt_template=template_id, arguments=template_args or {},
                )
                # 注入 AGENTS 和 GC_FRAMING 到 arguments 中，供模板渲染使用
                if agents_md:
                    if sys_msg.arguments is None:
                        sys_msg.arguments = {}
                    if not sys_msg.arguments.get("AGENTS"):
                        sys_msg.arguments["AGENTS"] = agents_md
                    _GC_FRAMING = "\n\n" + _GC_DEFAULT_PROMPT
                    if not sys_msg.arguments.get("GC_FRAMING"):
                        sys_msg.arguments["GC_FRAMING"] = _GC_FRAMING.replace("{{AGENTS}}\n\n", "")
                messages.append(sys_msg)
            elif system_prompt:
                if agents_md:
                    gc_prompt = _GC_DEFAULT_PROMPT.replace("{{AGENTS}}", agents_md)
                    # 避免重复注入
                    if gc_prompt not in system_prompt:
                        system_prompt = system_prompt + "\n\n" + gc_prompt
                # 若知道调用者身份，告知当前 agent 对方是谁
                if caller_agent_id and agents_md:
                    caller_agent = agent_manager.get(caller_agent_id)
                    if caller_agent:
                        caller_nickname = caller_agent.get("nickname", caller_agent_id)
                        system_prompt += "\n\n用户的角色是 " + caller_nickname + " (" + caller_agent_id + ")"
                messages.append(Message(role="system", content=system_prompt))
            elif agents_md:
                # 无 system_prompt，直接用默认的群聊提示词
                gc_prompt = _GC_DEFAULT_PROMPT.replace("{{AGENTS}}", agents_md)
                messages.append(Message(role="system", content=gc_prompt))
            messages.append(Message(role="user", content=message))

            request = InferenceRequest(
                model_id=model_id,
                tool_ids=agent_tool_ids,
                messages=messages,
                max_tool_rounds=int(os.environ.get("MAX_TOOL_ROUNDS", 200))
            )

            chunks = []
            collected_msgs = []
            try:
                for msg in runtime.infer_stream(request, cancel_event=cancel_event):
                    collected_msgs.append(msg)
                    if msg.role == "assistant" and msg.content:
                        chunks.append(msg.content)
                        if parent_sse_callback is not None:
                            try:
                                parent_sse_callback({
                                    "role": "tool",
                                    "name": "talk_to",
                                    "tool_use_id": tool_use_id,
                                    "streaming": True,
                                    "delta": msg.content,
                                    "depth": parent_depth + 1,
                                    "agent_id": caller_agent_id,
                                    "target_agent_id": agent_id,
                                    "target_agent_nickname": nickname,
                                })
                            except Exception:
                                pass
            except Exception as exc:
                return (agent_id, nickname, f"Error: {exc}", None)
            finally:
                journal_manager = getattr(thread_local, "file_journal_manager", None)
                if journal_manager is not None:
                    try:
                        journal_manager.flush()
                    except Exception as journal_err:
                        logger.warning("talk_to: failed to finalize file journal: %s", journal_err)

            result = "".join(chunks)

            # 持久化子会话（复用 delegate 的 persist_conversation 机制）
            persistence_info = None
            if context_manager is not None and sub_session_id is not None and parent_session_id is not None:
                try:
                    from runtime.server import persist_conversation
                    import copy as _copy
                    sub_cm = _copy.copy(context_manager)
                    sub_cm._chats_dir = os.path.join(
                        context_manager._chats_dir,
                        parent_session_id.replace("-", os.sep),
                    )
                    sub_cm._memory_store = {}
                    short_sub_id = sub_session_id[len(parent_session_id) + 1:]
                    sub_agent_ids = [agent_id]
                    sub_model_id = model_id
                    exc = persist_conversation(
                        context_manager=sub_cm,
                        session_id=short_sub_id,
                        original_messages=messages,
                        collected_messages=collected_msgs,
                        session_manager=None,  # 子会话不更新顶层 index
                        tool_ids=agent_tool_ids,
                        agent_ids=sub_agent_ids,
                        model_id=sub_model_id,
                        extra_meta={"parent_session_id": parent_session_id},
                    )
                    if exc is not None:
                        raise exc
                    persistence_info = short_sub_id
                except Exception as persist_err:
                    logger.warning("talk_to: 持久化 SubAgent Session 失败 (%s): %s",
                                   agent_id, persist_err)

            return (agent_id, nickname, result, persistence_info)

        # 第二阶段：并行执行所有 agent 推理
        all_results: list[tuple[str, str, str, object]] = []
        with ThreadPoolExecutor(max_workers=min(len(resolved), 8)) as executor:
            future_map = {}
            for name, agent in resolved:
                future = executor.submit(run_one, name, agent)
                future_map[future] = name

            for future in as_completed(future_map):
                try:
                    agent_id, nickname, result, persisted = future.result()
                    all_results.append((agent_id, nickname, result, persisted))
                except Exception as exc:
                    name = future_map[future]
                    all_results.append((name, name, f"Error: {exc}", None))

        # 按原始 agents 参数顺序排列结果
        result_by_agent = {agent_id: (nickname, result)
                           for agent_id, nickname, result, _ in all_results}
        ordered: list[str] = []
        for name, agent in resolved:
            aid = agent["agent_id"] if agent else name
            nickname, result = result_by_agent.get(aid, (name, f"Error: Agent not found."))
            ordered.append(f"**{nickname}** ({aid}): {result}")

        # 推送结束帧
        if parent_sse_callback is not None:
            try:
                parent_sse_callback({
                    "role": "tool",
                    "name": "talk_to",
                    "tool_use_id": tool_use_id,
                    "streaming": False,
                    "agent_id": caller_agent_id,
                })
            except Exception:
                pass

        return "\n".join(ordered)

    return talk_to
def _no_runtime_delegate(**kwargs) -> str:
    """当 runtime 未提供时，delegate / talk_to / read_image 工具的占位函数，向后兼容。"""
    return "Error: this tool requires a Runtime instance. Pass runtime= to register_builtin_tools()."
