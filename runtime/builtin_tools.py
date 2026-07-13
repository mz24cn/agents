"""Built-in tools for the Agent Service.

Provides basic tools (write_file, exec_shell) that are always available to the LLM,
especially after Skill progressive disclosure when the LLM needs to
execute commands described in SKILL.md.

These tools use only Python standard library modules.
"""

import fnmatch
import gzip
import hashlib
import json
import logging
import os
import re
import shlex
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import datetime
import urllib.request
import urllib.error
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("runtime.builtin_tools")


from runtime.common import get_system_encoding, SYSTEM_ENCODING

if sys.platform != "win32":
    import fcntl
    import pty
    import select
    import struct
    import termios

from runtime.models import InferenceRequest, Message, ToolConfig
from runtime.registry import ToolRegistry
from runtime.common import (
    _thread_local,
    convert_image_to_base64,
    set_request_context,
    get_request_context,
    clear_request_context,
    get_workspace,
    utc_now_iso as _utc_now_iso,
    parse_iso_timestamp as _parse_journal_timestamp,
    sha256_bytes as _sha256_bytes,
    safe_rel_path as _safe_rel_path,
    atomic_write_json as _atomic_write_json,
    session_timestamp,
    kill_process_group,
)

# Shared registry mapping session_id → subprocess.Popen for the currently
# executing exec_cli command.  Populated by _exec_shell so that the abort
# handler (which runs in the HTTPServer thread) can kill the process.
_active_processes: dict[str, subprocess.Popen] = {}
_active_processes_lock = threading.Lock()


def _was_terminated_by_signal(proc: subprocess.Popen) -> bool:
    signal_returncodes = {-signal.SIGTERM}
    sigkill = getattr(signal, "SIGKILL", None)
    if sigkill is not None:
        signal_returncodes.add(-sigkill)
    return proc.returncode in signal_returncodes


def kill_active_process(session_id: str) -> bool:
    """Kill the shell process associated with *session_id*.

    Called from the abort handler in a different thread.  Returns True if a
    process was found and killed, False otherwise.
    """
    with _active_processes_lock:
        proc = _active_processes.pop(session_id, None)
    if proc is None:
        return False
    try:
        # Kill the entire process group so child processes are also terminated.
        kill_process_group(proc)
        return True
    except (ProcessLookupError, OSError):
        return False


def _get_file_journal_manager(workspace: str) -> '_FileJournalManager':
    session_id = get_request_context("session_id")
    user_message_timestamp = get_request_context("user_message_timestamp")
    session_dir = get_request_context("session_dir")

    journal_manager = get_request_context("file_journal_manager")
    if (
        journal_manager is None
        or journal_manager.workspace != workspace
        or journal_manager.session_id != session_id
        or journal_manager.session_dir != session_dir
        or journal_manager.user_message_timestamp != user_message_timestamp
    ):
        journal_manager = _FileJournalManager(
            workspace,
            session_id=session_id,
            user_message_timestamp=user_message_timestamp,
            session_dir=session_dir,
        )
        set_request_context(file_journal_manager=journal_manager)
    return journal_manager


def _exec_cli(
    command: str,
    cwd: str = "",
    prompt_pattern: str = "",
    idle_timeout: int = 1000,
    read_after_delay: int = 0,
) -> str:
    """Execute input via a persistent CLI terminal and return observed screen output.

    Completion is driven by provided parameters:
    - prompt_pattern: regex that completes when it matches visible output.
    - idle_timeout: milliseconds of no new output before returning.
    - read_after_delay: milliseconds to read before returning regardless of output.

    An empty command reads the latest terminal progress without sending input.
    timeout is intentionally not a tool parameter; CLI_EXEC_TIMEOUT is the
    hard safety cap for all completion conditions.
    """
    timeout = int(os.environ.get("CLI_EXEC_TIMEOUT", 300))
    session_id = get_request_context("session_id")
    if session_id:
        try:
            from runtime.server import get_or_create_terminal
            terminal_info = get_or_create_terminal(session_id)
            if terminal_info:
                if cwd and command:
                    command = f"cd {shlex.quote(cwd)} && {command}"
                result = execute_command_in_terminal(
                    session_id,
                    command,
                    timeout=timeout,
                    prompt_pattern=prompt_pattern,
                    idle_timeout=idle_timeout,
                    read_after_delay=read_after_delay,
                )
                if not result.startswith("Error:"):
                    return result
                logger.debug("Terminal execution returned error, falling back to subprocess: %s", result)
        except Exception as e:
            logger.debug("Terminal execution failed, falling back to subprocess: %s", e)

    # Fallback for contexts without a terminal session.
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            encoding=SYSTEM_ENCODING, errors='replace',
            timeout=timeout, cwd=cwd if cwd else None,
        )
        output = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if err:
            return (output + "\n" + err).strip()
        return output if output else "(empty output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
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
CLI_TOOL_CONFIG = ToolConfig(
    tool_id="exec_cli",
    tool_type="function",
    name="exec_cli",
    description=(
        "Execute input in a persistent terminal session and return observed screen output. "
        "Completion is driven by prompt_pattern, idle_timeout, and read_after_delay; "
        "the first satisfied condition returns. Time values are in milliseconds."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The command or terminal input to send via exec_cli. Empty string only reads the latest terminal progress without sending input.",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory for the command (optional, shell commands only)",
            },
            "prompt_pattern": {
                "type": "string",
                "description": "Regex that completes output collection when it matches visible terminal output.",
            },
            "idle_timeout": {
                "type": "integer",
                "description": "Milliseconds of no new output before returning (default 1000). Set to 0 to disable idle completion.",
            },
            "read_after_delay": {
                "type": "integer",
                "description": "Milliseconds to read before returning regardless of output (default 0; disabled).",
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



BUILTIN_TOOLS = [
    (CLI_TOOL_CONFIG, _exec_cli),
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
                max_tool_rounds=int(os.environ.get("MAX_TOOL_ROUNDS", 100))
            )

            # 保存旧值，切换到子 session 上下文
            old_depth = current_depth
            old_session_id = session_id
            old_session_dir = getattr(thread_local, "session_dir", None)
            old_file_journal_manager = getattr(thread_local, "file_journal_manager", None)
            old_user_message_timestamp = getattr(thread_local, "user_message_timestamp", None)
            thread_local.depth = current_depth + 1
            if sub_session_id is not None:
                thread_local.session_id = sub_session_id
                if old_session_dir is not None and session_id and sub_session_id.startswith(f"{session_id}-"):
                    thread_local.session_dir = os.path.join(old_session_dir, sub_session_id[len(session_id) + 1:])
                else:
                    thread_local.session_dir = None
                thread_local.file_journal_manager = None

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
                # 恢复 depth、tool_scope、session_id、session_dir 和用户消息时间戳
                thread_local.depth = old_depth
                thread_local.session_id = old_session_id
                thread_local.session_dir = old_session_dir
                thread_local.file_journal_manager = old_file_journal_manager
                thread_local.user_message_timestamp = old_user_message_timestamp
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
                        "tool_call_id": tool_call_id,
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


def _no_runtime_delegate(**kwargs) -> str:
    """当 runtime 未提供时，delegate / read_image 工具的占位函数，向后兼容。"""
    return "Error: this tool requires a Runtime instance. Pass runtime= to register_builtin_tools()."


def register_builtin_tools(tool_registry: ToolRegistry, runtime=None) -> list[str]:
    """Register all built-in tools into the given ToolRegistry.

    When runtime is None, the delegate and read_image tools are registered
    but their callables return an error string when called (backward compatibility).

    Args:
        tool_registry: The ToolRegistry to register tools into.
        runtime: Optional Runtime instance for runtime-dependent tools
            (delegate, read_image). If None, those tools are registered
            with a no-op callable.

    Returns:
        List of registered tool_ids.
    """
    ids = []
    for config, fn in BUILTIN_TOOLS:
        if fn is not None:
            tool_registry.register(config, callable_fn=fn)
        # fn is None for runtime-dependent tools — skip here, handled below
        ids.append(config.tool_id)

    # Register delegate tool with runtime-aware callable
    if runtime is not None:
        delegate_fn = _make_delegate_fn(runtime, _thread_local)
        read_image_fn = _make_read_image_fn(runtime)
    else:
        delegate_fn = _no_runtime_delegate
        read_image_fn = _no_runtime_delegate
    tool_registry.register(DELEGATE_TOOL_CONFIG, callable_fn=delegate_fn)
    tool_registry.register(READ_IMAGE_TOOL_CONFIG, callable_fn=read_image_fn)
    ids.append("delegate")

    return ids


# ---------------------------------------------------------------------------
# Helper components for builtin file/code/exec tools
# ---------------------------------------------------------------------------


def _validate_path(workspace: str, raw_path: str) -> str:
    """Resolve *raw_path* and verify it stays inside *workspace*.

    Returns the resolved absolute path on success.

    Raises
    ------
    ValueError
        With an ``error_code`` attribute set to either
        ``"PathTraversalDenied"`` or ``"AbsolutePathDenied"``.
    """
    if os.path.isabs(raw_path):
        resolved = os.path.realpath(raw_path)
        if not (resolved == workspace or resolved.startswith(workspace + os.sep)):
            err = ValueError("Absolute paths outside workspace are not permitted")
            err.error_code = "AbsolutePathDenied"  # type: ignore[attr-defined]
            raise err
        return resolved
    else:
        joined = os.path.realpath(os.path.join(workspace, raw_path))
        if not (joined == workspace or joined.startswith(workspace + os.sep)):
            err = ValueError("Path escapes the workspace boundary")
            err.error_code = "PathTraversalDenied"  # type: ignore[attr-defined]
            raise err
        return joined


def _journal_turn_key(value: Optional[str]) -> tuple[str, str, bool]:
    dt = _parse_journal_timestamp(value)
    if dt is None:
        dt = datetime.datetime.utcnow()
        timestamp = dt.replace(microsecond=0).isoformat()
        return dt.strftime("%y%m%d_%H%M%S"), timestamp, True
    return dt.strftime("%y%m%d_%H%M%S"), value or dt.isoformat(), False


def _flatten_journal_path(rel_path: str, role: str) -> str:
    safe_rel = _safe_rel_path(rel_path)
    flat = re.sub(r"[\\/]+", "-", safe_rel)
    flat = re.sub(r"[^A-Za-z0-9._-]", "_", flat) or "file"
    short_hash = hashlib.sha256(safe_rel.encode("utf-8")).hexdigest()[:8]
    return f"{flat}.{short_hash}.{role}.gz"


def _file_mode(path: str) -> str:
    mode = os.lstat(path).st_mode
    return "100755" if mode & stat.S_IXUSR else "100644"


def _capture_file_state(path: str) -> dict:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"exists": False}
    is_symlink = stat.S_ISLNK(st.st_mode)
    if is_symlink:
        data = os.readlink(path).encode("utf-8", errors="surrogateescape")
    else:
        with open(path, "rb") as fh:
            data = fh.read()
    return {
        "exists": True,
        "data": data,
        "mode": "100755" if st.st_mode & stat.S_IXUSR else "100644",
        "is_symlink": is_symlink,
    }


def _restore_file_state(path: str, state: dict) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    if not state.get("exists"):
        return
    data = state.get("data", b"")
    if state.get("is_symlink"):
        target = data.decode("utf-8", errors="surrogateescape")
        os.symlink(target, path)
        return
    fd, tmp_path = tempfile.mkstemp(dir=parent or None)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_path, path)
        tmp_path = None
        os.chmod(path, 0o755 if state.get("mode") == "100755" else 0o644)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


def _blob_ref_from_state(state: dict, journal_dir: str, rel_path: str, role: str) -> dict:
    if not state.get("exists"):
        return {"exists": False}
    data = state.get("data", b"")
    blob_name = _flatten_journal_path(rel_path, role)
    blob_rel = os.path.join("files", blob_name)
    blob_path = os.path.join(journal_dir, blob_rel)
    _write_gzip_blob(blob_path, data)
    return {
        "exists": True,
        "store": "sidecar",
        "file": blob_rel.replace(os.sep, "/"),
        "sha256": _sha256_bytes(data),
        "size": len(data),
        "compression": "gzip",
        "mode": state.get("mode", "100644"),
        "is_symlink": bool(state.get("is_symlink")),
    }


def _write_gzip_blob(path: str, raw: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    compressed = gzip.compress(raw, compresslevel=6)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(compressed)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


def _read_gzip_blob(path: str, expected_sha256: Optional[str] = None) -> bytes:
    with open(path, "rb") as fh:
        raw = gzip.decompress(fh.read())
    if expected_sha256 and _sha256_bytes(raw) != expected_sha256:
        raise ValueError("Sidecar blob sha256 mismatch")
    return raw


class _ManifestLock:
    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._fh = open(self.path, "w")
        if sys.platform != "win32":
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh is not None:
            if sys.platform != "win32":
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()


class _FileJournalManager:
    def __init__(
        self,
        workspace: str,
        session_id: Optional[str] = None,
        user_message_timestamp: Optional[str] = None,
        session_dir: Optional[str] = None,
    ) -> None:
        self.workspace = os.path.realpath(workspace)
        self.session_id = session_id
        self.user_message_timestamp = user_message_timestamp
        self.session_dir = session_dir
        self.disabled = os.environ.get("DISABLE_FILE_JOURNAL", "false").lower() == "true"
        self.turn_key, self.timestamp, self.timestamp_fallback_used = _journal_turn_key(user_message_timestamp)
        self.journal_id = f"{session_id or 'stateless'}/{self.turn_key}"
        if session_dir:
            self.journal_dir = os.path.join(session_dir, "file_journals", self.turn_key)
            self.manifest_path = os.path.join(self.journal_dir, "manifest.json")
            self.lock_path = self.manifest_path + ".lock"
        else:
            self.journal_dir = None
            self.manifest_path = None
            self.lock_path = None

    def _skipped(self, reason: str) -> dict:
        return {"skipped": True, "reason": reason, "turn_key": self.turn_key, "journal_id": self.journal_id}

    def response_metadata(self) -> dict:
        if self.disabled:
            return self._skipped("disabled")
        if not self.session_dir:
            return self._skipped("no_session_dir")
        return {"journal_id": self.journal_id, "turn_key": self.turn_key, "session_id": self.session_id}

    def ensure_baseline(self, tool_name: str, file_path: str) -> dict:
        if self.disabled:
            return self._skipped("disabled")
        if not self.session_dir or not self.manifest_path or not self.journal_dir or not self.lock_path:
            return self._skipped("no_session_dir")
        try:
            resolved_path = _validate_path(self.workspace, file_path)
            rel_path = _safe_rel_path(os.path.relpath(resolved_path, self.workspace))
            with _ManifestLock(self.lock_path):
                manifest = self._load_manifest()
                files = manifest.setdefault("files", {})
                entry = files.get(rel_path)
                if entry is None:
                    entry = {"path": rel_path, "tools": []}
                    files[rel_path] = entry
                if tool_name not in entry.setdefault("tools", []):
                    entry["tools"].append(tool_name)
                if "baseline" not in entry:
                    entry["baseline"] = self._baseline_ref(resolved_path, rel_path)
                self._save_manifest(manifest)
            return self.response_metadata()
        except Exception as exc:
            logger.warning("File journal baseline failed for %s: %s", file_path, exc)
            return {"error": "JournalFailed", "message": "Could not save baseline before modifying file"}

    def record_after(self, tool_name: str, file_path: str) -> dict:
        if self.disabled:
            return self._skipped("disabled")
        if not self.session_dir or not self.manifest_path or not self.journal_dir or not self.lock_path:
            return self._skipped("no_session_dir")
        try:
            resolved_path = _validate_path(self.workspace, file_path)
            rel_path = _safe_rel_path(os.path.relpath(resolved_path, self.workspace))
            with _ManifestLock(self.lock_path):
                manifest = self._load_manifest()
                files = manifest.setdefault("files", {})
                entry = files.setdefault(rel_path, {"path": rel_path, "tools": []})
                if tool_name not in entry.setdefault("tools", []):
                    entry["tools"].append(tool_name)
                if "baseline" not in entry:
                    entry["baseline"] = {"exists": False}
                entry["after"] = _blob_ref_from_state(_capture_file_state(resolved_path), self.journal_dir, rel_path, "after")
                self._save_manifest(manifest)
            return self.response_metadata()
        except Exception as exc:
            logger.warning("File journal after snapshot failed for %s: %s", file_path, exc)
            return {"error": "JournalFailed", "message": "Could not save after snapshot"}

    def flush(self) -> None:
        return None

    def _load_manifest(self) -> dict:
        if self.manifest_path and os.path.isfile(self.manifest_path):
            with open(self.manifest_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        now = _utc_now_iso()
        return {
            "version": 1,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "timestamp_fallback_used": self.timestamp_fallback_used,
            "turn_key": self.turn_key,
            "workspace": self.workspace,
            "created_at": now,
            "updated_at": now,
            "files": {},
        }

    def _save_manifest(self, manifest: dict) -> None:
        manifest["updated_at"] = _utc_now_iso()
        _atomic_write_json(self.manifest_path, manifest)  # type: ignore[arg-type]

    def _baseline_ref(self, resolved_path: str, rel_path: str) -> dict:
        state = _capture_file_state(resolved_path)
        if not state.get("exists"):
            return {"exists": False}
        git_ref = self._git_baseline_ref(rel_path, state)
        if git_ref is not None:
            return git_ref
        return _blob_ref_from_state(state, self.journal_dir or "", rel_path, "baseline")

    def _git(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(["git"] + args, cwd=self.workspace, capture_output=True, text=False)

    def _git_text(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(["git"] + args, cwd=self.workspace, capture_output=True, text=True,
                              encoding=SYSTEM_ENCODING, errors='replace')

    def _git_baseline_ref(self, rel_path: str, state: dict) -> Optional[dict]:
        if state.get("is_symlink") or not os.path.isdir(os.path.join(self.workspace, ".git")):
            return None
        status = self._git_text(["status", "--porcelain", "--", rel_path])
        if status.returncode != 0 or (status.stdout or "").strip():
            return None
        oid_result = self._git_text(["rev-parse", f"HEAD:{rel_path}"])
        if oid_result.returncode != 0:
            return None
        commit_result = self._git_text(["rev-parse", "HEAD"])
        cat_result = self._git(["cat-file", "-p", (oid_result.stdout or "").strip()])
        if commit_result.returncode != 0 or cat_result.returncode != 0:
            return None
        ls_result = self._git_text(["ls-tree", "HEAD", "--", rel_path])
        mode = state.get("mode", "100644")
        if ls_result.returncode == 0 and (ls_result.stdout or "").strip():
            mode = (ls_result.stdout or "").split()[0]
        raw = cat_result.stdout or ""
        return {
            "exists": True,
            "store": "git",
            "oid": (oid_result.stdout or "").strip(),
            "git_object_format": "sha1",
            "git_commit": (commit_result.stdout or "").strip(),
            "git_path": rel_path,
            "sha256": _sha256_bytes(raw),
            "size": len(raw),
            "mode": mode,
            "is_symlink": False,
        }


class _PathValidator:
    def __init__(self, workspace: str) -> None:
        self.workspace = os.path.realpath(workspace)

    def validate(self, raw_path: str) -> str:
        return _validate_path(self.workspace, raw_path)



class _Linter:
    """Run a basic syntax check on a file after editing.

    The linter command is selected by file extension.  Unknown extensions
    always pass.  This class never raises an exception.
    """

    # Map of file extension → command template (%s is replaced by the path)
    _COMMANDS: dict[str, list[str]] = {
        ".py": ["python", "-m", "py_compile"],
        ".js": ["node", "--check"],
        ".jsx": ["node", "--check"],
        ".ts": ["npx", "--yes", "tsc", "--noEmit", "--allowJs", "--checkJs"],
        ".tsx": ["npx", "--yes", "tsc", "--noEmit", "--allowJs", "--checkJs"],
        ".json": ["python", "-c", "import sys,json; json.load(open(sys.argv[1]),'r')"],
        ".yaml": ["python", "-c",
                  "import sys; import importlib; yaml=importlib.import_module('yaml'); "
                  "yaml.safe_load(open(sys.argv[1]))"],
        ".yml": ["python", "-c",
                 "import sys; import importlib; yaml=importlib.import_module('yaml'); "
                 "yaml.safe_load(open(sys.argv[1]))"],
        ".sh": ["bash", "-n"],
    }

    def check(self, path: str) -> tuple[bool, str]:
        """Check *path* for syntax errors.

        Returns ``(True, "")`` for unknown extensions or when the check
        passes.  Returns ``(False, <output>)`` when the check fails.
        Never raises.
        """
        try:
            ext = os.path.splitext(path)[1].lower()
            cmd_template = self._COMMANDS.get(ext)
            if cmd_template is None:
                return (True, "")

            cmd = cmd_template + [path]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding=SYSTEM_ENCODING, errors='replace',
                timeout=30,
            )
            if result.returncode == 0:
                return (True, (result.stdout or "") + (result.stderr or ""))
            return (False, ((result.stdout or "") + (result.stderr or "")).strip())
        except Exception as exc:
            # Never raise — treat unexpected errors as a pass so that the
            # linter doesn't block edits when the tool is unavailable.
            logger.warning("_Linter.check(%r) raised unexpectedly: %s", path, exc)
            return (True, "")


# ---------------------------------------------------------------------------
# Task 4.1 — _read_file implementation
# ---------------------------------------------------------------------------

def _read_file(path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> str:
    """Read a file and return its contents as line-numbered JSON output."""

    def error(code: str, message: str) -> str:
        return json.dumps({"error": code, "message": message})

    workspace = get_workspace()
    check_path_for_read = os.environ.get("CHECK_PATH_FOR_READ", "false").lower() == "true"

    if check_path_for_read:
        try:
            resolved_path = _validate_path(workspace, path)
        except ValueError as exc:
            return error(exc.error_code, str(exc))  # type: ignore[attr-defined]
    else:
        if os.path.isabs(path):
            resolved_path = os.path.realpath(path)
        else:
            resolved_path = os.path.realpath(os.path.join(workspace, path))

    if not os.path.isfile(resolved_path):
        return error("FileNotFound", f"The specified file `{resolved_path}` does not exist")

    with open(resolved_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    total_lines = len(lines)

    if start_line is not None and end_line is not None and start_line > end_line:
        return error("InvalidRange", "start_line must be less than or equal to end_line")
    if start_line is not None and not 1 <= start_line <= total_lines:
        return error("LineOutOfRange", "Line number is out of file bounds")
    if end_line is not None and not 1 <= end_line <= total_lines:
        end_line = total_lines

    if start_line is None and end_line is None:
        threshold = int(os.environ.get("READ_TRUNCATION_LINES", 1000))
        start, end = 1, min(total_lines, threshold)
        truncated = total_lines > threshold
        # If we're truncating by READ_TRUNCATION_LINES, track omitted lines
        if truncated:
            omitted_lines = total_lines - threshold
        else:
            omitted_lines = 0
    else:
        start, end = start_line or 1, end_line or total_lines
        truncated = False
        omitted_lines = 0

    selected_lines = lines[start - 1:end]
    
    # Apply output limits (EXEC_OUTPUT_LINE_LIMIT and EXEC_OUTPUT_COLUMN_LIMIT)
    # similar to exec_shell and search_code
    output_line_limit = int(os.environ.get("EXEC_OUTPUT_LINE_LIMIT", 1000))
    max_line_length = int(os.environ.get("EXEC_OUTPUT_COLUMN_LIMIT", 1000))
    
    # Track if any truncation occurs
    line_truncated = False
    column_truncated = False
    
    # Store the initial omitted_lines from READ_TRUNCATION_LINES
    read_truncation_omitted = omitted_lines
    
    # Truncate lines if needed (EXEC_OUTPUT_LINE_LIMIT)
    if len(selected_lines) > output_line_limit:
        truncated = True
        omitted_lines = total_lines - output_line_limit
        selected_lines = selected_lines[:output_line_limit]
        line_truncated = True
    
    # Build content with line numbers and apply column limit
    content_parts = []
    for line_number, line in enumerate(selected_lines, start=start):
        # Remove trailing newline for consistent formatting
        line_content = line.rstrip('\n').rstrip('\r\n')
        
        # Apply column limit to each line
        if len(line_content) > max_line_length:
            line_content = line_content[:max_line_length - 3] + "..."
            column_truncated = True
        
        content_parts.append(f"{line_number}: {line_content}")
    
    # If column was truncated, mark as truncated
    if column_truncated:
        truncated = True
    
    content = "\n".join(content_parts)
    
    # Add truncation notice if output was truncated by line count
    if line_truncated:
        content += f"\n[...output truncated: {omitted_lines} lines omitted...]"
    
    # Calculate final omitted_lines for the result
    if line_truncated:
        # Use the EXEC_OUTPUT_LINE_LIMIT based omission
        final_omitted_lines = omitted_lines
    elif read_truncation_omitted > 0:
        # Use the READ_TRUNCATION_LINES based omission
        final_omitted_lines = read_truncation_omitted
    else:
        # If only column was truncated or no truncation, no lines were omitted
        final_omitted_lines = 0
    
    result: dict = {
        "content": content,
        "total_lines": total_lines,
        "truncated": truncated,
    }
    if truncated:
        result["omitted_lines"] = final_omitted_lines

    return json.dumps(result)


# Task 4.2 — Register read_file tool
READ_FILE_TOOL_CONFIG = ToolConfig(
    tool_id="read_file",
    tool_type="function",
    name="read_file",
    description=(
        "Read a file from the workspace and return its contents with line numbers. "
        "Optionally specify start_line and/or end_line to read a range."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file (relative to workspace)",
            },
            "start_line": {
                "type": "integer",
                "description": "First line to return (1-indexed, inclusive)",
            },
            "end_line": {
                "type": "integer",
                "description": "Last line to return (1-indexed, inclusive)",
            },
        },
        "required": ["path"],
    },
    builtin=True,
)

BUILTIN_TOOLS.append((READ_FILE_TOOL_CONFIG, _read_file))


# ---------------------------------------------------------------------------
# Task 5.1 — _write_file implementation
# ---------------------------------------------------------------------------

def _write_file(path: str, content: str) -> str:
    """Write content to a file atomically with a pre-write file journal snapshot.

    Args:
        path: Path to the file (relative to workspace or absolute within it).
        content: The content to write to the file (UTF-8 string).

    Returns:
        JSON string with keys: file, bytes_written, journal on success.
        On error, returns JSON string with keys: error, message.
    """
    import tempfile

    workspace = get_workspace()
    encoded = content.encode("utf-8")
    bytes_written = len(encoded)

    # Always write to a temp file first so content is never lost even when
    # path validation fails (content may be large / expensive in tokens).
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix="write_file_", suffix=".tmp", dir="/tmp"
        )
        try:
            with os.fdopen(fd, "wb") as tmp_file:
                tmp_file.write(encoded)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
    except Exception as exc:
        return json.dumps({"error": "WriteFailure", "message": str(exc)})

    try:
        resolved_path = _validate_path(workspace, path)
    except ValueError as exc:
        # Validation failed – temp file in /tmp is kept; point caller at it.
        return json.dumps({
            "error": exc.error_code,  # type: ignore[attr-defined]
            "message": (
                f"{exc}. Content was NOT written to the requested path "
                f"but has been saved to temporary file: {tmp_path}"
            ),
        })

    journal_manager = _get_file_journal_manager(workspace)
    backup = _capture_file_state(resolved_path)
    journal_result = journal_manager.ensure_baseline("write_file", resolved_path)
    if isinstance(journal_result, dict) and journal_result.get("error"):
        return json.dumps(journal_result)

    # Create parent directories if they don't exist
    parent_dir = os.path.dirname(resolved_path)
    try:
        os.makedirs(parent_dir, exist_ok=True)
    except OSError as exc:
        return json.dumps({"error": "WriteFailure", "message": str(exc)})

    # Move temp file to target.  shutil.move uses os.rename (atomic) when
    # /tmp and the target are on the same filesystem, falling back to
    # copy+delete otherwise.
    try:
        shutil.move(tmp_path, resolved_path)
        tmp_path = None  # moved successfully, no cleanup needed
    except OSError as exc:
        return json.dumps({"error": "WriteFailure", "message": str(exc)})
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    journal_result = journal_manager.record_after("write_file", resolved_path)
    if isinstance(journal_result, dict) and journal_result.get("error"):
        try:
            _restore_file_state(resolved_path, backup)
        except Exception:
            return json.dumps({
                "error": "JournalFailed",
                "message": "Could not save after snapshot and failed to restore file",
            })
        return json.dumps({
            "error": "JournalFailed",
            "message": "Could not save after snapshot; file was restored to pre-call state",
        })

    # Compute relative path from workspace root for the response
    rel_path = os.path.relpath(resolved_path, workspace)

    journal_meta = journal_manager.response_metadata()
    return json.dumps({
        "file": rel_path,
        "bytes_written": bytes_written,
        "journal_id": journal_meta.get("journal_id"),
        "journal": journal_meta,
    })


# Task 5.2 — Register write_file tool
WRITE_FILE_TOOL_CONFIG = ToolConfig(
    tool_id="write_file",
    tool_type="function",
    name="write_file",
    description=(
        "Write content to a file in the workspace atomically. "
        "Creates parent directories if they don't exist. "
        "A file journal snapshot is saved before writing so the change can be reviewed or reverted with the session."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file (relative to workspace)",
            },
            "content": {
                "type": "string",
                "description": "Content to write to the file (UTF-8 string)",
            },
        },
        "required": ["path", "content"],
    },
    builtin=True,
)

BUILTIN_TOOLS.append((WRITE_FILE_TOOL_CONFIG, _write_file))


# ---------------------------------------------------------------------------
# Task 7.1 & 7.2 — _edit_file implementation (search_replace + diff modes)
# ---------------------------------------------------------------------------

def _edit_file(
    path: str,
    mode: str,
    old_str: Optional[str] = None,
    new_str: Optional[str] = None,
    patch: Optional[str] = None,
) -> str:
    """Edit a file using search_replace or diff mode.

    Args:
        path: Path to the file (relative to workspace or absolute within it).
        mode: Either 'search_replace' or 'diff'.
        old_str: (search_replace mode) The text block to find and replace.
        new_str: (search_replace mode) The replacement text block.
        patch: (diff mode) A unified diff patch string to apply.

    Returns:
        JSON string with keys: file, lines_added, lines_removed, file_modified on success.
        On error, returns JSON string with keys: error, message.
    """
    workspace = get_workspace()

    try:
        resolved_path = _validate_path(workspace, path)
    except ValueError as exc:
        return json.dumps({"error": exc.error_code, "message": str(exc)})  # type: ignore[attr-defined]

    # Check file exists
    if not os.path.isfile(resolved_path):
        return json.dumps({"error": "FileNotFound", "message": f"The specified file `{resolved_path}` does not exist"})

    journal_manager = _get_file_journal_manager(workspace)
    backup = _capture_file_state(resolved_path)
    journal_result = journal_manager.ensure_baseline("edit_file", resolved_path)
    if isinstance(journal_result, dict) and journal_result.get("error"):
        return json.dumps(journal_result)

    rel_path = os.path.relpath(resolved_path, workspace)

    result = None
    if mode == "search_replace":
        result = _edit_file_search_replace(
            resolved_path, rel_path, workspace, old_str, new_str, backup
        )
    elif mode == "diff":
        result = _edit_file_diff(
            resolved_path, rel_path, workspace, patch, backup
        )
    else:
        return json.dumps({"error": "InvalidMode", "message": f"Unknown mode: {mode!r}. Use 'search_replace' or 'diff'."})

    if result and isinstance(result, str):
        try:
            result_data = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result
        if "error" in result_data:
            return result
        journal_result = journal_manager.record_after("edit_file", resolved_path)
        if isinstance(journal_result, dict) and journal_result.get("error"):
            try:
                _restore_file_state(resolved_path, backup)
            except Exception:
                return json.dumps({
                    "error": "JournalFailed",
                    "message": "Could not save after snapshot and failed to restore file",
                })
            return json.dumps({
                "error": "JournalFailed",
                "message": "Could not save after snapshot; file was restored to pre-call state",
            })
        journal_meta = journal_manager.response_metadata()
        result_data["journal_id"] = journal_meta.get("journal_id")
        result_data["journal"] = journal_meta
        return json.dumps(result_data)

    return result


def _strip_lines(text: str) -> list[str]:
    """Return a list of lines with leading/trailing whitespace stripped."""
    return [line.strip() for line in text.splitlines()]


def _find_first_occurrence(file_lines: list[str], old_str: str) -> Optional[int]:
    """Find the start index (0-based) of the first occurrence of old_str in file_lines.

    Uses whitespace-tolerant matching: leading/trailing whitespace on each line
    is ignored when comparing.

    Returns the 0-based line index of the first matching line, or None if not found.
    """
    old_stripped = _strip_lines(old_str)
    if not old_stripped:
        return None

    n_old = len(old_stripped)
    n_file = len(file_lines)

    file_stripped = [line.strip() for line in file_lines]

    for i in range(n_file - n_old + 1):
        if file_stripped[i:i + n_old] == old_stripped:
            return i
    return None


def _edit_file_search_replace(
    resolved_path: str,
    rel_path: str,
    workspace: str,
    old_str: Optional[str],
    new_str: Optional[str],
    backup: dict,
) -> str:
    """Perform search_replace edit on the file."""
    if old_str is None:
        return json.dumps({"error": "LineNotFound", "message": "old_str parameter is required for search_replace mode"})
    if new_str is None:
        new_str = ""

    # Read file content
    with open(resolved_path, "r", encoding="utf-8", errors="replace") as f:
        file_lines = f.readlines()

    # Find first occurrence using whitespace-tolerant matching
    match_start = _find_first_occurrence(file_lines, old_str)
    if match_start is None:
        return json.dumps({"error": "LineNotFound", "message": "Could not find text block in the specified file"})

    old_line_count = len(old_str.splitlines()) if old_str else 0
    # Ensure we handle old_str that doesn't end with newline
    # The matched block spans file_lines[match_start : match_start + old_line_count]

    # Build new content: lines before + new_str lines + lines after
    new_str_lines = new_str.splitlines(keepends=True)
    # If new_str doesn't end with newline but the replaced block did, preserve trailing newline
    if new_str_lines and not new_str_lines[-1].endswith("\n"):
        # Check if the last replaced line had a newline
        last_replaced_idx = match_start + old_line_count - 1
        if last_replaced_idx < len(file_lines) and file_lines[last_replaced_idx].endswith("\n"):
            new_str_lines[-1] += "\n"
    elif not new_str_lines and old_line_count > 0:
        # Replacing with empty string — remove the lines entirely
        pass

    new_file_lines = (
        file_lines[:match_start]
        + new_str_lines
        + file_lines[match_start + old_line_count:]
    )

    new_content = "".join(new_file_lines)

    # Write back
    try:
        import tempfile
        parent_dir = os.path.dirname(resolved_path)
        fd, tmp_path = tempfile.mkstemp(dir=parent_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            os.unlink(tmp_path)
            raise
        os.replace(tmp_path, resolved_path)
    except OSError as exc:
        return json.dumps({"error": "WriteFailure", "message": str(exc)})

    # Run linter
    linter = _Linter()
    passed, lint_output = linter.check(resolved_path)
    if not passed:
        _restore_file_state(resolved_path, backup)
        return json.dumps({"error": "LintFailed", "message": lint_output})

    # Calculate lines added/removed
    new_line_count = len(new_str.splitlines()) if new_str else 0
    lines_removed = old_line_count
    lines_added = new_line_count

    # Detect if file content actually changed
    old_content = backup.get("data", b"")
    try:
        with open(resolved_path, "rb") as f:
            new_content = f.read()
    except FileNotFoundError:
        # File was deleted during edit (unlikely but handle gracefully)
        new_content = b""
    file_modified = (old_content != new_content)

    return json.dumps({
        "file": rel_path,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "file_modified": file_modified,
    })


def _strip_diff_fence(patch: str) -> str:
    lines = patch.splitlines(keepends=True)
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is None or not lines[first].lstrip().startswith("```"):
        return patch

    last = next((i for i in range(len(lines) - 1, first, -1) if lines[i].strip()), None)
    if last is not None and lines[last].strip() == "```":
        return "".join(lines[first + 1:last])
    return patch


def _find_line_block(file_lines: list[str], block: list[str], start: int = 0) -> Optional[int]:
    """Find a block of logical lines in file_lines using whitespace-tolerant matching."""
    if not block:
        return start
    stripped_file = [line.rstrip("\n").strip() for line in file_lines]
    stripped_block = [line.strip() for line in block]
    n = len(stripped_block)
    for i in range(max(start, 0), len(stripped_file) - n + 1):
        if stripped_file[i:i + n] == stripped_block:
            return i
    return None


def _format_hunk_header(old_start: int, old_count: int, new_start: int, new_count: int) -> str:
    old_range = str(old_start) if old_count == 1 else f"{old_start},{old_count}"
    new_range = str(new_start) if new_count == 1 else f"{new_start},{new_count}"
    return f"@@ -{old_range} +{new_range} @@"


def _is_added_line(line: str) -> bool:
    return line.startswith("+")


def _is_removed_line(line: str) -> bool:
    return line.startswith("-")


def _count_hunk_lines(hunk_lines: list[str]) -> tuple[int, int]:
    old_count = 0
    new_count = 0
    for line in hunk_lines:
        if _is_added_line(line):
            new_count += 1
        elif _is_removed_line(line):
            old_count += 1
        elif line.startswith("\\"):
            continue
        else:
            old_count += 1
            new_count += 1
    return old_count, new_count


def _rewrite_unified_hunk_counts(patch: str) -> str:
    """Rewrite hunk line counts to match the hunk body.

    LLMs often produce otherwise-valid unified diffs with stale @@ -a,b +c,d @@
    counts.  The external patch command treats those as malformed, so normalize
    the counts before invoking it.
    """
    lines = patch.splitlines()
    out: list[str] = []
    i = 0
    hunk_re = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$")
    while i < len(lines):
        line = lines[i]
        match = hunk_re.match(line)
        if not match:
            out.append(line)
            i += 1
            continue

        body: list[str] = []
        i += 1
        while i < len(lines):
            next_line = lines[i]
            if next_line.startswith("@@ ") or next_line.startswith("--- "):
                break
            if not next_line.startswith((" ", "+", "-", "\\")):
                # Some generated diffs omit the required leading space on
                # context lines.  Add it so the hunk is syntactically valid.
                next_line = f" {next_line}"
            body.append(next_line)
            i += 1

        old_count, new_count = _count_hunk_lines(body)
        old_start = int(match.group(1))
        new_start = int(match.group(2))
        suffix = match.group(3) or ""
        out.append(_format_hunk_header(old_start, old_count, new_start, new_count) + suffix)
        out.extend(body)

    return "\n".join(out) + ("\n" if patch.endswith("\n") or out else "")


def _build_located_hunk(file_lines: list[str], patch_lines: list[str], search_start: int = 0) -> tuple[Optional[list[str]], int]:
    old_block = [line[1:] if _is_removed_line(line) else line[1:] if line.startswith(" ") else line for line in patch_lines if not _is_added_line(line)]
    if not old_block:
        # Pure insertion with no context is ambiguous; let patch report a useful
        # diagnostic instead of inventing a location.
        return None, search_start

    match_start = _find_line_block(file_lines, old_block, search_start)
    if match_start is None:
        return None, search_start

    old_count, new_count = _count_hunk_lines(patch_lines)
    hunk = [_format_hunk_header(match_start + 1, old_count, match_start + 1, new_count)]
    for line in patch_lines:
        if _is_added_line(line) or _is_removed_line(line) or line.startswith("\\"):
            hunk.append(line)
        else:
            hunk.append(f" {line[1:] if line.startswith(' ') else line}")
    return hunk, match_start + max(old_count, 1)


def _convert_begin_patch_format(patch: str, rel_path: str, resolved_path: Optional[str] = None) -> str:
    """Convert common *** Begin Patch update hunks to unified diff.

    The Begin Patch DSL frequently uses bare @@ markers as anchors, e.g. one
    anchor for the containing function and another anchor for the insertion
    point.  Treat anchor-only sections as location hints and emit only hunks that
    actually contain +/- changes, with line numbers located from the target file.
    """
    file_lines: list[str] = []
    if resolved_path:
        try:
            with open(resolved_path, "r", encoding="utf-8", errors="replace") as f:
                file_lines = f.readlines()
        except OSError:
            file_lines = []

    lines = patch.splitlines()
    result: list[str] = []
    current_file = rel_path
    section: list[str] = []
    search_start = 0
    in_file = False

    def flush_section() -> None:
        nonlocal section, search_start
        if not section:
            return
        has_change = any(_is_added_line(line) or _is_removed_line(line) for line in section)
        if has_change:
            hunk, next_start = _build_located_hunk(file_lines, section, search_start)
            if hunk is None:
                # Fall back to a syntactically valid hunk; patch will diagnose
                # any context mismatch.
                old_count, new_count = _count_hunk_lines(section)
                hunk = [_format_hunk_header(1, old_count, 1, new_count), *section]
            else:
                search_start = next_start
            result.extend(hunk)
        else:
            found = _find_line_block(file_lines, section, search_start)
            if found is not None:
                search_start = found + len(section)
        section = []

    for line in lines:
        if line.startswith("*** Begin Patch") or line.startswith("*** End Patch"):
            continue
        if line.startswith("*** Update File:"):
            flush_section()
            current_file = line.split(":", 1)[1].strip() or rel_path
            result = [f"--- {current_file}", f"+++ {current_file}"]
            in_file = True
            continue
        if line.startswith("*** Add File:") or line.startswith("*** Delete File:"):
            # Keep unsupported operations syntactically simple. edit_file already
            # targets an existing single file, so update hunks are the useful case.
            flush_section()
            current_file = line.split(":", 1)[1].strip() or rel_path
            result = [f"--- {current_file}", f"+++ {current_file}"]
            in_file = True
            continue
        if line.startswith("@@"):
            # Bare @@ markers are anchors in Begin Patch DSL.  Numbered @@
            # headers are also treated as section boundaries; we recalculate the
            # final location/counts from the target file below.
            flush_section()
            continue
        if in_file:
            section.append(line)

    flush_section()
    if not result:
        return ""
    return _rewrite_unified_hunk_counts("\n".join(result) + "\n")


def _normalize_patch_for_path(patch: str, rel_path: str, resolved_path: Optional[str] = None) -> str:
    patch = _strip_diff_fence(patch)

    # Handle "*** Begin Patch" format (used by some LLMs)
    if "*** Begin Patch" in patch:
        patch = _convert_begin_patch_format(patch, rel_path, resolved_path)

    lines = patch.splitlines(keepends=True)
    first_text = next((line.lstrip() for line in lines if line.strip()), "")
    if first_text.startswith("@@ "):
        patch = f"--- {rel_path}\n+++ {rel_path}\n" + patch
        return _rewrite_unified_hunk_counts(patch)

    normalized = []
    before_first_hunk = True
    for line in lines:
        if before_first_hunk and line.startswith("--- "):
            suffix = "\n" if line.endswith("\n") else ""
            normalized.append(f"--- {rel_path}{suffix}")
            continue
        if before_first_hunk and line.startswith("+++ "):
            suffix = "\n" if line.endswith("\n") else ""
            normalized.append(f"+++ {rel_path}{suffix}")
            continue
        if line.startswith("@@ "):
            before_first_hunk = False
        normalized.append(line)
    return _rewrite_unified_hunk_counts("".join(normalized))


def _patch_process_output(result: subprocess.CompletedProcess) -> str:
    output = "\n".join(
        part.strip() for part in (result.stdout or "", result.stderr or "") if part and part.strip()
    )
    
    if not output:
        return "Patch did not apply cleanly"
    
    # Add helpful context for common errors
    if "Only garbage was found" in output:
        return f"{output}\n\nHint: The patch format appears to be invalid. Make sure to use standard unified diff format with proper --- and +++ file headers."
    
    if "unexpectedly ends" in output:
        return f"{output}\n\nHint: The patch appears to be incomplete. Make sure all hunks are properly terminated."
    
    if "patch: ****" in output:
        return f"{output}\n\nHint: The patch format is invalid. Consider using search_replace mode instead."
    
    return output


def _cleanup_patch_artifacts(resolved_path: str) -> None:
    for suffix in (".orig", ".rej"):
        try:
            os.unlink(resolved_path + suffix)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Failed to remove patch artifact: %s", resolved_path + suffix)


def _restore_patched_file(resolved_path: str, backup: dict) -> None:
    _restore_file_state(resolved_path, backup)
    _cleanup_patch_artifacts(resolved_path)


def _run_patch(workspace: str, patch: str, dry_run: bool) -> subprocess.CompletedProcess:
    args = ["patch", "--batch", "--forward", "-p0"]
    if dry_run:
        args.append("--dry-run")
    return subprocess.run(
        args,
        input=patch,
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding=SYSTEM_ENCODING, errors='replace',
    )


def _edit_file_diff(
    resolved_path: str,
    rel_path: str,
    workspace: str,
    patch: Optional[str],
    backup: dict,
) -> str:
    """Apply a unified diff patch to the file."""
    if patch is None:
        return json.dumps({"error": "PatchFailed", "message": "patch parameter is required for diff mode"})

    normalized_patch = _normalize_patch_for_path(patch, rel_path, resolved_path)

    try:
        dry_run = _run_patch(workspace, normalized_patch, dry_run=True)
    except FileNotFoundError:
        return json.dumps({"error": "PatchFailed", "message": "patch command not found"})

    if dry_run.returncode != 0:
        _cleanup_patch_artifacts(resolved_path)
        return json.dumps({"error": "PatchFailed", "message": _patch_process_output(dry_run)})

    result = _run_patch(workspace, normalized_patch, dry_run=False)
    if result.returncode != 0:
        _restore_patched_file(resolved_path, backup)
        return json.dumps({"error": "PatchFailed", "message": _patch_process_output(result)})

    _cleanup_patch_artifacts(resolved_path)

    linter = _Linter()
    passed, lint_output = linter.check(resolved_path)
    if not passed:
        _restore_patched_file(resolved_path, backup)
        return json.dumps({"error": "LintFailed", "message": lint_output})

    lines_added = 0
    lines_removed = 0
    for line in normalized_patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines_added += 1
        elif line.startswith("-") and not line.startswith("---"):
            lines_removed += 1

    # Detect if file content actually changed
    old_content = backup.get("data", b"")
    try:
        with open(resolved_path, "rb") as f:
            new_content = f.read()
    except FileNotFoundError:
        # File was deleted during edit (unlikely but handle gracefully)
        new_content = b""
    file_modified = (old_content != new_content)

    return json.dumps({
        "file": rel_path,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "file_modified": file_modified,
    })


# Task 7.3 — Register edit_file tool
EDIT_FILE_TOOL_CONFIG = ToolConfig(
    tool_id="edit_file",
    tool_type="function",
    name="edit_file",
    description=(
        "Edit a file in the workspace using search_replace or diff mode. "
        "In search_replace mode, finds the first occurrence of old_str and replaces it with new_str. "
        "In diff mode, applies a unified diff patch. "
        "A file journal snapshot is saved before editing. "
        "Syntax is checked after editing; if it fails the edit is reverted to its pre-call state."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file (relative to workspace)",
            },
            "mode": {
                "type": "string",
                "enum": ["search_replace", "diff"],
                "description": "Edit mode: 'search_replace' (recommended) or 'diff'",
            },
            "old_str": {
                "type": "string",
                "description": "(search_replace mode) The text block to find and replace",
            },
            "new_str": {
                "type": "string",
                "description": "(search_replace mode) The replacement text block",
            },
            "patch": {
                "type": "string",
                "description": "(diff mode) A unified diff patch string to apply",
            },
        },
        "required": ["path", "mode"],
    },
    builtin=True,
)

BUILTIN_TOOLS.append((EDIT_FILE_TOOL_CONFIG, _edit_file))


# ---------------------------------------------------------------------------
# Task 8.1 — _search_code implementation
# ---------------------------------------------------------------------------

def _split_patterns(pattern: str | None) -> list[str]:
    """Split a pattern string by | into a list of non-empty normalized patterns."""
    if not pattern:
        return []
    return [p.strip().replace("\\", "/") for p in pattern.split("|") if p.strip()]


def _search_glob_matches(rel_path: str, pattern: str) -> bool:
    """Best-effort path glob matching shared by rg and grep result filtering."""
    normalized_path = rel_path.replace("\\", "/")
    if normalized_path.startswith("./"):
        normalized_path = normalized_path[2:]
    normalized_pattern = pattern.strip().replace("\\", "/")
    if normalized_pattern in {"*", "**", "**/*"}:
        return True

    basename = os.path.basename(normalized_path)
    if fnmatch.fnmatch(normalized_path, normalized_pattern):
        return True
    if fnmatch.fnmatch(basename, normalized_pattern):
        return True
    if normalized_pattern.startswith("**/"):
        suffix = normalized_pattern[3:]
        return fnmatch.fnmatch(normalized_path, suffix) or fnmatch.fnmatch(basename, suffix)
    return False


def _search_path_allowed(rel_path: str, include_patterns: list[str], exclude_patterns: list[str]) -> bool:
    """Apply user include/exclude globs after command execution.

    This is required for grep because GNU grep applies ``--include`` and
    ``--exclude`` to basenames only, while rg-style globs are path-aware.
    """
    normalized_path = rel_path.replace("\\", "/")
    if normalized_path.startswith("./"):
        normalized_path = normalized_path[2:]
    if include_patterns and not any(_search_glob_matches(normalized_path, pat) for pat in include_patterns):
        return False
    if exclude_patterns and any(_search_glob_matches(normalized_path, pat) for pat in exclude_patterns):
        return False
    return True


def _grep_include_args(patterns: list[str]) -> list[str]:
    """Convert path-aware include globs into safe basename-only grep includes."""
    args: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        basename_pattern = pattern.rsplit("/", 1)[-1]
        if basename_pattern in {"", "*", "**"} or basename_pattern in seen:
            continue
        seen.add(basename_pattern)
        args.append(f"--include={basename_pattern}")
    return args


def _grep_exclude_args(patterns: list[str]) -> list[str]:
    """Return grep excludes only for basename globs to avoid over-exclusion."""
    args: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        if "/" in pattern:
            continue
        if pattern in {"", "*", "**"} or pattern in seen:
            continue
        seen.add(pattern)
        args.append(f"--exclude={pattern}")
    return args


def _search_code(query: str, include: Optional[str] = None, exclude: Optional[str] = None) -> str:
    """Search the workspace codebase for a regex pattern using ripgrep or grep.

    Args:
        query: A regular expression pattern to search for.
        include: Optional glob pattern to restrict search to matching files.
                 Multiple patterns can be separated by | (e.g. '*.svelte|*.js').
        exclude: Optional glob pattern to exclude matching files from search.
                 Multiple patterns can be separated by | (e.g. '*.log|*.bak').

    Returns:
        JSON string with keys: results, truncated, total_found on success.
        On error, returns JSON string with keys: error, message.
    """
    # Validate the query as a valid regex
    try:
        re.compile(query)
    except re.error as exc:
        return json.dumps({"error": "InvalidQuery", "message": str(exc)})

    # Get workspace
    workspace = get_workspace()

    max_results = int(os.environ.get("SEARCH_MAX_RESULTS", 100))
    max_context_length = int(os.environ.get("EXEC_OUTPUT_COLUMN_LIMIT", 1000))

    # Default excludes
    default_excludes = [".git", "node_modules", "dist"]
    include_patterns = _split_patterns(include)
    exclude_patterns = _split_patterns(exclude)

    # Try ripgrep first
    if shutil.which("rg") is not None:
        cmd = ["rg", "--json"]
        # Add default excludes
        for excl in default_excludes:
            cmd += ["--glob", f"!{excl}"]
        # Add user-specified include patterns (support | as OR)
        for pat in include_patterns:
            cmd += ["--glob", pat]
        # Add user-specified exclude patterns (support | as OR)
        for pat in exclude_patterns:
            cmd += ["--glob", f"!{pat}"]
        # Explicit path is required: when stdin is not a TTY, rg may otherwise
        # read stdin instead of recursively searching the workspace.
        cmd += ["-e", query, "."]

        try:
            result = subprocess.run(
                cmd,
                cwd=workspace,
                capture_output=True,
                text=True,
                encoding=SYSTEM_ENCODING, errors='replace',
            )
        except Exception as exc:
            return json.dumps({"error": "SearchToolNotFound", "message": str(exc)})

        results = []
        total_found = 0

        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if obj.get("type") != "match":
                continue

            data = obj.get("data", {})
            file_path = data.get("path", {}).get("text", "")
            # Make path relative to workspace
            if os.path.isabs(file_path):
                file_path = os.path.relpath(file_path, workspace)
            if not _search_path_allowed(file_path, include_patterns, exclude_patterns):
                continue

            total_found += 1
            if len(results) < max_results:
                line_number = data.get("line_number", 0)
                submatches = data.get("submatches", [])
                column = submatches[0].get("start", 0) if submatches else 0
                context = data.get("lines", {}).get("text", "").rstrip("\n").rstrip("\r\n")
                # Smart truncation: ensure search keywords are preserved
                if len(context) > max_context_length and submatches:
                    # Use submatches to determine where to truncate
                    match_start = submatches[0].get("start", 0)
                    match_end = submatches[0].get("end", 0)
                    
                    # Calculate truncation range around the match
                    context_half = max_context_length // 2
                    start = max(0, match_start - context_half)
                    end = min(len(context), match_end + context_half)
                    
                    # Ensure we don't exceed max_context_length
                    if end - start > max_context_length:
                        # Adjust to fit within limit while keeping match
                        start = max(0, end - max_context_length)
                    
                    context = context[start:end]
                    
                    # Add truncation markers
                    if start > 0:
                        context = "..." + context
                    if end < len(data.get("lines", {}).get("text", "").rstrip("\n").rstrip("\r\n")):
                        context = context + "..."
                elif len(context) > max_context_length:
                    # Fallback: simple truncation if no submatches
                    context = context[:max_context_length] + "..."
                results.append({
                    "file": file_path,
                    "line": line_number,
                    "column": column,
                    "context": context,
                })

        truncated = total_found > max_results
        return json.dumps({
            "results": results,
            "truncated": truncated,
            "total_found": total_found,
        })

    # Fall back to grep
    if shutil.which("grep") is not None:
        cmd = ["grep", "-r", "-n"]
        for excl in default_excludes:
            cmd += [f"--exclude-dir={excl}"]
        # GNU grep --include/--exclude only match basenames.  Use safe command
        # narrowing and apply authoritative path-glob filtering in Python below.
        cmd += _grep_include_args(include_patterns)
        cmd += _grep_exclude_args(exclude_patterns)
        # Add the query and search path. Use -e to prevent option injection when
        # a user regex starts with '-' and -I to ignore binary files.
        cmd += ["-I", "-E", "-e", query, "."]

        try:
            result = subprocess.run(
                cmd,
                cwd=workspace,
                capture_output=True,
                text=True,
                encoding=SYSTEM_ENCODING, errors='replace',
            )
        except Exception as exc:
            return json.dumps({"error": "SearchToolNotFound", "message": str(exc)})

        results = []
        total_found = 0

        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            # grep output format: filename:line_number:content
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            file_path, line_num_str, context = parts[0], parts[1], parts[2]
            # Make path relative (grep may prefix with ./)
            if file_path.startswith("./"):
                file_path = file_path[2:]
            if not _search_path_allowed(file_path, include_patterns, exclude_patterns):
                continue
            try:
                line_number = int(line_num_str)
            except ValueError:
                continue

            total_found += 1
            if len(results) < max_results:
                # Smart truncation for grep (no submatches info available)
                if len(context) > max_context_length:
                    # Try to find the query pattern in context for smart truncation
                    try:
                        # Find the first match of the query in context
                        match = re.search(query, context)
                        if match:
                            # Smart truncation around the match
                            context_half = max_context_length // 2
                            start = max(0, match.start() - context_half)
                            end = min(len(context), match.end() + context_half)
                            
                            # Ensure we don't exceed max_context_length
                            if end - start > max_context_length:
                                start = max(0, end - max_context_length)
                            
                            context = context[start:end]
                            
                            # Add truncation markers
                            if start > 0:
                                context = "..." + context
                            if end < len(parts[2]):
                                context = context + "..."
                        else:
                            # Fallback: simple truncation
                            context = context[:max_context_length] + "..."
                    except re.error:
                        # If regex is invalid, use simple truncation
                        context = context[:max_context_length] + "..."
                results.append({
                    "file": file_path,
                    "line": line_number,
                    "column": 0,
                    "context": context,
                })

        truncated = total_found > max_results
        return json.dumps({
            "results": results,
            "truncated": truncated,
            "total_found": total_found,
            "fallback": "grep",
        })

    # Neither rg nor grep available
    return json.dumps({
        "error": "SearchToolNotFound",
        "message": "Neither ripgrep nor grep is available in PATH",
    })


# Task 8.2 — Register search_code tool
SEARCH_CODE_TOOL_CONFIG = ToolConfig(
    tool_id="search_code",
    tool_type="function",
    name="search_code",
    description=(
        "Search the workspace codebase for a regex pattern. "
        "Returns structured results with file, line, column, and context. "
        "Uses ripgrep if available, falls back to grep. "
        "Automatically excludes .git, node_modules, and dist directories. "
        "Supports | in query for regex alternation (OR). "
        "Multiple include/exclude patterns can be separated by |."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A regular expression pattern to search for",
            },
            "include": {
                "type": "string",
                "description": "Optional glob pattern to restrict search to matching file paths. Multiple patterns can be separated by | (e.g. '*.svelte|*.js' for file extensions, 'src/**' to limit to a directory, 'src/**/*.py' for specific files in a directory).",
            },
            "exclude": {
                "type": "string",
                "description": "Optional glob pattern to exclude matching file paths from the search. Multiple patterns can be separated by | (e.g. '*_test.py|*.pyc' to skip test files and bytecode, 'vendor/**' to skip a directory).",
            },
        },
        "required": ["query"],
    },
    builtin=True,
)

BUILTIN_TOOLS.append((SEARCH_CODE_TOOL_CONFIG, _search_code))


# ---------------------------------------------------------------------------
# Helper functions for terminal-based command execution
# ---------------------------------------------------------------------------
def _drain_terminal_buffer(terminal_info: dict) -> str:
    with terminal_info["buffer_lock"]:
        if not terminal_info["output_buffer"]:
            return ""
        chunk = "".join(terminal_info["output_buffer"])
        terminal_info["output_buffer"].clear()
        return chunk


def _strip_terminal_noise(text: str, command: str) -> str:
    """Clean terminal control noise while preserving human-visible output."""
    text = re.sub(r'\x1b\].*?(?:\x07|\x1b\\)', '', text)
    text = re.sub(r'\x1b\[[0-9;?]*[ -/]*[@-~]', '', text)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    text = re.sub(r'\[\?[0-9;]*[a-z]', '', text)

    cmd = command.strip()
    if cmd and text.startswith(cmd):
        text = text[len(cmd):].lstrip('\r\n')
    return text.strip()


def execute_command_in_terminal(
    session_id: str,
    command: str,
    timeout: int = 300,
    prompt_pattern: str = "",
    idle_timeout: int = 1000,
    read_after_delay: int = 0,
) -> str:
    """Send optional input to a terminal and collect screen output."""
    from runtime.server import get_terminal_for_session

    terminal_info = get_terminal_for_session(session_id)
    if not terminal_info:
        return "Error: No terminal session available for this session"

    if sys.platform == "win32":
        proc = terminal_info.get("proc")
        if not proc:
            return "Error: Terminal session has no proc"
        write_method = lambda cmd: proc.write(f"{cmd}\r\n")
    else:
        master_fd = terminal_info.get("master_fd")
        if not master_fd:
            return "Error: Terminal session has no master_fd"
        write_method = lambda cmd: os.write(master_fd, f"{cmd}\n".encode("utf-8"))

    try:
        idle_timeout_value = int(idle_timeout or 0)
    except (TypeError, ValueError):
        idle_timeout_value = 1000
    try:
        read_after_delay_value = int(read_after_delay or 0)
    except (TypeError, ValueError):
        read_after_delay_value = 0

    raw_enabled = read_after_delay_value > 0
    prompt_enabled = bool(prompt_pattern)
    idle_enabled = idle_timeout_value > 0

    if not raw_enabled and not prompt_enabled and not idle_enabled:
        idle_enabled = True
        idle_timeout_value = 1000

    idle_timeout_seconds = max(0.1, idle_timeout_value / 1000.0)
    read_after_delay_seconds = max(0.0, read_after_delay_value / 1000.0)
    check_interval = float(os.environ.get("OUTPUT_CHECK_INTERVAL", "0.05"))
    deadline = time.monotonic() + timeout

    if command:
        with terminal_info["buffer_lock"]:
            terminal_info["output_buffer"].clear()

        try:
            write_method(command)
        except Exception as e:
            return f"Error writing to terminal: {e}"

    collected = ""
    last_output_at = time.monotonic()
    start = last_output_at
    compiled_prompt = None
    if prompt_enabled:
        try:
            compiled_prompt = re.compile(prompt_pattern, re.MULTILINE)
        except re.error as exc:
            return f"Error: invalid prompt_pattern: {exc}"

    while time.monotonic() < deadline:
        chunk = _drain_terminal_buffer(terminal_info)
        now = time.monotonic()
        if chunk:
            collected += chunk
            last_output_at = now

        if raw_enabled and now - start >= read_after_delay_seconds:
            break

        if compiled_prompt:
            visible = _strip_terminal_noise(collected, command)
            if compiled_prompt.search(visible):
                break

        if idle_enabled and (collected or not command) and now - last_output_at >= idle_timeout_seconds:
            break

        time.sleep(check_interval)

    if time.monotonic() >= deadline:
        suffix = "" if collected else " (no output received)"
        return f"Error: command timed out after {timeout}s{suffix}"

    result = _strip_terminal_noise(collected, command)
    return result if result else "(empty output)"


# ---------------------------------------------------------------------------
# Task 10.1 — _exec_shell implementation
# ---------------------------------------------------------------------------

def _exec_shell(command: str, timeout: Optional[int] = None, background: bool = False) -> str:
    """Execute a shell command in the workspace with output limits.

    Args:
        command: The shell command to execute.
        timeout: Optional timeout in seconds. If None, uses EXEC_DEFAULT_TIMEOUT.
        background: If True and platform is Windows, run command in background using Start-Process.

    Returns:
        JSON string with keys: exit_code, stdout, stderr, truncated on success.
        When truncated, also includes omitted_lines.
        On error, returns JSON string with keys: error, message (and exit_code: null for Timeout).
    """
    # Reject empty command
    if not command or not command.strip():
        return json.dumps({"error": "EmptyCommand", "message": "Command must not be empty"})

    # Handle background execution on Windows
    if sys.platform == "win32":
        start_prefix = "start /B "
        start_index = command.find(start_prefix)
        if start_index != -1:
            command = command[start_index + len(start_prefix):]
            background = True

        if background:
            try:
                parts = shlex.split(command)
                if not parts:
                    return json.dumps({"error": "EmptyCommand", "message": "Command must not be empty"})

                program = parts[0]
                arguments = parts[1:] if len(parts) > 1 else []

                # Escape single quotes for PowerShell (double them)
                def escape_ps(s):
                    return s.replace("'", "''")

                # Build Start-Process command via PowerShell
                if arguments:
                    arg_str = " ".join(arguments)
                    ps_command = f"Start-Process -WindowStyle Hidden '{escape_ps(program)}' -ArgumentList '{escape_ps(arg_str)}'"
                else:
                    ps_command = f"Start-Process -WindowStyle Hidden '{escape_ps(program)}'"

                # Wrap in powershell.exe -Command
                command = f"powershell.exe -Command \"{ps_command}\""
            except ValueError as e:
                return json.dumps({"error": "CommandParseError", "message": f"Failed to parse command: {e}"})

    # Read configuration
    output_line_limit = int(os.environ.get("EXEC_OUTPUT_LINE_LIMIT", 1000))
    max_line_length = int(os.environ.get("EXEC_OUTPUT_COLUMN_LIMIT", 1000))
    if timeout is None:
        timeout_val = int(os.environ.get("EXEC_DEFAULT_TIMEOUT", 30))
    else:
        timeout_val = int(timeout)

    # Get workspace
    workspace = get_workspace()

    # Build environment: inherit current env, override TERM=dumb
    env = os.environ.copy()
    env["TERM"] = "dumb"

    # Use Popen with start_new_session=True so the abort handler can kill
    # the entire process group via kill_active_process().
    session_id = get_request_context("session_id")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding=SYSTEM_ENCODING, errors='replace',
            start_new_session=sys.platform != "win32",
            creationflags=creationflags,
        )
    except Exception as exc:
        return json.dumps({"error": "SpawnFailed", "message": str(exc)})

    # Register so the abort handler can kill it from another thread.
    if session_id:
        with _active_processes_lock:
            _active_processes[session_id] = proc

    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_val)
        except subprocess.TimeoutExpired:
            # Timeout — kill the process group.
            kill_process_group(proc)
            stdout, stderr = proc.communicate()
            return json.dumps({
                "error": "Timeout",
                "message": f"Command exceeded timeout of {timeout_val} seconds",
                "exit_code": None,
            })
    finally:
        # Unregister.
        if session_id:
            with _active_processes_lock:
                _active_processes.pop(session_id, None)

    # Check if the process was killed externally (abort handler).
    if _was_terminated_by_signal(proc):
        return json.dumps({
            "error": "Aborted",
            "message": "Command was aborted by user",
            "exit_code": proc.returncode,
        })

    # Truncate combined output to output_line_limit lines
    # Handle case where communicate() returns None (e.g., on Windows after taskkill)
    stdout = stdout or ""
    stderr = stderr or ""
    stdout_lines = stdout.splitlines(keepends=True)
    stderr_lines = stderr.splitlines(keepends=True)
    
    # Limit line length for each line
    def truncate_line(line):
        """Truncate a line if it exceeds max_line_length."""
        if len(line) > max_line_length:
            # Keep the newline character if present
            if line.endswith('\n'):
                return line[:max_line_length - 3] + "...\n"
            else:
                return line[:max_line_length - 3] + "..."
        return line
    
    stdout_lines = [truncate_line(line) for line in stdout_lines]
    stderr_lines = [truncate_line(line) for line in stderr_lines]
    
    total_lines = len(stdout_lines) + len(stderr_lines)

    truncated = False
    omitted_lines = 0

    if total_lines > output_line_limit:
        truncated = True
        omitted_lines = total_lines - output_line_limit
        # Allocate lines: fill stdout first, then stderr with remaining budget
        if len(stdout_lines) >= output_line_limit:
            stdout_lines = stdout_lines[:output_line_limit]
            stderr_lines = []
        else:
            remaining = output_line_limit - len(stdout_lines)
            stderr_lines = stderr_lines[:remaining]
        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)
        # Append truncation notice to stdout
        stdout += f"\n[...output truncated: {omitted_lines} lines omitted...]"
    else:
        # Reassemble lines even when not truncated, so line-length truncation takes effect
        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)

    response: dict = {
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": truncated,
    }
    if truncated:
        response["omitted_lines"] = omitted_lines

    return json.dumps(response)


# Task 10.2 — Register exec_shell tool
EXEC_SHELL_TOOL_CONFIG = ToolConfig(
    tool_id="exec_shell",
    tool_type="function",
    name="exec_shell",
    description=(
        "Execute a "
        + ("Windows cmd shell " if sys.platform == "win32" else "")
        + "command in the workspace directory. Runs in non-interactive mode (TERM=dumb). "
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute",
            },
            "timeout": {
                "type": "integer",
                "description": "Optional timeout in seconds (default: EXEC_DEFAULT_TIMEOUT)",
            },
        },
        "required": ["command"],
    },
    builtin=True,
)

# Add background parameter only on Windows
if sys.platform == "win32":
    EXEC_SHELL_TOOL_CONFIG.parameters["properties"]["background"] = {
        "type": "boolean",
        "description": "Run command in background mode.",
        "default": False,
    }

BUILTIN_TOOLS.append((EXEC_SHELL_TOOL_CONFIG, _exec_shell))


# ---------------------------------------------------------------------------
# Task 11.1 — _undo implementation
# ---------------------------------------------------------------------------

def _undo() -> str:
    """Undo the latest file-journal turn for the current session without changing conversation history.

    Returns:
        JSON string with keys: turn_key, restored_files on success.
        On error, returns JSON string with keys: error, message.
    """
    workspace = get_workspace()
    session_dir = get_request_context("session_dir")
    session_id = get_request_context("session_id")
    if not session_dir:
        return json.dumps({
            "error": "NoSessionJournal",
            "message": "No current session directory is available for file journal undo",
        })
    try:
        from runtime.context_manager import undo_latest_file_journal_turn
        result = undo_latest_file_journal_turn(workspace, session_dir, session_id=session_id)
    except Exception as exc:
        result = {"error": "UndoFailed", "message": str(exc)}
    return json.dumps(result)


# Task 11.2 — Register undo tool
UNDO_TOOL_CONFIG = ToolConfig(
    tool_id="undo",
    tool_type="function",
    name="undo",
    description=(
        "Undo the latest file journal turn in the current session without changing conversation history. "
        "Restores files to that turn's baseline and marks the turn as undone."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
    builtin=True,
)

BUILTIN_TOOLS.append((UNDO_TOOL_CONFIG, _undo))


# ---------------------------------------------------------------------------
# read_image — VLM 图片解读内置工具
# ---------------------------------------------------------------------------

def _make_read_image_fn(runtime):
    """创建 read_image 工具的可调用函数。

    Args:
        runtime: Runtime 实例，用于执行 VLM 推理
    """

    def read_image(base64_contents: list[str], prompt: str = "") -> str:
        """通过 VLM 解读图片内容。

        使用本地 VLM 对一张或多张图片进行理解。
        模型 ID 为 "read-image"，需在模型配置中预先注册。

        Args:
            base64_contents: 图片内容数组。每个元素可以是：
                - base64 编码的图片字符串
                - 本地文件路径
                - HTTP/HTTPS 图片链接
            prompt: 图片理解的指令。如果不提供，默认使用详细描述。

        Returns:
            VLM 对图片的解读结果文本。
        """
        # 解析所有图片为 base64
        resolved_images: list[str] = []
        for item in base64_contents:
            resolved = convert_image_to_base64(item)
            resolved_images.append(resolved)

        if not prompt:
            prompt = "请详细描述这张图片的内容。如果图片中包含文字，请完整识别出来。"

        # 构建消息：图片放在 images 字段
        user_message = Message(
            role="user",
            content=prompt,
            images=resolved_images,
        )

        # 非流式推理，使用 "read-image" 模型
        result = runtime.infer(InferenceRequest(
            model_id="read-image",
            messages=[user_message],
        ))

        if not result.success:
            return f"Error: VLM 推理失败: {result.error}"

        # 提取助手回复
        for msg in reversed(result.messages):
            if msg.role == "assistant" and msg.content:
                return msg.content

        return "（VLM 未返回任何内容）"

    return read_image


READ_IMAGE_TOOL_CONFIG = ToolConfig(
    tool_id="read_image",
    tool_type="function",
    name="read_image",
    description=(
        "通过 VLM（视觉语言模型）解读图片内容。"
        "支持本地文件路径、HTTP(S) 链接或 base64 编码的图片。"
        "适用于需要理解图片、识别图中文字、描述图像内容等场景。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "base64_contents": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "图片内容数组。每个元素可以是：本地文件路径（/path/to/img.png）、"
                    "HTTP/HTTPS URL（https://...）、或 base64 编码字符串。"
                ),
            },
            "prompt": {
                "type": "string",
                "description": (
                    "图片理解的指令（可选）。例如：'描述这张图片'、'识别图中的文字'、"
                    "'这张图片里有什么物体'。不提供则默认详细描述图片内容。"
                ),
            },
        },
        "required": ["base64_contents"],
    },
    builtin=True,
)

BUILTIN_TOOLS.append((READ_IMAGE_TOOL_CONFIG, None))  # callable 在 register_builtin_tools 中注入
