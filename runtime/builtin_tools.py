"""Built-in tools for the Composable Agent Runtime.

Provides basic tools (bash, fetch) that are always available to the LLM,
especially after Skill progressive disclosure when the LLM needs to
execute commands described in SKILL.md.

These tools use only Python standard library modules.
"""

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import datetime
import urllib.request
import urllib.error
import uuid
from typing import Optional

logger = logging.getLogger("runtime.builtin_tools")

if sys.platform != "win32":
    import fcntl
    import pty
    import select
    import struct
    import termios

from runtime.models import InferenceRequest, Message, ToolConfig
from runtime.registry import ToolRegistry

_thread_local = threading.local()


def _bash_execute(command: str, cwd: str = "") -> str:
    """Execute a shell command via a pseudo-TTY so programs behave as if
    running in an interactive terminal (spinner text, color, login prompts, etc.).
    On Windows, falls back to subprocess.run (no PTY support yet).

    Args:
        command: The shell command to execute.
        cwd: Working directory for the command. Empty string means current dir.
    """
    timeout = int(os.environ.get("BASH_EXEC_TIMEOUT", 300))

    if sys.platform == "win32":
        # TODO: add Windows PTY support (e.g. via ConPTY / PowerShell)
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=timeout, cwd=cwd if cwd else None,
            )
            output = result.stdout.strip()
            if result.returncode != 0:
                err = result.stderr.strip()
                return f"Exit code {result.returncode}\nstderr: {err}\nstdout: {output}"
            return output if output else "(empty output)"
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {timeout}s"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"

    output_chunks = []

    try:
        master_fd, slave_fd = pty.openpty()

        # Set terminal size to 80x24 so apps don't complain
        winsize = struct.pack("HHHH", 24, 80, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            cwd=cwd if cwd else None,
        )
        os.close(slave_fd)  # parent doesn't need the slave end

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                return f"Error: command timed out after {timeout}s"
            ready, _, _ = select.select([master_fd], [], [], min(remaining, 0.5))
            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                    if chunk:
                        output_chunks.append(chunk)
                except OSError:
                    break  # slave closed (process exited)
            if proc.poll() is not None:
                # Drain any remaining output
                while True:
                    ready, _, _ = select.select([master_fd], [], [], 0.1)
                    if not ready:
                        break
                    try:
                        chunk = os.read(master_fd, 4096)
                        if chunk:
                            output_chunks.append(chunk)
                    except OSError:
                        break
                break

        os.close(master_fd)
        proc.wait()

        raw = b"".join(output_chunks).decode("utf-8", errors="replace")
        # Strip ANSI/VT escape sequences, keep plain text
        clean = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][AB012]|\r", "", raw).strip()

        if proc.returncode != 0:
            return f"Exit code {proc.returncode}\n{clean}" if clean else f"Exit code {proc.returncode}"
        return clean if clean else "(empty output)"

    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


def _fetch_url(url: str, method: str = "GET", body: str = "",
               headers: str = "{}") -> str:
    """Fetch a URL via HTTP.

    Args:
        url: The URL to fetch.
        method: HTTP method (GET, POST, etc.).
        body: Request body string (for POST/PUT).
        headers: JSON string of additional headers.
    """
    try:
        parsed_headers = json.loads(headers) if headers else {}
    except (json.JSONDecodeError, ValueError):
        parsed_headers = {}

    try:
        body_bytes = body.encode("utf-8") if body else None
        req = urllib.request.Request(url, data=body_bytes, method=method.upper())
        for k, v in parsed_headers.items():
            req.add_header(k, v)

        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            max_size = int(os.environ.get("FETCH_MAX_SIZE", 262144))
            return data[:max_size] if len(data) > max_size else data
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            pass
        return f"HTTP {e.code}: {e.reason}\n{err_body}"
    except urllib.error.URLError as e:
        return f"Error: {e.reason}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


# Tool configs for built-in tools
BASH_TOOL_CONFIG = ToolConfig(
    tool_id="bash",
    tool_type="function",
    name="bash",
    description="Execute a shell command. Use cwd to set the working directory.",
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory for the command (optional)",
            },
        },
        "required": ["command"],
    },
    builtin=True,
)

FETCH_TOOL_CONFIG = ToolConfig(
    tool_id="fetch",
    tool_type="function",
    name="fetch",
    description="Fetch a URL via HTTP. Supports GET, POST, etc.",
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch",
            },
            "method": {
                "type": "string",
                "description": "HTTP method (GET, POST, etc.)",
            },
            "body": {
                "type": "string",
                "description": "Request body (for POST/PUT)",
            },
            "headers": {
                "type": "string",
                "description": "JSON string of additional HTTP headers",
            },
        },
        "required": ["url"],
    },
    builtin=True,
)

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
                "description": "SubAgent 使用的模型 ID，必须已在 ModelRegistry 中注册",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "SubAgent 可用的工具名称列表（使用工具的 name 字段）。传入空数组表示纯推理模式（不使用任何工具）",
            },
            "task": {
                "type": "string",
                "description": "委派给 SubAgent 的任务描述，作为 user 角色消息",
            },
            "context": {
                "type": "string",
                "description": "（可选）SubAgent 的系统提示词，作为 system 角色消息插入对话首条。如果是继续前一次 SubAgent 会话，传入 `[continue]`",
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
        "required": ["model_id", "tools", "task"],
    },
    builtin=True,
)


def resolve_tool_ids(tools: list[str] | str, scope: list) -> list[str]:
    """将工具 name 列表解析为 tool_id 列表，仅在 scope 内查找。

    大模型生成的工具名来自 ToolConfig.name，而 InferenceRequest 需要 tool_id。
    本函数只在当前请求的 scope（即发送给模型的工具集）内按 name 匹配，
    避免全局 registry 中同名工具导致的歧义。
    找不到对应工具的 name 会被跳过并记录警告。

    兼容大模型输出不稳定的情况：
    - tools 可能是字符串而非列表，如 "bash, ppt-master" 或 "[bash, ppt-master]"
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



BUILTIN_TOOLS = [
    (BASH_TOOL_CONFIG, _bash_execute),
    (FETCH_TOOL_CONFIG, _fetch_url),
]


def _make_delegate_fn(runtime, thread_local):
    """创建 delegate 工具的可调用函数。

    Args:
        runtime: Runtime 实例，用于执行 SubAgent 推理
        thread_local: threading.local 实例，用于读取上下文信息

    Returns:
        delegate 可调用函数
    """
    def delegate(model_id: str, tools: list[str], task: str, context: str = "", images: list[str] | None = None) -> str:
        tool_call_id = "call_" + uuid.uuid4().hex[:8]
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
            if '[continue]' in context and thread_local.last_session_id:
                sub_session_id = thread_local.last_session_id
                if context_manager is not None:
                    try:
                        messages = context_manager.load(sub_session_id)
                    except Exception as load_err:
                        logger.warning("delegate: 恢复 SubAgent 会话历史失败: %s", load_err)
            elif session_id is not None:
                sub_ts = datetime.datetime.now().strftime("%y%m%d_%H%M%S")
                sub_session_id = f"{session_id}-sub_{sub_ts}"
                if context:
                    messages.append(Message(role="system", content=context))

            messages.append(Message(role="user", content=task, images=images))
            request = InferenceRequest(
                model_id=model_id,
                tool_ids=resolved_ids,
                messages=messages,
                max_tool_rounds=int(os.environ.get("MAX_TOOL_ROUNDS", 20))
            )

            # 保存旧值，切换到子 session 上下文
            old_depth = current_depth
            old_session_id = session_id
            thread_local.depth = current_depth + 1
            if sub_session_id is not None:
                thread_local.session_id = sub_session_id

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
                                    "tool_call_id": tool_call_id,
                                    "streaming": True,
                                    "delta": msg.content,
                                    "depth": current_depth + 1,
                                })
                            except Exception:
                                pass  # SSE 写入失败不中断推理
            finally:
                # 恢复 depth、tool_scope 和 session_id
                thread_local.depth = old_depth
                thread_local.session_id = old_session_id
                if sub_session_id is not None:
                    thread_local.last_session_id = sub_session_id

            result = "".join(chunks)

            # 推送结束帧：通知前端流式消息框已完成，并重置 assistant 消息索引
            # 不携带 content（内容已通过流式增量帧完整推送），仅作状态信号
            if sse_callback is not None:
                try:
                    sse_callback({
                        "role": "tool",
                        "name": "delegate",
                        "tool_call_id": tool_call_id,
                        "streaming": False,
                        "content": "",
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
                    exc = persist_conversation(
                        context_manager=sub_cm,
                        session_id=short_sub_id,
                        original_messages=messages,
                        collected_messages=collected_msgs,
                        session_manager=None,  # sub session 不更新顶层 index
                        tool_ids=resolved_ids,
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


def _no_runtime_delegate(**kwargs) -> str:
    """当 runtime 未提供时的 delegate 占位函数，向后兼容。"""
    return "Error: delegate tool requires a Runtime instance. Pass runtime= to register_builtin_tools()."


def register_builtin_tools(tool_registry: ToolRegistry, runtime=None) -> list[str]:
    """Register all built-in tools into the given ToolRegistry.

    When runtime is None, the delegate tool is registered but its callable
    returns an error string when called (backward compatibility).

    Args:
        tool_registry: The ToolRegistry to register tools into.
        runtime: Optional Runtime instance for the delegate tool. If None,
            delegate tool is registered with a no-op callable.

    Returns:
        List of registered tool_ids.
    """
    ids = []
    for config, fn in BUILTIN_TOOLS:
        tool_registry.register(config, callable_fn=fn)
        ids.append(config.tool_id)

    # Register delegate tool with runtime-aware callable
    if runtime is not None:
        callable_fn = _make_delegate_fn(runtime, _thread_local)
    else:
        callable_fn = _no_runtime_delegate
    tool_registry.register(DELEGATE_TOOL_CONFIG, callable_fn=callable_fn)
    ids.append("delegate")

    return ids
