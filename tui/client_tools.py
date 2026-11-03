"""Client-side tools for the terminal front end: AskUser and local_shell.

A client-side tool is registered on the agent service as JSON only - name,
description, parameter schema - and the service never runs it.  When the model
calls one, the inference loop ends *normally* and hands the call back
(``client_tool_calls``), and this module is what the terminal runs locally to
answer it.  The result is posted back into the same session as a ``role='tool'``
message and the loop continues.

Presentation is *not* decided here: ``ask_user`` is injected by the caller
(``tui/app.py`` owns the terminal), which keeps this module importable and
testable on its own.  ``tui/`` deliberately has no dependency on ``runtime/``,
so nothing here imports the agent service.

The contract is mirrored in ``runtime/client_tools.py`` (the specification and
the ``tools.json`` registration) and ``web/src/lib/client-tools.js`` (browser);
``tests/test_client_tool_catalogue.py`` fails if the three drift apart.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Definitions shipped with every request ("client_tools")
# ---------------------------------------------------------------------------

ASKUSER_TOOL: Dict[str, Any] = {
    "name": "AskUser",
    "description": (
        "Ask the human user a question and wait for them to choose one of the given "
        "options. Use it when only the user can decide: a requirement that is "
        "ambiguous, a real trade-off between approaches, a risky or irreversible "
        "action, or a choice among alternatives you cannot rank from the information "
        "given. Ask one question per call, with 2-8 short mutually exclusive options. "
        "The result is the chosen option verbatim. Take that choice into account in "
        "the next step, and never invent an answer on the user's behalf."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The question to put in front of the user. Ask it in the language the "
                    "user is writing in, in one or two sentences, self-contained enough to "
                    "answer without re-reading the transcript."
                ),
            },
            "options": {
                "type": "array",
                "minItems": 2,
                "maxItems": 8,
                "items": {"type": "string", "minLength": 1},
                "description": (
                    "The choices the user can pick from, in display order. They must be "
                    "mutually exclusive and actionable, a few words each; the chosen string "
                    "comes back verbatim, so phrase each one as the decision it represents. "
                    "Free text is not offered - if 'something else' is a real possibility, "
                    "say so in the question and let the user answer in their next message."
                ),
            },
            "timeout": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": (
                    "Seconds to wait for the user. 0 (the default) waits forever, which is "
                    "the right choice for anything the user has to think about. Use a "
                    "positive value only when waiting is actually worse than deciding, and "
                    "pair it with `default_option`."
                ),
            },
            "default_option": {
                "type": "string",
                "description": (
                    "The option applied by itself when the countdown in `timeout` runs out "
                    "and the user has not answered. Must be one of `options`; ignored when "
                    "`timeout` is 0."
                ),
            },
        },
        "required": ["question", "options"],
    },
}

LOCAL_SHELL_TOOL: Dict[str, Any] = {
    "name": "local_shell",
    "description": (
        "Run a shell command on the machine this conversation is being driven from "
        "(the user's own machine, not the agent service host): bash on Linux/macOS, "
        "PowerShell on Windows. Returns exit_code, stdout and stderr. Available only "
        "when the user's client offers it - use it for things that have to happen "
        "where the user is sitting (opening a local file, checking local tooling), "
        "never for work the agent service can do itself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "The command line to run in the caller's shell: bash on Linux/macOS, "
                    "PowerShell on Windows."
                ),
            },
            "timeout": {
                "type": "integer",
                "minimum": 1,
                "description": "Seconds to wait for the command before killing it.",
            },
        },
        "required": ["command"],
    },
}

#: How long a handed-back ``local_shell`` call may run when the model does not
#: say (kept well below the HTTP/read timeout of the resume request).
SHELL_DEFAULT_TIMEOUT = 120
#: Output caps, mirroring the agent service's own shell tool so the model sees
#: the same shape from either execution side.
SHELL_LINE_LIMIT = 1000
SHELL_COLUMN_LIMIT = 1000

#: Answers the model may get that were not really the user's decision.
CANCELLED_ANSWER = "(the user closed the question without choosing)"


def client_tool_definitions(include_shell: bool = False,
                            include_ask_user: bool = True) -> List[Dict[str, Any]]:
    """The ``client_tools`` array to send with every inference request.

    ``AskUser`` is on by default because a terminal has no tool list to tick:
    it is how the model reaches the human instead of guessing. ``--no-ask-user``
    turns it off for callers that want a strictly unattended run.
    """
    tools = []
    if include_ask_user:
        tools.append(ASKUSER_TOOL)
    if include_shell:
        tools.append(LOCAL_SHELL_TOOL)
    return tools


def client_tool_names(include_shell: bool = False,
                      include_ask_user: bool = True) -> List[str]:
    return [t["name"] for t in client_tool_definitions(include_shell, include_ask_user)]


# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------

def parse_arguments(arguments: Any) -> Dict[str, Any]:
    """Accept the JSON string the service hands back, or an already-parsed dict."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        raw = arguments.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            # Not JSON at all: keep it as-is under ``_raw`` and let each handler
            # use it for its one required field rather than dropping the value.
            return {"_raw": raw}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {}


def question_request(args: Dict[str, Any]) -> Tuple[str, List[str], int, Optional[str]]:
    """Normalise an ``AskUser`` call into ``(question, options, timeout, default)``.

    Tolerant on purpose: a model may send numbers as strings, one option where
    two are required, or a default that only resembles one of the options.  The
    caller still gets a usable question out of it.
    """
    question = str(args.get("question") or args.get("_raw") or "").strip()
    raw_options = args.get("options")
    if isinstance(raw_options, str):
        raw_options = [line for line in raw_options.splitlines() if line.strip()]
    if not isinstance(raw_options, list):
        raw_options = []
    options: List[str] = []
    for item in raw_options:
        if item is None:
            continue
        text = str(item).strip()
        if text and text not in options:
            options.append(text)
    if not question:
        question = "请选择"
    try:
        timeout = int(args.get("timeout") or 0)
    except (TypeError, ValueError):
        timeout = 0
    timeout = max(0, timeout)
    default_raw = args.get("default_option")
    default = str(default_raw).strip() if default_raw is not None and str(default_raw).strip() else None
    return question, options, timeout, default


def normalize_answer(options: List[str], answer: Any) -> Optional[str]:
    """Map a user answer onto one of *options*, or ``None`` when it matches none.

    Accepts the exact string, a case/whitespace-insensitive match, a 1-based
    number, or a prefix that only one option starts with.
    """
    if answer is None:
        return None
    text = str(answer).strip()
    if not text:
        return None
    for option in options:
        if option == text:
            return option
    folded = text.casefold()
    for option in options:
        if option.casefold() == folded:
            return option
    try:
        index = int(text)
    except ValueError:
        index = 0
    if 1 <= index <= len(options):
        return options[index - 1]
    hits = [option for option in options if option.casefold().startswith(folded)]
    if len(hits) == 1:
        return hits[0]
    return None


# ---------------------------------------------------------------------------
# AskUser
# ---------------------------------------------------------------------------

def run_ask_user(args: Dict[str, Any], ask_user: Callable[..., Optional[str]],
                 clock: Callable[[], float] = time.monotonic) -> str:
    """Ask through *ask_user* and render the answer as the tool result.

    ``ask_user(question, options, timeout, default_option, deadline)`` returns
    the chosen option (any spelling), a marker string, or ``None`` for no
    answer.  The result text keeps the option verbatim so the model can quote
    it, and says out loud when the answer was not really the user's decision.
    """
    question, options, timeout, default = question_request(args)
    deadline = None if timeout <= 0 else clock() + timeout
    answer = ask_user(question, options, timeout, default, deadline)

    if answer is None:
        if timeout > 0:
            picked = normalize_answer(options, default)
            if picked is not None:
                return f"{picked} (auto-selected: the user did not answer within {timeout}s)"
            return f"(no answer: the user did not respond within {timeout}s and no default_option was given)"
        return CANCELLED_ANSWER
    picked = normalize_answer(options, answer)
    if picked is not None:
        return picked
    # An answer outside the list (a client that offers free text, or a stale
    # definition): report it as given rather than throwing it away.
    return str(answer).strip()


# ---------------------------------------------------------------------------
# local_shell — bash on POSIX, PowerShell on Windows
# ---------------------------------------------------------------------------

def shell_argv(command: str) -> List[str]:
    """Wrap *command* for the caller's own shell."""
    if os.name == "nt":
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    bash = shutil.which("bash")
    if bash:
        return [bash, "--norc", "--noprofile", "-c", command]
    return ["/bin/sh", "-c", command]


def _cap_lines(text: str, line_limit: int, column_limit: int) -> Tuple[str, int]:
    """Cap both line count and line width; return ``(text, dropped_lines)``."""
    if not text:
        return text, 0
    lines = text.splitlines(keepends=True)

    def cap(line: str) -> str:
        if len(line) <= column_limit:
            return line
        keep = line[: max(0, column_limit - 3)]
        return keep + ("...\n" if line.endswith("\n") else "...")

    lines = [cap(line) for line in lines]
    if len(lines) <= line_limit:
        return "".join(lines), 0
    dropped = len(lines) - line_limit
    return "".join(lines[:line_limit]) + f"\n[...output truncated: {dropped} lines omitted...]", dropped


def run_local_shell(args: Dict[str, Any], workspace: Optional[str] = None,
                    default_timeout: int = SHELL_DEFAULT_TIMEOUT,
                    line_limit: int = SHELL_LINE_LIMIT,
                    column_limit: int = SHELL_COLUMN_LIMIT) -> str:
    """Run the command on *this* machine and answer in the agent service's shape.

    Same shape as the service's own ``exec_shell`` - ``exit_code`` / ``stdout``
    / ``stderr`` / ``truncated`` (plus ``omitted_lines``), or ``error`` /
    ``message`` - so a model that knows that tool needs no new instructions.
    Simplified on purpose: no background mode, no workspace git snapshots, no
    abort plumbing; a hand-back round trip is not the place for those.
    """
    command = args.get("command")
    if not isinstance(command, str):
        command = args.get("_raw")
    if not isinstance(command, str) or not command.strip():
        return json.dumps({"error": "EmptyCommand", "message": "Command must not be empty"},
                          ensure_ascii=False)
    try:
        timeout = int(args.get("timeout") or default_timeout)
    except (TypeError, ValueError):
        timeout = default_timeout
    timeout = max(1, timeout)

    cwd = workspace or os.getcwd()
    env = os.environ.copy()
    env["TERM"] = "dumb"
    try:
        proc = subprocess.Popen(
            shell_argv(command), cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace",
        )
    except OSError as exc:
        return json.dumps({"error": "SpawnFailed", "message": str(exc)}, ensure_ascii=False)

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001 - best effort reaping
            pass
        return json.dumps({
            "error": "Timeout",
            "message": f"Command timed out after {timeout}s",
            "exit_code": None,
            "timed_out": True,
        }, ensure_ascii=False)

    stdout, dropped_out = _cap_lines(stdout or "", line_limit, column_limit)
    stderr, dropped_err = _cap_lines(stderr or "", line_limit, column_limit)
    dropped = dropped_out + dropped_err
    response: Dict[str, Any] = {
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": dropped > 0,
    }
    if dropped:
        response["omitted_lines"] = dropped
    return json.dumps(response, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def supports(name: str, include_shell: bool = False,
             include_ask_user: bool = True) -> bool:
    return name in client_tool_names(include_shell, include_ask_user)


def run_client_tool(name: str, arguments: Any, *,
                    ask_user: Callable[..., Optional[str]],
                    workspace: Optional[str] = None,
                    include_shell: bool = False) -> str:
    """Execute one handed-back client tool call and return its tool result text."""
    args = parse_arguments(arguments)
    if name == ASKUSER_TOOL["name"]:
        return run_ask_user(args, ask_user)
    if name == LOCAL_SHELL_TOOL["name"]:
        if not include_shell:
            return json.dumps({
                "error": "NotOffered",
                "message": "this terminal did not enable --local-shell",
            }, ensure_ascii=False)
        return run_local_shell(args, workspace=workspace)
    return json.dumps({
        "error": "UnknownClientTool",
        "message": f"this client cannot execute '{name}'",
    }, ensure_ascii=False)


def tool_result_message(call: Dict[str, Any], content: str) -> Dict[str, Any]:
    """The ``role='tool'`` message that answers a handed-back call."""
    message: Dict[str, Any] = {
        "role": "tool",
        "content": "" if content is None else str(content),
    }
    call_id = call.get("id")
    if call_id:
        message["tool_use_id"] = call_id
    name = call.get("name")
    if name:
        message["name"] = name
    return message


def pending_client_calls_from_messages(messages: List[Dict[str, Any]],
                                       names: List[str]) -> List[Dict[str, Any]]:
    """Calls in *messages* that were declared but never answered.

    The fallback path for a service that ends the stream without naming the
    handoff: the assistant turn declares the call, and no ``role='tool'`` frame
    with that ``tool_use_id`` follows it.
    """
    answered = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            answer_id = message.get("tool_use_id") or message.get("tool_call_id")
            if answer_id:
                answered.add(answer_id)
    pending: List[Dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in (message.get("tool_calls") or []):
            if not isinstance(call, dict):
                continue
            name = call.get("name")
            if name not in names:
                continue
            call_id = call.get("id") or call.get("tool_use_id")
            if call_id and call_id in answered:
                continue
            arguments = call.get("arguments")
            if arguments is None:
                function = call.get("function") or {}
                arguments = function.get("arguments")
            pending.append({
                "id": call_id or f"client-{name}-{len(pending)}",
                "name": name,
                "arguments": arguments if isinstance(arguments, str) else json.dumps(
                    arguments or {}, ensure_ascii=False),
            })
    return pending


def elapsed(deadline: Optional[float], clock: Callable[[], float] = time.monotonic) -> float:
    """Seconds left until *deadline* (``0.0`` once passed, ``inf`` if none)."""
    if deadline is None:
        return float("inf")
    return max(0.0, deadline - clock())


def windows_are_posix_shells() -> bool:  # pragma: no cover - platform trivia
    return sys.platform != "win32"
