"""Built-in misc tools for the Agent Service.

This module holds the remaining built-in tools:

  - exec_cli: persistent terminal execution (with its terminal helpers)
  - fetch:    HTTP fetch
  - read_image: VLM image understanding (callable injected at register time)

The tool configs and callables are aggregated and registered by
``runtime.builtin_tools`` (the facade module).
"""

import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
import urllib.error

from runtime.common import (
    SYSTEM_ENCODING,
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
    timeout = env_int("CLI_EXEC_TIMEOUT", 300)
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
    check_interval = env_float("OUTPUT_CHECK_INTERVAL", 0.05)
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
