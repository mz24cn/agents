"""Built-in tools for the Composable Agent Runtime.

Provides basic tools (bash, fetch) that are always available to the LLM,
especially after Skill progressive disclosure when the LLM needs to
execute commands described in SKILL.md.

These tools use only Python standard library modules.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from typing import Optional

if sys.platform != "win32":
    import fcntl
    import pty
    import select
    import struct
    import termios

from runtime.models import ToolConfig
from runtime.registry import ToolRegistry


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

BUILTIN_TOOLS = [
    (BASH_TOOL_CONFIG, _bash_execute),
    (FETCH_TOOL_CONFIG, _fetch_url),
]


def register_builtin_tools(tool_registry: ToolRegistry) -> list[str]:
    """Register all built-in tools into the given ToolRegistry.

    Returns:
        List of registered tool_ids.
    """
    ids = []
    for config, fn in BUILTIN_TOOLS:
        tool_registry.register(config, callable_fn=fn)
        ids.append(config.tool_id)
    return ids
