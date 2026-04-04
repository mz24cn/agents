"""Built-in tools for the Composable Agent Runtime.

Provides basic tools (bash, fetch) that are always available to the LLM,
especially after Skill progressive disclosure when the LLM needs to
execute commands described in SKILL.md.

These tools use only Python standard library modules.
"""

import os
import subprocess


import json
import urllib.request
import urllib.error
from typing import Optional

from runtime.models import ToolConfig
from runtime.registry import ToolRegistry


def _bash_execute(command: str, cwd: str = "") -> str:
    """Execute a shell command via subprocess.

    Args:
        command: The shell command to execute.
        cwd: Working directory for the command. Empty string means current dir.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("BASH_EXEC_TIMEOUT", 300)),
            cwd=cwd if cwd else None,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            err = result.stderr.strip()
            return f"Exit code {result.returncode}\nstderr: {err}\nstdout: {output}"
        return output if output else "(empty output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {os.environ.get('BASH_EXEC_TIMEOUT', 300)}s"
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
