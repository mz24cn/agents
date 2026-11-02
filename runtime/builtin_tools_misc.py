"""Built-in misc tools for the Agent Service.

This module holds the remaining built-in tools:

  - exec_cli: persistent terminal execution (with its terminal helpers)
  - fetch:    HTTP fetch
  - read_image: VLM image understanding (callable injected at register time)

The tool configs and callables are aggregated and registered by
``runtime.builtin_tools`` (the facade module).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

from runtime.common import (
    SYSTEM_ENCODING,
    heal_std_handles,
    is_windows_handle_error,
    get_request_context,
    convert_image_to_base64,
    env_float,
    env_int,
)
from runtime.models import InferenceRequest, Message, ToolConfig

logger = logging.getLogger("runtime.builtin_tools")

def _exec_cli(
    command: str,
    cwd: str = "",
    prompt_pattern: str = "",
    timeout: int = 1000,
    read_after_delay: int = 0,
) -> str:
    """Execute input via a persistent CLI terminal and return observed screen output.

    Completion is driven by provided parameters:
    - prompt_pattern: regex that completes when it matches visible output.
      When omitted, it defaults to the terminal's own prompt marker
      (user@hostname, e.g. ``^root@cc``) when the shell prompt carries one,
      so the call returns as soon as the command finishes and the shell is
      back at a prompt; otherwise completion stays idle-based.
    - timeout: milliseconds of no new output before returning (idle-based
      completion: the call ends early once output has been quiet this long).
      The runtime also reads this value to extend the wall-clock deadline
      allowed for the whole tool call, so large values let long-running
      commands run past the default tool timeout.
    - read_after_delay: milliseconds to read before returning regardless of output.

    An empty command reads the latest terminal progress without sending input.
    There is no internal hard cap: the bound is the ``timeout`` asked for here
    (which the runtime uses to extend the tool call's deadline) plus the
    TOOL_EXEC_TIMEOUT guard.  One terminal drives one command, so a call that
    arrives while the terminal is busy waits its turn instead of running
    concurrently -- its output is then collected on its own, not mixed with
    (or consumed by) the command that was already running.
    """
    session_id = get_request_context("session_id")
    if session_id:
        try:
            from runtime.server import get_or_create_terminal
            terminal_info = get_or_create_terminal(session_id)
            if terminal_info:
                if cwd and command:
                    command = f"cd {shlex.quote(cwd)} && {command}"
                if not prompt_pattern:
                    # Default completion signal: the shell's own prompt marker
                    # (user@hostname), derived from the terminal output buffer
                    # when detectable.  For an empty command (progress read)
                    # only reuse a previously derived pattern -- a fresh
                    # derivation could block on shell startup for nothing.
                    if command:
                        prompt_pattern = _derive_prompt_pattern(terminal_info)
                    else:
                        prompt_pattern = terminal_info.get("prompt_pattern") or ""
                result = execute_command_in_terminal(
                    session_id,
                    command,
                    timeout=timeout,
                    prompt_pattern=prompt_pattern,
                    read_after_delay=read_after_delay,
                )
                if not result.startswith("Error:"):
                    return result
                logger.debug("Terminal execution returned error, falling back to subprocess: %s", result)
        except Exception as e:
            logger.debug("Terminal execution failed, falling back to subprocess: %s", e)

    # Fallback for contexts without a terminal session.  No internal timeout:
    # the TOOL_EXEC_TIMEOUT guard bounds the caller's wait, and the worker
    # keeps running on its daemon thread until the command finishes.
    try:
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                encoding=SYSTEM_ENCODING, errors='replace',
                timeout=None, cwd=cwd if cwd else None,
            )
        except Exception as spawn_exc:
            # [Fix] stale/invalid std handles (WinError 6 句柄无效) make every
            # spawn with stdin=None fail; heal once and retry.
            if not (is_windows_handle_error(spawn_exc) and heal_std_handles()):
                raise
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                encoding=SYSTEM_ENCODING, errors='replace',
                timeout=None, cwd=cwd if cwd else None,
            )
        output = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if err:
            return (output + "\n" + err).strip()
        return output if output else "(empty output)"
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
            max_size = env_int("FETCH_MAX_SIZE", 262144)
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
        "Completion is driven by prompt_pattern, timeout, and read_after_delay; "
        "the first satisfied condition returns. Time values are in milliseconds. "
        "A larger timeout also extends how long this tool call may run. "
        "The terminal drives one command at a time: a call issued while another "
        "command is still running waits for its turn."
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
                "description": (
                    "Regex that completes output collection when it matches visible "
                    "terminal output. If omitted, defaults to the terminal's own "
                    "prompt marker (user@hostname) when the shell prompt carries "
                    "one, completing as soon as the shell returns to a prompt; "
                    "otherwise completion is idle-based (timeout)."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": (
                    "Milliseconds of no new output before returning (default 1000); "
                    "set to 0 to disable idle completion. Larger values also extend "
                    "the maximum time this tool call may run."
                ),
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
# ---------------------------------------------------------------------------
# Terminal output access: cursors, never draining
# ---------------------------------------------------------------------------
# The terminal's single PTY reader appends every chunk into
# ``terminal_info["output_buffer"]`` (runtime.server_state).  Reading must
# never consume what a *different* concurrent exec_cli call has not collected
# yet, so each reader keeps a cursor (an absolute chunk sequence) and takes
# only what was appended after it.

def _take_terminal_output(terminal_info: dict, cursor: int) -> tuple[str, int]:
    """Chunks appended after *cursor*, plus the updated cursor."""
    from runtime.server_state import take_terminal_output
    return take_terminal_output(terminal_info, cursor)


def _terminal_buffer_end(terminal_info: dict) -> int:
    """Cursor just past the last chunk buffered right now."""
    from runtime.server_state import terminal_buffer_end
    return terminal_buffer_end(terminal_info)


def _drain_terminal_buffer(terminal_info: dict) -> str:
    """Text new since the last *progress* read (no command was sent).

    Used by the empty-command "what is the terminal showing" read and when
    picking up what a previous call left behind.  It advances the terminal's
    own ``read_seq`` cursor instead of clearing the shared buffer, so a call
    that is still collecting keeps every byte inside its own cursor range.
    """
    cursor = terminal_info.get("read_seq", 0)
    text, new_cursor = _take_terminal_output(terminal_info, cursor)
    terminal_info["read_seq"] = new_cursor
    return text


# ---------------------------------------------------------------------------
# One terminal, one command: FIFO queue + cancellation of in-flight calls
# ---------------------------------------------------------------------------

class _TerminalQueue:
    """FIFO gate serialising exec_cli calls on one persistent terminal.

    A PTY shell drives a single foreground command, and its screen output
    cannot be attributed to a call after the fact, so calls that arrive while
    the terminal is busy wait their turn instead of writing into it -- that
    interleaving is what made one call return another call's output and left
    the other waiting for bytes that had already been consumed.

    A waiter that gets aborted gives its turn up (``_cancelled``), so an
    abandoned call can never stall the queue behind it.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._next_ticket = 0
        self._serving = 0
        self._cancelled: set[int] = set()
        self._owner = None
        self._owner_ticket = -1
        self._owner_started = None
        self._waiting = 0

    def acquire(self, state: dict, cancel_event: threading.Event) -> bool:
        """Wait for this call's turn; True once the terminal is ours."""
        with self._cond:
            ticket = self._next_ticket
            self._next_ticket += 1
            self._waiting += 1
            try:
                while True:
                    if cancel_event.is_set():
                        self._cancelled.add(ticket)
                        if self._serving == ticket:
                            self._advance_locked()
                        return False
                    if self._serving == ticket:
                        self._owner = state
                        self._owner_ticket = ticket
                        self._owner_started = time.monotonic()
                        state["started_at"] = self._owner_started
                        return True
                    self._cond.wait(0.2)
            finally:
                self._waiting -= 1

    def release(self, state: dict) -> None:
        with self._cond:
            if self._owner is not state:
                return
            self._owner = None
            self._owner_started = None
            if self._serving == self._owner_ticket:
                self._advance_locked()

    def _advance_locked(self) -> None:
        self._serving += 1
        while self._serving in self._cancelled:
            self._cancelled.discard(self._serving)
            self._serving += 1
        self._cond.notify_all()

    def busy(self) -> bool:
        with self._cond:
            return self._owner is not None

    def status(self) -> dict:
        with self._cond:
            return {
                "busy": self._owner is not None,
                "command": self._owner.get("command") if self._owner else "",
                "held_for": (
                    time.monotonic() - self._owner_started
                    if self._owner_started is not None else 0.0),
                "waiting": self._waiting,
            }


_terminal_state_init_lock = threading.Lock()


def _terminal_queue(terminal_info: dict) -> _TerminalQueue:
    """The terminal's FIFO gate, created on first use (fake terminals work)."""
    queue = terminal_info.get("command_queue")
    if queue is None:
        with _terminal_state_init_lock:
            queue = terminal_info.get("command_queue")
            if queue is None:
                queue = _TerminalQueue()
                terminal_info["command_queue"] = queue
    return queue


def _register_cli_call(terminal_info: dict, state: dict) -> None:
    """Track an in-flight exec_cli call so it can be interrupted."""
    terminal_info.setdefault("cli_calls", []).append(state)


def _unregister_cli_call(terminal_info: dict, state: dict) -> None:
    calls = terminal_info.get("cli_calls") or []
    for index, item in enumerate(calls):
        if item is state:                    # identity: never drop a sibling
            del calls[index]
            break


def _interrupt_terminal(terminal_info: dict) -> None:
    """Send Ctrl-C so the shell (and whatever runs in it) actually stops."""
    try:
        if sys.platform == "win32":
            proc = terminal_info.get("proc")
            if proc:
                proc.write("\x03")
        else:
            master_fd = terminal_info.get("master_fd")
            if master_fd:
                os.write(master_fd, b"\x03")
    except Exception as exc:
        logger.debug("exec_cli interrupt failed: %s", exc)


def cancel_running_command(session_id: str, tool_use_id: str = "") -> bool:
    """Interrupt the exec_cli call(s) still in flight on a session's terminal.

    Abort hook for the runtime: called with the timed-out call's
    ``tool_use_id`` when a single call exceeded its cap, or without one on a
    user abort, which stops every call of that session.  The collector loops
    notice the event and return the output they already have, and Ctrl-C frees
    the shell so the next call does not inherit a running command's output.

    Without this, a call the guard abandoned kept draining the terminal on its
    daemon thread and starved every later exec_cli call in the session.
    """
    from runtime.server import get_terminal_for_session

    terminal_info = get_terminal_for_session(session_id)
    if not terminal_info:
        return False
    cancelled = False
    running_cancelled = False
    for state in list(terminal_info.get("cli_calls") or []):
        # A tool_use_id targets one call; without one every in-flight call of
        # the session is stopped.  A call that carries no tool_use_id (context
        # without one, e.g. a remote child host) can therefore only be stopped
        # by an untargeted cancel -- that keeps a sibling call's output intact.
        if tool_use_id and state.get("tool_use_id") != tool_use_id:
            continue
        state["cancel"].set()
        cancelled = True
        # Ctrl-C only belongs to the terminal when the cancelled call is the
        # one driving it; interrupting on behalf of a call that is still
        # queued would kill an innocent command.
        if state.get("started_at") is not None:
            running_cancelled = True
    if running_cancelled:
        _interrupt_terminal(terminal_info)
    return cancelled


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


_PROMPT_MARKER_RE = re.compile(r"([\w.-]+)@([\w.-]+)")


def _read_terminal_buffer(terminal_info: dict) -> str:
    """Read what the terminal screen shows, without consuming anything.

    Only the tail of the buffer is joined: prompt derivation looks at the
    newest lines, and the buffer is no longer cleared by readers, so joining
    all of it every call would be needlessly expensive.
    """
    from runtime.server_state import read_terminal_tail
    return read_terminal_tail(terminal_info)


def _find_prompt_marker(visible_text: str) -> str:
    """Return the user@hostname marker from the most recent prompt line.

    The current prompt is the newest non-empty line, so lines are scanned
    from the end (last 10 non-empty lines) and the first marker found wins.
    Returns "" when no marker is present (custom prompts, PowerShell...).
    """
    lines = [ln for ln in visible_text.splitlines() if ln.strip()][-10:]
    for line in reversed(lines):
        m = _PROMPT_MARKER_RE.search(line)
        if m:
            return f"{m.group(1)}@{m.group(2)}"
    return ""


def _derive_prompt_pattern(terminal_info: dict, max_wait_ms: int = 1500) -> str:
    """Derive exec_cli's default prompt_pattern from the shell's own prompt.

    The persistent PTY shell prints its prompt at startup and after every
    command (default bash/zsh/fish PS1s carry a ``user@hostname`` marker,
    e.g. ``root@cc:/path# ``).  Matching that marker is a precise
    "command finished, shell ready" completion signal, so it becomes the
    default when the caller does not pass an explicit prompt_pattern.

    The result is cached per terminal in ``terminal_info``.  Only decisive
    outcomes are cached: "no output yet" (the shell may still be starting)
    is left uncached so the next call retries, while "output seen but no
    marker" (custom prompt, PowerShell, ...) is final.  Returns "" when no
    marker is detectable; completion then stays idle-based.
    """
    cached = terminal_info.get("prompt_pattern")
    if cached is not None:
        return cached
    if _terminal_queue(terminal_info).busy():
        # Another call is mid-command, so the screen currently shows partial
        # command output.  Deriving from it could cache "this shell has no
        # prompt marker" for the whole session and leave every later call
        # without a precise completion signal; reuse what is known instead.
        return ""
    try:
        deadline = time.monotonic() + max(100, max_wait_ms) / 1000.0
        no_marker_streak = 0
        while True:
            visible = _strip_terminal_noise(_read_terminal_buffer(terminal_info), "")
            marker = _find_prompt_marker(visible)
            if marker:
                pattern = f"^{re.escape(marker)}"
                terminal_info["prompt_pattern"] = pattern
                return pattern
            if visible.strip():
                no_marker_streak += 1
                # Two consecutive markerless reads confirm a custom prompt;
                # a single one may have been a partially written prompt line.
                if no_marker_streak >= 2:
                    terminal_info["prompt_pattern"] = ""
                    return ""
            else:
                no_marker_streak = 0
            if time.monotonic() >= deadline:
                return ""
            time.sleep(0.1)
    except Exception as e:
        logger.debug("prompt_pattern derivation failed: %s", e)
        return ""


def execute_command_in_terminal(
    session_id: str,
    command: str,
    timeout: int = 1000,
    prompt_pattern: str = "",
    read_after_delay: int = 0,
) -> str:
    """Send optional input to a terminal and collect screen output.

    ``timeout`` is the idle window in milliseconds: collection returns once
    no new output has been observed for that long (0 disables idle
    completion).  There is deliberately no internal hard cap here: the bound
    is the ``timeout`` the caller asks for, extended by the runtime guard that
    waits for this tool call.

    One terminal drives one command.  A call that arrives while the terminal
    is busy queues (FIFO) instead of writing into it, and every call collects
    only the chunks appended after its own cursor -- concurrent calls can no
    longer interleave their screen output or consume each other's bytes.
    """
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
        idle_timeout_value = int(timeout or 0)
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
    check_interval = env_float("OUTPUT_CHECK_INTERVAL", 0.05)

    compiled_prompt = None
    if prompt_enabled:
        try:
            compiled_prompt = re.compile(prompt_pattern, re.MULTILINE)
        except re.error as exc:
            return f"Error: invalid prompt_pattern: {exc}"

    cancel_event = threading.Event()
    state = {
        "cancel": cancel_event,
        "command": command,
        "tool_use_id": get_request_context("tool_use_id") or "",
        "started_at": None,
    }
    queue = _terminal_queue(terminal_info)

    if command:
        # Registered before waiting so a queued call is interruptible too.
        _register_cli_call(terminal_info, state)
        try:
            granted = queue.acquire(state, cancel_event)
        except BaseException:
            _unregister_cli_call(terminal_info, state)
            raise
        if not granted:
            _unregister_cli_call(terminal_info, state)
            status = queue.status()
            busy_note = (
                f" (another command is still running: {status['command']!r})"
                if status["busy"] else ""
            )
            logger.info(
                "exec_cli call cancelled while queued for the terminal%s",
                busy_note)
            return (
                "(interrupted before running: this exec_cli call was queued "
                "behind another command on the same terminal"
                f"{busy_note})"
            )

    # The cursor is taken once the terminal is ours: from here on, whatever the
    # PTY prints belongs to this call (echo, output, then the shell prompt).
    cursor = _terminal_buffer_end(terminal_info)

    if command:
        try:
            write_method(command)
        except Exception as e:
            _unregister_cli_call(terminal_info, state)
            queue.release(state)
            return f"Error writing to terminal: {e}"

    collected = ""
    cancelled = False
    last_output_at = time.monotonic()
    start = last_output_at

    try:
        while True:
            chunk, cursor = _take_terminal_output(terminal_info, cursor)
            now = time.monotonic()
            if chunk:
                collected += chunk
                last_output_at = now

            if cancel_event.is_set():
                cancelled = True
                break

            if raw_enabled and now - start >= read_after_delay_seconds:
                break

            if compiled_prompt:
                visible = _strip_terminal_noise(collected, command)
                if compiled_prompt.search(visible):
                    break

            if idle_enabled and (collected or not command) and now - last_output_at >= idle_timeout_seconds:
                break

            time.sleep(check_interval)
    finally:
        if command:
            # Progress reads (empty command) must not re-report output that a
            # completed call already returned; advancing read_seq reproduces the
            # old "buffer was consumed" behaviour without ever taking bytes
            # away from a call that is still collecting.
            terminal_info["read_seq"] = max(
                terminal_info.get("read_seq", 0), cursor)
            _unregister_cli_call(terminal_info, state)
            queue.release(state)

    result = _strip_terminal_noise(collected, command)
    if cancelled:
        note = ("(interrupted: this exec_cli call was cancelled; the command "
                "it sent may have been stopped before it finished)")
        return f"{result}\n\n{note}" if result else note
    return result if result else "(empty output)"
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

        # 提前检查 "read-image" 模型是否已注册，给出可操作的报错指引。
        # 该工具依赖一个注册了 model_id 或 labels 为 "read-image" 的 VLM 模型，
        # 否则直接进入推理只会得到晦涩的 MODEL_NOT_FOUND 错误。
        try:
            vlm_model = runtime._model_registry.get("read-image")
        except Exception:
            vlm_model = None
        if vlm_model is None:
            return (
                "Error: 未找到用于图片解读的 VLM 模型。请在模型管理中添加一个支持视觉（VLM）的模型，"
                "并将其 model_id 或 labels 设为 \"read-image\""
                "（例如 labels: [\"vlm\", \"read-image\"]），然后重试。"
            )
        if "vlm" not in (vlm_model.labels or []):
            logger.warning(
                "read_image: 模型 %s 已标记为 read-image 但 labels 缺少 \"vlm\"，"
                "若该模型实际不支持视觉输入，推理将失败",
                vlm_model.model_id,
            )

        # 非流式推理，使用 "read-image" 模型
        result = runtime.infer(InferenceRequest(
            model_id="read-image",
            messages=[user_message],
        ))

        if not result.success:
            if result.error_code == "MODEL_NOT_FOUND":
                return (
                    "Error: 未找到用于图片解读的 VLM 模型。请在模型管理中添加一个支持视觉（VLM）的模型，"
                    "并将其 model_id 或 labels 设为 \"read-image\""
                    "（例如 labels: [\"vlm\", \"read-image\"]），然后重试。"
                    f" 原始错误: {result.error}"
                )
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


# ---------------------------------------------------------------------------
# Misc tool cluster (exec_cli, fetch).  read_image's callable is injected at
# register time, so the facade appends (READ_IMAGE_TOOL_CONFIG, None) after
# this list to preserve the original BUILTIN_TOOLS ordering.
# ---------------------------------------------------------------------------
MISC_TOOLS = [
    (CLI_TOOL_CONFIG, _exec_cli),
    (FETCH_TOOL_CONFIG, _fetch_url),
]
