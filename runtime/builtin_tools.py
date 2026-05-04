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
import shutil
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
            if '[continue]' in context and getattr(thread_local, 'last_session_id', None):
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


class _SnapshotManager:
    """Manage Git snapshots for write/edit operations.

    Parameters
    ----------
    workspace:
        The workspace root directory (must be a git repository).
    """

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace

    def _run_git(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + args,
            cwd=self.workspace,
            capture_output=True,
            text=True,
        )

    def snapshot(self, tool_name: str) -> str | dict:
        """Stage all changes and create a pre-action commit.

        Returns the commit SHA string on success, or a dict with
        ``{"error": ..., "message": ...}`` on failure.
        """
        # Verify git repo exists
        git_dir = os.path.join(self.workspace, ".git")
        if not os.path.exists(git_dir):
            return {
                "error": "GitNotInitialized",
                "message": "Workspace is not a Git repository",
            }

        # Stage all changes
        add_result = self._run_git(["add", "."])
        if add_result.returncode != 0:
            return {
                "error": "SnapshotFailed",
                "message": "Pre-action snapshot could not be created",
            }

        # Commit
        commit_msg = f"Agent pre-action: {tool_name}"
        commit_result = self._run_git(["commit", "-m", commit_msg, "--allow-empty"])
        if commit_result.returncode != 0:
            return {
                "error": "SnapshotFailed",
                "message": "Pre-action snapshot could not be created",
            }

        # Extract the commit SHA
        rev_result = self._run_git(["rev-parse", "HEAD"])
        if rev_result.returncode != 0:
            return {
                "error": "SnapshotFailed",
                "message": "Pre-action snapshot could not be created",
            }
        return rev_result.stdout.strip()

    def undo(self) -> dict:
        """Revert the workspace to the previous Git commit.

        Returns a dict with ``reverted_commit`` and ``restored_commit`` on
        success, or ``{"error": ..., "message": ...}`` on failure.
        """
        # Get current HEAD (the commit we are about to revert)
        head_result = self._run_git(["rev-parse", "HEAD"])
        if head_result.returncode != 0:
            return {
                "error": "UndoFailed",
                "message": head_result.stderr.strip(),
            }
        reverted_commit = head_result.stdout.strip()

        # Check that HEAD^ exists (i.e. there is a parent commit)
        parent_result = self._run_git(["rev-parse", "HEAD^"])
        if parent_result.returncode != 0:
            return {
                "error": "NoPreviousCommit",
                "message": "No previous snapshot to revert to",
            }

        # Perform the reset
        reset_result = self._run_git(["reset", "--hard", "HEAD^"])
        if reset_result.returncode != 0:
            return {
                "error": "UndoFailed",
                "message": reset_result.stderr.strip(),
            }

        # Remove untracked files and directories so the workspace matches the
        # restored commit exactly (e.g. a file created by write_file that was
        # never committed should disappear after undo).
        self._run_git(["clean", "-fd"])

        restored_commit = parent_result.stdout.strip()
        return {
            "reverted_commit": reverted_commit,
            "restored_commit": restored_commit,
        }


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
                timeout=30,
            )
            if result.returncode == 0:
                return (True, result.stdout + result.stderr)
            return (False, (result.stdout + result.stderr).strip())
        except Exception as exc:
            # Never raise — treat unexpected errors as a pass so that the
            # linter doesn't block edits when the tool is unavailable.
            logger.warning("_Linter.check(%r) raised unexpectedly: %s", path, exc)
            return (True, "")


# ---------------------------------------------------------------------------
# Task 4.1 — _read_file implementation
# ---------------------------------------------------------------------------

def _read_file(path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> str:
    """Read a file and return its contents as line-numbered output.

    Args:
        path: Path to the file (relative to AGENT_WORKSPACE or absolute within it).
        start_line: First line to return (1-indexed, inclusive). None means start of file.
        end_line: Last line to return (1-indexed, inclusive). None means end of file.

    Returns:
        JSON string with keys: content, total_lines, truncated, and optionally omitted_lines.
        On error, returns JSON string with keys: error, message.
    """
    workspace = os.path.realpath(os.environ.get("AGENT_WORKSPACE", ""))

    try:
        resolved_path = _validate_path(workspace, path)
    except ValueError as exc:
        return json.dumps({"error": exc.error_code, "message": str(exc)})  # type: ignore[attr-defined]

    # Check file exists
    if not os.path.isfile(resolved_path):
        return json.dumps({"error": "FileNotFound", "message": "The specified file does not exist"})

    # Read all lines
    with open(resolved_path, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()

    total_lines = len(all_lines)

    # Validate range parameters before applying them
    if start_line is not None and end_line is not None:
        if start_line > end_line:
            return json.dumps({"error": "InvalidRange", "message": "start_line must be less than or equal to end_line"})

    # Validate individual line numbers against file bounds
    if start_line is not None:
        if start_line < 1 or start_line > total_lines:
            return json.dumps({"error": "LineOutOfRange", "message": "Line number is out of file bounds"})
    if end_line is not None:
        if end_line < 1 or end_line > total_lines:
            return json.dumps({"error": "LineOutOfRange", "message": "Line number is out of file bounds"})

    # Determine which lines to return
    truncated = False
    omitted_lines = 0

    if start_line is None and end_line is None:
        # No range specified — apply truncation threshold
        threshold = int(os.environ.get("READ_TRUNCATION_LINES", 500))
        if total_lines > threshold:
            selected_lines = all_lines[:threshold]
            truncated = True
            omitted_lines = total_lines - threshold
        else:
            selected_lines = all_lines
        line_offset = 1  # 1-indexed start
    elif start_line is not None and end_line is not None:
        # Both specified
        selected_lines = all_lines[start_line - 1:end_line]
        line_offset = start_line
    elif start_line is not None:
        # Only start_line
        selected_lines = all_lines[start_line - 1:]
        line_offset = start_line
    else:
        # Only end_line
        selected_lines = all_lines[:end_line]
        line_offset = 1

    # Format lines as "{n}: {content}"
    content_parts = []
    for i, line in enumerate(selected_lines):
        line_num = line_offset + i
        # Preserve the line content as-is (including newline if present)
        content_parts.append(f"{line_num}: {line}")

    content = "".join(content_parts)

    result: dict = {
        "content": content,
        "total_lines": total_lines,
        "truncated": truncated,
    }
    if truncated:
        result["omitted_lines"] = omitted_lines

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
                "description": "Path to the file (relative to AGENT_WORKSPACE)",
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
    """Write content to a file atomically with a pre-write Git snapshot.

    Args:
        path: Path to the file (relative to AGENT_WORKSPACE or absolute within it).
        content: The content to write to the file (UTF-8 string).

    Returns:
        JSON string with keys: file, bytes_written, commit_id on success.
        On error, returns JSON string with keys: error, message.
    """
    import tempfile

    workspace = os.path.realpath(os.environ.get("AGENT_WORKSPACE", ""))

    try:
        resolved_path = _validate_path(workspace, path)
    except ValueError as exc:
        return json.dumps({"error": exc.error_code, "message": str(exc)})  # type: ignore[attr-defined]

    # Create pre-action snapshot
    snapshot_manager = _SnapshotManager(workspace)
    snapshot_result = snapshot_manager.snapshot("write_file")
    if isinstance(snapshot_result, dict):
        # snapshot returned an error dict
        return json.dumps(snapshot_result)
    commit_id = snapshot_result

    # Create parent directories if they don't exist
    parent_dir = os.path.dirname(resolved_path)
    try:
        os.makedirs(parent_dir, exist_ok=True)
    except OSError as exc:
        return json.dumps({"error": "WriteFailure", "message": str(exc)})

    # Atomic write: write to a tempfile in the same directory, then os.replace
    encoded = content.encode("utf-8")
    bytes_written = len(encoded)

    tmp_path = None
    try:
        # Create temp file in the same directory to ensure same filesystem
        fd, tmp_path = tempfile.mkstemp(dir=parent_dir)
        try:
            with os.fdopen(fd, "wb") as tmp_file:
                tmp_file.write(encoded)
        except Exception:
            # fd was already opened via fdopen; if fdopen fails, close fd manually
            try:
                os.close(fd)
            except OSError:
                pass
            raise

        os.replace(tmp_path, resolved_path)
        tmp_path = None  # successfully replaced, no cleanup needed
    except OSError as exc:
        return json.dumps({"error": "WriteFailure", "message": str(exc)})
    except Exception as exc:
        return json.dumps({"error": "WriteFailure", "message": str(exc)})
    finally:
        # Clean up temp file if os.replace failed
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # Compute relative path from workspace root for the response
    rel_path = os.path.relpath(resolved_path, workspace)

    return json.dumps({
        "file": rel_path,
        "bytes_written": bytes_written,
        "commit_id": commit_id,
    })


# Task 5.2 — Register write_file tool
WRITE_FILE_TOOL_CONFIG = ToolConfig(
    tool_id="write_file",
    tool_type="function",
    name="write_file",
    description=(
        "Write content to a file in the workspace atomically. "
        "Creates parent directories if they don't exist. "
        "A Git snapshot is created before writing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file (relative to AGENT_WORKSPACE)",
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
        path: Path to the file (relative to AGENT_WORKSPACE or absolute within it).
        mode: Either 'search_replace' or 'diff'.
        old_str: (search_replace mode) The text block to find and replace.
        new_str: (search_replace mode) The replacement text block.
        patch: (diff mode) A unified diff patch string to apply.

    Returns:
        JSON string with keys: file, commit_id, lines_changed on success.
        On error, returns JSON string with keys: error, message.
    """
    workspace = os.path.realpath(os.environ.get("AGENT_WORKSPACE", ""))

    try:
        resolved_path = _validate_path(workspace, path)
    except ValueError as exc:
        return json.dumps({"error": exc.error_code, "message": str(exc)})  # type: ignore[attr-defined]

    # Check file exists
    if not os.path.isfile(resolved_path):
        return json.dumps({"error": "FileNotFound", "message": "The specified file does not exist"})

    # Create pre-action snapshot
    snapshot_manager = _SnapshotManager(workspace)
    snapshot_result = snapshot_manager.snapshot("edit_file")
    if isinstance(snapshot_result, dict):
        return json.dumps(snapshot_result)
    commit_id = snapshot_result

    rel_path = os.path.relpath(resolved_path, workspace)

    if mode == "search_replace":
        return _edit_file_search_replace(
            resolved_path, rel_path, workspace, old_str, new_str, commit_id
        )
    elif mode == "diff":
        return _edit_file_diff(
            resolved_path, rel_path, workspace, patch, commit_id
        )
    else:
        return json.dumps({"error": "InvalidMode", "message": f"Unknown mode: {mode!r}. Use 'search_replace' or 'diff'."})


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
    commit_id: str,
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
        # Revert the file
        subprocess.run(
            ["git", "checkout", "--", rel_path],
            cwd=workspace,
            capture_output=True,
        )
        return json.dumps({"error": "LintFailed", "message": lint_output})

    # Calculate lines_changed
    new_line_count = len(new_str.splitlines()) if new_str else 0
    lines_changed = abs(new_line_count - old_line_count)

    return json.dumps({
        "file": rel_path,
        "commit_id": commit_id,
        "lines_changed": lines_changed,
    })


def _edit_file_diff(
    resolved_path: str,
    rel_path: str,
    workspace: str,
    patch: Optional[str],
    commit_id: str,
) -> str:
    """Apply a unified diff patch to the file."""
    if patch is None:
        return json.dumps({"error": "PatchFailed", "message": "patch parameter is required for diff mode"})

    # Apply the patch using `patch -p1` with stdin
    try:
        result = subprocess.run(
            ["patch", "-p1"],
            input=patch,
            cwd=workspace,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return json.dumps({"error": "PatchFailed", "message": "patch command not found"})

    if result.returncode != 0:
        return json.dumps({"error": "PatchFailed", "message": "Patch did not apply cleanly"})

    # Run linter
    linter = _Linter()
    passed, lint_output = linter.check(resolved_path)
    if not passed:
        # Revert the file
        subprocess.run(
            ["git", "checkout", "--", rel_path],
            cwd=workspace,
            capture_output=True,
        )
        return json.dumps({"error": "LintFailed", "message": lint_output})

    # Count lines changed from the patch (lines starting with + or - excluding +++ and ---)
    lines_changed = 0
    for line in patch.splitlines():
        if (line.startswith("+") and not line.startswith("+++")) or \
           (line.startswith("-") and not line.startswith("---")):
            lines_changed += 1

    return json.dumps({
        "file": rel_path,
        "commit_id": commit_id,
        "lines_changed": lines_changed,
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
        "A Git snapshot is created before editing. "
        "Syntax is checked after editing; if it fails the edit is reverted."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file (relative to AGENT_WORKSPACE)",
            },
            "mode": {
                "type": "string",
                "enum": ["search_replace", "diff"],
                "description": "Edit mode: 'search_replace' or 'diff'",
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
    """Split a pattern string by | into a list of non-empty patterns."""
    if not pattern:
        return []
    return [p.strip() for p in pattern.split("|") if p.strip()]


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
    workspace = os.path.realpath(os.environ.get("AGENT_WORKSPACE", ""))

    max_results = int(os.environ.get("SEARCH_MAX_RESULTS", 20))

    # Default excludes
    default_excludes = [".git", "node_modules", "dist"]

    # Try ripgrep first
    if shutil.which("rg") is not None:
        cmd = ["rg", "--json", query]
        # Add default excludes
        for excl in default_excludes:
            cmd += ["--glob", f"!{excl}"]
        # Add user-specified include patterns (support | as OR)
        if include:
            for pat in _split_patterns(include):
                cmd += ["--glob", pat]
        # Add user-specified exclude patterns (support | as OR)
        if exclude:
            for pat in _split_patterns(exclude):
                cmd += ["--glob", f"!{pat}"]

        try:
            result = subprocess.run(
                cmd,
                cwd=workspace,
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            return json.dumps({"error": "SearchToolNotFound", "message": str(exc)})

        results = []
        total_found = 0

        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if obj.get("type") != "match":
                continue

            total_found += 1
            if len(results) < max_results:
                data = obj.get("data", {})
                file_path = data.get("path", {}).get("text", "")
                # Make path relative to workspace
                if os.path.isabs(file_path):
                    file_path = os.path.relpath(file_path, workspace)
                line_number = data.get("line_number", 0)
                submatches = data.get("submatches", [])
                column = submatches[0].get("start", 0) if submatches else 0
                context = data.get("lines", {}).get("text", "").rstrip("\n").rstrip("\r\n")
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
        # Add user-specified include patterns (support | as OR)
        if include:
            for pat in _split_patterns(include):
                cmd += [f"--include={pat}"]
        # Add user-specified exclude patterns (support | as OR)
        if exclude:
            for pat in _split_patterns(exclude):
                cmd += [f"--exclude={pat}"]
        # Add the query and search path
        cmd += ["-E", query, "."]

        try:
            result = subprocess.run(
                cmd,
                cwd=workspace,
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            return json.dumps({"error": "SearchToolNotFound", "message": str(exc)})

        results = []
        total_found = 0

        for line in result.stdout.splitlines():
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
            try:
                line_number = int(line_num_str)
            except ValueError:
                continue

            total_found += 1
            if len(results) < max_results:
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
        "Automatically excludes .git, node_modules, and dist directories."
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
                "description": "Optional glob pattern to restrict search to matching files (e.g. '*.py')",
            },
            "exclude": {
                "type": "string",
                "description": "Optional glob pattern to exclude files from search (e.g. '*.min.js')",
            },
        },
        "required": ["query"],
    },
    builtin=True,
)

BUILTIN_TOOLS.append((SEARCH_CODE_TOOL_CONFIG, _search_code))


# ---------------------------------------------------------------------------
# Task 10.1 — _execute_command implementation
# ---------------------------------------------------------------------------

def _execute_command(command: str, timeout: Optional[int] = None) -> str:
    """Execute a shell command in the workspace with output limits.

    Args:
        command: The shell command to execute.
        timeout: Optional timeout in seconds. If None, uses EXEC_DEFAULT_TIMEOUT.

    Returns:
        JSON string with keys: exit_code, stdout, stderr, truncated on success.
        When truncated, also includes omitted_lines.
        On error, returns JSON string with keys: error, message (and exit_code: null for Timeout).
    """
    # Reject empty command
    if not command or not command.strip():
        return json.dumps({"error": "EmptyCommand", "message": "Command must not be empty"})

    # Read configuration
    output_line_limit = int(os.environ.get("EXEC_OUTPUT_LINE_LIMIT", 1000))
    if timeout is None:
        timeout_val = int(os.environ.get("EXEC_DEFAULT_TIMEOUT", 30))
    else:
        timeout_val = int(timeout)

    # Get workspace
    workspace = os.path.realpath(os.environ.get("AGENT_WORKSPACE", ""))

    # Build environment: inherit current env, override TERM=dumb
    env = os.environ.copy()
    env["TERM"] = "dumb"

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_val,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({
            "error": "Timeout",
            "message": f"Command exceeded timeout of {timeout_val} seconds",
            "exit_code": None,
        })

    stdout = result.stdout
    stderr = result.stderr

    # Truncate combined output to output_line_limit lines
    stdout_lines = stdout.splitlines(keepends=True)
    stderr_lines = stderr.splitlines(keepends=True)
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

    response: dict = {
        "exit_code": result.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": truncated,
    }
    if truncated:
        response["omitted_lines"] = omitted_lines

    return json.dumps(response)


# Task 10.2 — Register execute_command tool
EXECUTE_COMMAND_TOOL_CONFIG = ToolConfig(
    tool_id="execute_command",
    tool_type="function",
    name="execute_command",
    description=(
        "Execute a shell command in the workspace directory. "
        "Runs in non-interactive mode (TERM=dumb). "
        "Output is truncated to EXEC_OUTPUT_LINE_LIMIT lines. "
        "A default timeout of EXEC_DEFAULT_TIMEOUT seconds is applied."
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

BUILTIN_TOOLS.append((EXECUTE_COMMAND_TOOL_CONFIG, _execute_command))


# ---------------------------------------------------------------------------
# Task 11.1 — _undo implementation
# ---------------------------------------------------------------------------

def _undo() -> str:
    """Undo the most recent write or edit operation by reverting to the previous Git commit.

    Returns:
        JSON string with keys: reverted_commit, restored_commit on success.
        On error, returns JSON string with keys: error, message.
    """
    workspace = os.path.realpath(os.environ.get("AGENT_WORKSPACE", ""))
    snapshot_manager = _SnapshotManager(workspace)
    result = snapshot_manager.undo()
    return json.dumps(result)


# Task 11.2 — Register undo tool
UNDO_TOOL_CONFIG = ToolConfig(
    tool_id="undo",
    tool_type="function",
    name="undo",
    description=(
        "Undo the most recent write or edit operation by reverting the workspace "
        "to the previous Git commit (git reset --hard HEAD^). "
        "Returns the reverted commit ID and the restored commit ID."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
    builtin=True,
)

BUILTIN_TOOLS.append((UNDO_TOOL_CONFIG, _undo))
