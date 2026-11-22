"""Built-in misc tools for the Agent Service.

This module holds the remaining built-in tools:

  - exec_cli: persistent terminal execution (with its terminal helpers)
  - fetch:    HTTP fetch
  - read_image: VLM image understanding (callable injected at register time)
  - AskUser:  a *client-side* tool (config only: the client runs it, so there
              is deliberately no callable here)

The tool configs and callables are aggregated and registered by
``runtime.builtin_tools`` (the facade module).
"""

from __future__ import annotations

import copy
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

from runtime.client_tools import ASKUSER_CLIENT_TOOL
from runtime.common import (
    SYSTEM_ENCODING,
    heal_std_handles,
    is_windows_handle_error,
    get_request_context,
    convert_image_to_base64,
    env_float,
    env_int,
)
from runtime.models import CLIENT_SIDE_TOOL_TYPE, InferenceRequest, Message, ToolConfig

logger = logging.getLogger("runtime.builtin_tools")

def _exec_cli(
    command: str,
    cwd: str = "",
    prompt_pattern: str = "",
    timeout: int = 1000,
    read_after_delay: int = 0,
    extra_env: str = "",
) -> str:
    """Execute input via a persistent CLI terminal and return observed screen output.

    Completion is driven by the parameters:
    - prompt_pattern: regex that completes when it matches visible output.
      When omitted, the prompt of whatever interface the terminal is sitting at
      is re-read on every call: the shell's marker (``^root@host``), then
      ``^>>> $`` once python3 owns the screen, then the shell's marker again
      after ``exit``.  A pattern that never shows up while the output is quiet
      is reported back as ``pattern_miss`` instead of being waited out.
    - timeout: milliseconds of no new output before returning (idle-based
      completion: the call ends early once output has been quiet this long).
      The runtime also reads this value to extend the wall-clock deadline
      allowed for the whole tool call, so large values let long-running
      commands run past the default tool timeout.
    - read_after_delay: milliseconds to read before returning regardless of output.
    - extra_env: per-call environment variables for the terminal's persistent
      shell (``K=V K2='a b'``, ``K=`` to unset).  Only what differs from the
      environment that shell already holds is applied, so repeating the same
      env on every call costs nothing.

    An empty command reads the latest terminal progress without sending input.
    One terminal drives one command, so a call that arrives while the terminal
    is busy waits its turn instead of running concurrently -- its output is then
    collected on its own, not mixed with (or consumed by) the command that was
    already running.
    """
    session_id = get_request_context("session_id")
    if session_id:
        try:
            from runtime.server import get_or_create_terminal
            terminal_info = get_or_create_terminal(session_id)
            if terminal_info:
                if cwd and command:
                    command = f"cd {shlex.quote(cwd)} && {command}"
                    # Remembered so that a terminal rebuilt after its shell
                    # exited starts where this shell left off.
                    terminal_info["last_cwd"] = cwd
                result = execute_command_in_terminal(
                    session_id,
                    command,
                    timeout=timeout,
                    prompt_pattern=prompt_pattern,
                    read_after_delay=read_after_delay,
                    extra_env=extra_env,
                )
                if result.get("status") in ("terminal_unavailable", "error"):
                    logger.debug(
                        "Terminal execution returned %s, falling back to "
                        "subprocess: %s", result.get("status"),
                        result.get("error"))
                else:
                    return _render_exec_cli_result(result)
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


def _render_exec_cli_result(result: dict) -> str:
    """Turn exec_cli's structured result into the text its caller sees.

    The screen output stays the body; the completion state is appended as one
    short line whenever it says something the text alone cannot: that the
    waited-for prompt never appeared (the caller's pattern is wrong and every
    step will cost a wait), that the terminal was rebuilt or had exited, or that
    the command may still be running.  Leaving those silent is what made a
    mis-set pattern and a dead terminal invisible from the outside.
    """
    stdout = str(result.get("stdout") or "")
    notes: list[str] = []
    if result.get("terminal_exited"):
        notes.append(f"terminal_exited=true exit_code={result.get('exit_code')}")
    if result.get("rebuilt"):
        notes.append("terminal_rebuilt=true (its shell had exited)")
    if result.get("pattern_miss"):
        note = (f"completion=idle pattern_miss=true pattern={result.get('pattern')!r} "
                "(that prompt never appeared while the terminal was quiet)")
        hint = result.get("pattern_hint")
        if hint:
            note += f"; the screen ends at {hint!r}"
        notes.append(note + " -- fix prompt_pattern or leave it empty")
    if result.get("rearmed"):
        notes.append(f"rearmed=true: the screen changed interface, completion used "
                     f"{result.get('pattern')!r}")
    if result.get("status") == "still_running":
        notes.append("still_running=true -- call again with command='' to read more")
    if result.get("note"):
        notes.append(str(result["note"]))
    if not notes:
        return stdout
    tail = "\n".join(notes)
    return f"{stdout}\n\n[exec_cli: {tail}]" if stdout else f"[exec_cli: {tail}]"


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
        "command is still running waits for its turn. "
        "Without prompt_pattern the prompt of the interface the terminal is at is "
        "re-detected per call, so nested programs (python3, sqlite3, pdb, ssh) "
        "return as fast as the shell does -- prefer leaving it empty. "
        "The response says how the call completed (completion=pattern|idle) and "
        "flags pattern_miss when an explicit pattern never appeared, the terminal "
        "was rebuilt after its shell exited, or the command may still be running."
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
                    "terminal output. Leave it empty to let the tool use the prompt "
                    "of the interface the terminal is currently at (the shell's "
                    "user@host marker, then >>> / sqlite> / (Pdb) inside a nested "
                    "program, the shell's again after exit); this is faster than any "
                    "pattern you can guess. If an explicit pattern does not appear "
                    "while the output is quiet, the call ends idle-based and reports "
                    "pattern_miss=true instead of waiting out the timeout."
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
            "extra_env": {
                "type": "string",
                "description": (
                    "Environment variables for this call, applied to the terminal's "
                    "persistent shell: \"KEY=value KEY2='a b'\", \"KEY=\" to unset. "
                    "Only variables that differ from what that shell already holds "
                    "are re-applied, so passing the same value every call is free."
                ),
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


def _strip_terminal_noise(text: str, command: str,
                          keep_trailing: bool = False) -> str:
    """Clean terminal control noise while preserving human-visible output.

    ``keep_trailing`` keeps the trailing whitespace.  Which prompt the screen is
    sitting at is decided by how its last line *ends* ("sqlite> ", ">>> " --
    space included), and the rstrip that is right for display text erases
    exactly that signal.
    """
    text = re.sub(r'\x1b\].*?(?:\x07|\x1b\\)', '', text)
    text = re.sub(r'\x1b\[[0-9;?]*[ -/]*[@-~]', '', text)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    text = re.sub(r'\[\?[0-9;]*[a-z]', '', text)

    cmd = command.strip()
    if cmd and text.startswith(cmd):
        text = text[len(cmd):]
    if keep_trailing:
        return text.lstrip('\r\n')
    return text.strip()


_PROMPT_MARKER_RE = re.compile(r"([\w.-]+)@([\w.-]+)")

# ---------------------------------------------------------------------------
# Which prompt the terminal is sitting at -- re-read on every call
# ---------------------------------------------------------------------------
# The completion marker belongs to the *interface currently on screen*, and one
# persistent PTY cycles through several of them: the shell's own prompt, then a
# nested program's (python3 -> ">>> ", sqlite3 -> "sqlite> ", pdb -> "(Pdb) "),
# then the shell's again after ``exit``, or a remote host's after ``ssh``.
# Deriving it once at terminal start made every step inside a nested program
# fall back to the idle wait, because the shell marker had scrolled out of the
# screen tail: measured 1058 ms vs 53 ms per python-REPL step, 8603 ms vs 471 ms
# per sqlite3 step.  Re-reading the tail per call costs one regex over a few
# hundred bytes and follows the interface instead.
#
#: literal prompts of nested programs; longest first, so ">>> " wins over "> "
NESTED_PROMPT_LITERALS: tuple[tuple[str, str], ...] = (
    ("pdb", "(Pdb++) "),
    ("pdb", "(Pdb) "),
    ("sqlite", "sqlite> "),
    ("mysql", "mysql> "),
    ("gdb", "(gdb) "),
    ("python", ">>> "),
    ("shell", "PS1> "),
    ("shell", "PS2> "),
    ("python", "... "),
    ("repl", "> "),
    ("shell", "$ "),
    ("shell", "# "),
)

#: a markerless prompt line still ends in the conventional "$" / "#" sigil
_CUSTOM_PROMPT_TOKEN_RE = re.compile(r"[\w@.\-/~+\[\]()%*:>'\",;$#]*[$#]$")


def _looks_like_prompt_line(line: str) -> bool:
    """Does a markerless last line read as a shell prompt ("/app#", "(base) x#")?

    Deliberately narrow: the sigil has to end the line's last word and that word
    has to look like a path / host / venv prefix, so ordinary output that merely
    ends with "#" is not mistaken for a prompt.
    """
    if not line or len(line) > 96:
        return False
    token = line.rsplit(" ", 1)[-1]
    if token in ("$", "#"):
        return True                        # bare sh / root prompt
    if len(line) - len(token) > 32:
        return False                       # a sentence, not a prompt
    return bool(_CUSTOM_PROMPT_TOKEN_RE.search(token))


def _anchor_prompt(stem: str) -> str:
    """Line-anchored pattern for a prompt whose text is exactly *stem*.

    ``\\s*$`` rather than a literal trailing space: prompt literals are written
    with a trailing space (``">>> "``, ``"sqlite> "``, ``"(Pdb) "``), but a PTY
    read can end at ``">>>"`` with the space landing in the next chunk, and the
    screen text may equally well be stripped of it.  Anchoring both ends keeps
    the match to the screen's last line, which is what "awaits input" means.
    """
    return f"^{re.escape(stem)}\\s*$"


def _prompt_from_text(visible: str, tolerant: bool = False) -> tuple[str, str]:
    """``(pattern, interpreter)`` when *visible* ends at a recognised prompt.

    Only the screen's bottom line counts: after ``python3`` the shell prompt is
    still on screen one line up, and matching *that* is what kept waiting for a
    marker which would not print again until the REPL was exited.  The returned
    pattern is anchored to the end of a line, since "the program is waiting for
    input" is a statement about the end of the screen.

    ``tolerant`` additionally accepts a ``user@host`` marker on an older line
    (prompts that carry no trailing space, partially redrawn prompt lines) --
    used while deriving, never as evidence that a command finished.
    """
    if not visible:
        return "", ""
    lines = visible.splitlines()
    line = lines[-1] if lines else ""
    body = line.rstrip()
    if not body:
        return "", ""
    settled = visible[-1].isspace()       # the prompt line was fully written
    if settled:
        marker = _PROMPT_MARKER_RE.search(line)
        if marker:
            return f"^{re.escape(marker.group(0))}", "shell"
    for interpreter, literal in NESTED_PROMPT_LITERALS:
        stem = literal.rstrip()
        if line.endswith(literal) or (not settled and line == stem):
            # Unsettled case: python writes ">>> " and a PTY read can split off
            # the space -- the prompt is there, the space arrives next chunk.
            return _anchor_prompt(stem), interpreter
    if settled and _looks_like_prompt_line(body):
        return _anchor_prompt(body), "shell"
    if tolerant:
        marker = _find_prompt_marker(visible)
        if marker:
            return f"^{re.escape(marker)}", "shell"
    return "", ""


def _visible_tail(terminal_info: dict, keep_trailing: bool = False) -> str:
    """This terminal's screen tail, control noise removed."""
    return _strip_terminal_noise(_read_terminal_buffer(terminal_info), "",
                                 keep_trailing=keep_trailing)


def _screen_prompt(terminal_info: dict, tolerant: bool = False) -> tuple[str, str]:
    """The prompt this terminal's buffer currently ends at.

    Strict by default: only the screen's last line counts.  A marker found
    further up is not evidence of where the terminal is -- after ``python3`` the
    shell prompt is still on screen one line above the ``>>>`` -- and adopting it
    would send the next call back to the idle wait.  ``tolerant`` widens the
    search to older lines, which is only safe while deriving a prompt for a
    shell whose prompt style is not yet known.
    """
    return _prompt_from_text(_visible_tail(terminal_info, keep_trailing=True),
                             tolerant=tolerant)


def _remember_prompt(terminal_info: dict, pattern: str,
                     interpreter: str = "") -> str:
    """Record the prompt (and the program) this terminal is waiting at."""
    if pattern:
        terminal_info["prompt_pattern"] = pattern
        terminal_info["prompt_interpreter"] = interpreter or ""
    return pattern


def _follow_terminal_prompt(terminal_info: dict, visible: str) -> str:
    """Adopt the prompt this call's output ended at (into a REPL, out of one).

    Called after every command: the pattern the *next* call waits for is decided
    by what the screen shows now, so ``exit`` back to a shell restores the shell
    pattern instead of leaving a stale ">>> " to truncate shell output.
    """
    pattern, interpreter = _prompt_from_text(visible)
    if not pattern:
        pattern, interpreter = _screen_prompt(terminal_info)
    return _remember_prompt(terminal_info, pattern, interpreter)


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

    The answer is stored on the terminal in ``terminal_info`` (as the pattern
    plus the interpreter it belongs to).  Only decisive readings are stored: "no
    output yet" (the shell may still be starting) is left uncached so the next
    call retries, while "output seen but nothing to match" (custom prompt,
    PowerShell, ...) stands until the screen changes.  Returns "" when the
    screen offers nothing to match; completion then stays idle-based.
    """
    # Re-evaluated per call, not derived once and frozen: the same terminal is
    # a python3 / sqlite3 / pdb prompt while such a program owns the screen,
    # and the shell prompt again once it exits.  The screen tail is matched
    # against NESTED_PROMPT_LITERALS (and the shell marker) and the answer --
    # with the interpreter it belongs to -- is kept as the next call's pattern.
    #
    # The conservative rules of the original derivation still hold: "no output
    # yet" (a shell may still be starting) proves nothing and is not cached,
    # and only two consecutive markerless reads confirm a prompt we cannot
    # match.  Returns "" when the current screen offers nothing to match;
    # completion then stays idle-based for this call.
    pattern, interpreter = _screen_prompt(terminal_info)
    if pattern:
        return _remember_prompt(terminal_info, pattern, interpreter)
    cached = terminal_info.get("prompt_pattern")
    if cached is not None:
        # Nothing decisive on screen right now -- a command may still be
        # printing, or this shell's prompt may carry no marker at all.  Keep the
        # previous conclusion rather than treating "unknown" as "no marker".
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
            # Waiting for the shell to finish starting: tolerate a prompt that
            # carries no trailing space (the marker is still conclusive there).
            pattern, interpreter = _screen_prompt(terminal_info, tolerant=True)
            if pattern:
                return _remember_prompt(terminal_info, pattern, interpreter)
            if _visible_tail(terminal_info).strip():
                no_marker_streak += 1
                # Two consecutive markerless reads confirm a custom prompt;
                # a single one may have been a partially written prompt line.
                if no_marker_streak >= 2:
                    terminal_info["prompt_pattern"] = ""
                    terminal_info["prompt_interpreter"] = ""
                    return ""
            else:
                no_marker_streak = 0
            if time.monotonic() >= deadline:
                return ""
            time.sleep(0.1)
    except Exception as e:
        logger.debug("prompt_pattern derivation failed: %s", e)
        return ""


# ---------------------------------------------------------------------------
# How long to wait for a completion signal
# ---------------------------------------------------------------------------

#: consecutive calls whose awaited prompt never showed up while the terminal
#: was quiet.  After that many the pattern is reported as useless instead of
#: being waited for again: a wrong pattern otherwise costs a full timeout per
#: step (measured 3072 ms) while still returning the right text, which no
#: caller notices.
PATTERN_MISS_LIMIT = 2

#: floor for the wall-clock bound of one call.  A small ``timeout`` means "the
#: output has been quiet long enough", not "stop working on me", so collection
#: re-arms instead of returning; something has to stop it when the program
#: never goes quiet *and* never shows its prompt (a pager, a build log).
PATTERN_WAIT_CAP_SECONDS = 10.0


def _collect_until_prompt(terminal_info: dict, cursor: int, command: str,
                          patterns: list, initial: str, idle_seconds: float,
                          delay_seconds: float, raw_enabled: bool,
                          check_interval: float, cancel_event,
                          deadline: float) -> tuple[str, int, dict]:
    """Collect PTY output until the screen settles, and report how it settled.

    Four ways out:
      * "delay"   -- read_after_delay mode: take whatever arrived, done.
      * "pattern" -- the awaited prompt appeared at the end of the output.
      * "idle"    -- output has been quiet for ``idle_seconds``.
      * "idle" with ``timed_out`` -- the wall-clock bound was hit while the
        program was still working; the caller reports "may still be running".
      * "idle" with ``eof`` -- the PTY stream ended (the shell exited); the
        caller reports the terminal as gone instead of returning an empty screen.

    Silence alone is not completion.  A shell running ``sleep 3`` and a REPL
    evaluating a slow statement both go quiet for a moment and then print more;
    ending the call on that pause is what made nested-interpreter steps come
    back truncated.  So when no prompt is being waited for, the idle rule only
    fires once output has actually arrived (or the screen already ends at a
    prompt); otherwise collection continues to ``deadline``.  With a prompt in
    play the prompt alone decides, and the caller deals with the case where it
    never arrives -- waiting for a pattern that is not on screen is exactly what
    used to cost a full timeout per step.

    Returns ``(text, cursor, outcome)``.
    """
    collected = initial
    start = time.monotonic()
    last_output_at = start
    delay_deadline = start + delay_seconds if raw_enabled else None
    outcome = {
        "completion": "idle",
        "pattern": patterns[0][1].pattern if patterns else "",
        "source": patterns[0][0] if patterns else "none",
        "pattern_miss": False,
        "timed_out": False,
    }

    while True:
        chunk, cursor = _take_terminal_output(terminal_info, cursor)
        now = time.monotonic()
        if chunk:
            collected += chunk
            last_output_at = now

        if cancel_event.is_set():
            outcome["completion"] = "cancelled"
            break

        if terminal_info.get("pty_eof"):
            # The PTY stream ended (``exit``, a crash, the container going
            # away): nothing else can arrive, so waiting for a prompt or for
            # quietness is pure delay.  The caller reports the exited shell.
            outcome["eof"] = True
            break

        if delay_deadline is not None and now >= delay_deadline:
            outcome["completion"] = "delay"
            break

        visible = _strip_terminal_noise(collected, command, keep_trailing=True)
        for source, compiled in patterns:
            if compiled.search(visible):
                outcome["completion"] = "pattern"
                outcome["pattern"] = compiled.pattern
                outcome["source"] = source
                break
        if outcome["completion"] == "pattern":
            break

        quiet = now - last_output_at >= idle_seconds
        if not command:
            # Progress read: quietness is the whole question.
            if quiet:
                break
        elif patterns:
            if quiet:
                break        # caller decides: re-arm on the real prompt or not
        elif quiet and (visible.strip() or _screen_prompt(terminal_info)[0]):
            # Output arrived and stopped, or the screen already awaits input.
            break

        if now >= deadline:
            outcome["timed_out"] = True
            break
        time.sleep(check_interval)

    return collected, cursor, outcome


def _retire_dead_terminal(terminal_info: dict, exit_code) -> None:
    """Drop a terminal whose shell is gone from the registry."""
    from runtime.server_state import unregister_terminal_session
    terminal_id = str(terminal_info.get("terminal_id") or "")
    if terminal_id:
        unregister_terminal_session(terminal_id)
    logger.info("exec_cli: terminal %s shell exited (exit_code=%s); unregistered",
                terminal_id, exit_code)


def _terminal_cwd(terminal_info: dict) -> str:
    """Directory this terminal's shell was last asked to work in.

    Reported in every response because the terminal is persistent: the cwd of
    one call is inherited by the next, and a rebuilt shell is put back there.
    """
    return str(terminal_info.get("last_cwd") or "")


def _note_shell_exit(terminal_info: dict):
    """Report (and record) the shell being gone, if it is gone.

    Called once the call is over, so the last command's own output is kept:
    ``exit`` typed at a shell leaves a live-looking PTY that answers nothing
    afterwards, and the *next* caller must not have to rediscover that by
    spending its timeout on an empty screen.
    """
    from runtime.server_state import terminal_liveness
    alive, exit_code = terminal_liveness(terminal_info)
    if alive:
        return None
    if terminal_info.get("exit_code") is None:
        terminal_info["exit_code"] = exit_code
    return exit_code


# ---------------------------------------------------------------------------
# Per-call environment for the terminal's persistent shell
# ---------------------------------------------------------------------------

def _parse_env_pairs(raw) -> dict:
    """``"K=V K2='a b'"`` -> ``{"K": "V", "K2": "a b"}`` (``K=`` means unset).

    A value that is not a single shell word has to be quoted, so shlex is used
    to split; an unterminated quote is taken literally rather than failing the
    call, since env spelling should never be able to break a command.
    """
    if not raw or not str(raw).strip():
        return {}
    text = str(raw)
    try:
        import shlex
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    pairs: dict = {}
    for part in parts:
        key, sep, value = part.partition("=")
        if not sep or not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            logger.debug("exec_cli extra_env: ignoring %r (expected NAME=value)", part)
            continue
        pairs[key] = value
    return pairs



def _shell_env(terminal_info: dict) -> dict:
    """The environment this terminal's shell was last known to hold."""
    env = terminal_info.get("shell_env")
    return dict(env) if isinstance(env, dict) else {}


def _env_prefix(terminal_info: dict, raw) -> str:
    """Shell commands that bring this terminal's shell to the requested env.

    The tool writes into one persistent shell, so only what differs from the
    environment that shell already has has to be typed: passing the same env on
    every call then costs nothing, and a terminal rebuilt after its shell exited
    (which starts from the base process environment again) gets the whole set
    resent.  The commands go through the shell rather than the PTY API because
    a PTY cannot change the environment of a shell that is already running.
    """
    new = _parse_env_pairs(raw)
    old = _shell_env(terminal_info)
    lines: list[str] = []
    if sys.platform == "win32":
        quote = lambda v: "'" + v.replace("'", "''") + "'"
        for key, value in new.items():
            if old.get(key) == value:
                continue
            lines.append(f"$env:{key} = {quote(value)}" if value else f"Remove-Item Env:{key}")
        for key in old:
            if key not in new:
                lines.append(f"Remove-Item Env:{key}")
    else:
        quote = lambda v: "'" + v.replace("'", "'\"'\"'") + "'"
        for key, value in new.items():
            if old.get(key) == value:
                continue
            lines.append(f"export {key}={quote(value)}" if value else f"unset {key}")
        for key in old:
            if key not in new:
                lines.append(f"unset {key}")
    return " ; ".join(lines)


def _record_env(terminal_info: dict, raw) -> None:
    """Remember that the terminal's shell now holds this environment."""
    if not str(raw or "").strip():
        terminal_info.pop("shell_env", None)
        return
    terminal_info["shell_env"] = _parse_env_pairs(raw)


def execute_command_in_terminal(
    session_id: str,
    command: str,
    timeout: int = 1000,
    prompt_pattern: str = "",
    read_after_delay: int = 0,
    extra_env: str = "",
) -> dict:
    """Send optional input to a terminal and collect screen output.

    ``timeout`` is the idle window in milliseconds: collection returns once no
    new output has been observed for that long (0 disables idle completion).
    When a prompt pattern is what is being waited for, the wait is additionally
    capped at PATTERN_WAIT_CAP_SECONDS: waiting out the caller's timeout for a
    prompt that never arrives is never useful (see the pattern-miss guard).

    ``extra_env`` applies per-call variables to the terminal's persistent shell
    (``K=V``, ``K="a b"``, ``K=`` to unset).  Only the difference versus what
    that shell already has is typed, so repeating the same env on every call
    costs nothing and a rebuilt shell receives the whole set again.

    The result is a dict, because "the prompt matched", "output went quiet" and
    "this terminal is gone" must not look alike: stdout, completion
    (pattern|idle|delay|cancelled), status (ok|still_running|terminal_exited),
    pattern (the effective one), pattern_source (explicit|auto|none),
    pattern_miss, interpreter, terminal_id, cwd, shell_pid, rebuilt,
    terminal_exited, exit_code.

    One terminal drives one command.  A call that arrives while the terminal is
    busy queues (FIFO) instead of writing into it, and every call collects only
    the chunks appended after its own cursor -- concurrent calls can no longer
    interleave their screen output or consume each other's bytes.
    """
    from runtime.server import (
        get_or_create_terminal,
        get_terminal_for_session,
        terminal_liveness,
    )

    terminal_info = get_terminal_for_session(session_id)
    if not terminal_info:
        return {"stdout": "", "completion": "idle",
                "status": "terminal_unavailable",
                "error": "No terminal session available for this session"}

    rebuilt = False
    alive, exit_code = terminal_liveness(terminal_info)
    if not alive:
        # The shell behind this PTY is gone -- someone typed exit, it crashed,
        # the container ended.  Reading more of it can only ever yield an empty
        # screen once the caller's timeout has been spent, which looks exactly
        # like "still running", so the entry is dropped and -- the default
        # policy -- the session's terminal is rebuilt (workspace, environment
        # and last cwd replayed) and this very call runs on the replacement.
        logger.info("exec_cli: terminal %s shell exited (exit_code=%s); rebuilding",
                    terminal_info.get("terminal_id"), exit_code)
        replacement = get_or_create_terminal(session_id)
        if replacement and terminal_liveness(replacement)[0]:
            terminal_info = replacement
            rebuilt = True
        else:
            _retire_dead_terminal(terminal_info, exit_code)
            return {"stdout": "", "completion": "idle",
                    "status": "terminal_exited", "terminal_exited": True,
                    "rebuild_required": True, "exit_code": exit_code,
                    "error": "the terminal's shell has exited and could not be "
                             "rebuilt"}

    if sys.platform == "win32":
        proc = terminal_info.get("proc")
        if not proc:
            return {"stdout": "", "completion": "idle", "status": "error",
                    "error": "Terminal session has no proc"}
        write_method = lambda cmd: proc.write(f"{cmd}\r\n")
    else:
        master_fd = terminal_info.get("master_fd")
        if not master_fd:
            return {"stdout": "", "completion": "idle", "status": "error",
                    "error": "Terminal session has no master_fd"}
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
    explicit_pattern = str(prompt_pattern or "")
    idle_enabled = idle_timeout_value > 0

    if not raw_enabled and not explicit_pattern and not idle_enabled:
        idle_enabled = True
        idle_timeout_value = 1000

    idle_seconds = max(0.1, idle_timeout_value / 1000.0)
    read_after_delay_seconds = max(0.0, read_after_delay_value / 1000.0)
    check_interval = env_float("OUTPUT_CHECK_INTERVAL", 0.05)

    # Which prompt means "this call is over"?  An explicit pattern is honoured
    # as given but watched for misses, because a wrong one is silently
    # expensive: a full timeout per step, and still the right text.  Without
    # one, the pattern is re-read from the current screen on every call, so
    # completion follows the interface -- the shell's prompt, then ">>>" inside
    # python3, then the shell's prompt again after exit.
    patterns: list[tuple[str, re.Pattern]] = []
    if explicit_pattern:
        try:
            compiled = re.compile(explicit_pattern, re.MULTILINE)
        except re.error as exc:
            return {"stdout": "", "completion": "idle", "status": "error",
                    "error": f"invalid prompt_pattern: {exc}"}
        patterns.append(("explicit", compiled))
        pattern_text = explicit_pattern
    else:
        pattern_text = (
            # Progress read: reuse the prompt already known for this terminal
            # rather than deriving one -- derivation may sit waiting for a shell
            # that is still starting, which is not what "show me the screen"
            # asked for.
            str(terminal_info.get("prompt_pattern") or "")
            if not command else _derive_prompt_pattern(terminal_info)
        )
        if pattern_text:
            patterns.append(("auto", re.compile(pattern_text, re.MULTILINE)))
    interpreter = str(terminal_info.get("prompt_interpreter") or "")

    # Per-call environment for the persistent shell: only what differs from the
    # env already in it is typed.  Computed before the cursor is taken, so its
    # own echo is not mistaken for command output.
    env_prefix = _env_prefix(terminal_info, extra_env) if command else ""

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
            return {"stdout": ("(interrupted before running: this exec_cli call "
                               "was queued behind another command on the same "
                               f"terminal{busy_note})"),
                    "completion": "cancelled", "status": "ok",
                    "pattern": pattern_text,
                    "pattern_source": "explicit" if explicit_pattern else "auto",
                    "pattern_miss": False,
                    "terminal_id": str(terminal_info.get("terminal_id") or "")}

    # The cursor is taken once the terminal is ours: from here on, whatever the
    # PTY prints belongs to this call (echo, output, then the shell prompt).
    cursor = _terminal_buffer_end(terminal_info)

    if command:
        try:
            if env_prefix:
                # Typed as an ordinary line into the shell that also runs the
                # command, so later calls inherit the same environment.
                write_method(env_prefix)
            write_method(command)
        except Exception as e:
            _unregister_cli_call(terminal_info, state)
            queue.release(state)
            return {"stdout": "", "completion": "idle", "status": "error",
                    "error": f"writing to terminal failed: {e}"}

    deadline = time.monotonic() + max(
        idle_seconds, read_after_delay_seconds, PATTERN_WAIT_CAP_SECONDS)
    collected = ""
    outcome = {}
    attempts = 0
    # Whether the awaited prompt failed to appear while the terminal was quiet.
    # Tracked outside the outcome because a re-arm replaces it with a fresh
    # collection -- the caller still asked for a pattern that never showed up.
    missed = False
    try:
        while True:
            collected, cursor, outcome = _collect_until_prompt(
                terminal_info, cursor, command, patterns, collected,
                idle_seconds, read_after_delay_seconds, raw_enabled,
                check_interval, cancel_event, deadline)
            if (outcome["completion"] != "idle" or outcome["timed_out"]
                    or outcome.get("eof") or not patterns):
                break
            # A prompt was being waited for, but the PTY output has gone quiet
            # without it: the pattern does not fit the current screen.  Never
            # keep waiting for it up to the caller's timeout -- that is the
            # silently expensive failure (a wrong pattern costs a full timeout
            # per step, more than no pattern at all).
            attempts += 1
            if patterns and patterns[0][0] == "explicit":
                # The caller named the prompt to wait for and it is not on
                # screen.  Answer this frame on the idle rule and say so,
                # instead of substituting our own guess for theirs.
                missed = True
                break
            # Our own guess no longer fits the screen -- an interpreter started
            # or exited mid-call.  Retry against the prompt the screen shows
            # now, bounded so a wrong guess can never cost more than the idle
            # rule it replaces.
            fresh = _derive_prompt_pattern(terminal_info)
            if not fresh or fresh == outcome["pattern"]:
                break                     # nothing better to wait for
            patterns = [("auto", re.compile(fresh, re.MULTILINE))]
            outcome["pattern"] = fresh
            outcome["rearmed"] = True
            if attempts >= PATTERN_MISS_LIMIT:
                break
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

    # A miss stays a miss even when re-arming against the prompt the screen
    # really shows completed the call: the pattern the caller asked for never
    # appeared, and that is the fact the caller needs in order to correct it.
    outcome["pattern_miss"] = missed or bool(outcome.get("pattern_miss"))

    if env_prefix and outcome["completion"] != "cancelled":
        # Only claim the environment reached the shell once the screen settled:
        # after a cancel the "export ..." line may never have been executed.
        _record_env(terminal_info, extra_env)

    result = _strip_terminal_noise(collected, command)
    terminal_info["last_collect_at"] = time.monotonic()
    from runtime.server import touch_terminal
    touch_terminal(terminal_info)
    # Where did this call leave the terminal -- inside python3, or back at the
    # shell?  That answer decides the prompt the *next* call waits for, so
    # ``exit`` restores the shell pattern instead of leaving a stale ">>>" to
    # cut shell output short (and vice versa).
    _follow_terminal_prompt(terminal_info, result)
    interpreter = str(terminal_info.get("prompt_interpreter") or "")
    exit_code = _note_shell_exit(terminal_info)
    if outcome.get("pattern_miss"):
        streak = int(terminal_info.get("prompt_miss_streak", 0)) + 1
        terminal_info["prompt_miss_streak"] = streak
        logger.info(
            "exec_cli: prompt_pattern %r did not appear while the terminal was "
            "quiet (%d time(s)); completion fell back to idle",
            outcome["pattern"], streak)
    else:
        terminal_info["prompt_miss_streak"] = 0

    response = {
        "stdout": result,
        "completion": outcome["completion"],
        "status": "still_running" if outcome["timed_out"] else "ok",
        "pattern": outcome["pattern"],
        "pattern_source": outcome["source"],
        "pattern_miss": bool(outcome.get("pattern_miss")),
        "interpreter": interpreter,
        "terminal_id": str(terminal_info.get("terminal_id") or ""),
        "cwd": _terminal_cwd(terminal_info),
        "shell_pid": terminal_info.get("shell_pid"),
        "rebuilt": rebuilt,
        "terminal_exited": exit_code is not None,
    }
    if exit_code is not None:
        response["exit_code"] = exit_code
    if explicit_pattern:
        # What the caller asked for, so a miss is actionable: the effective
        # pattern may be a re-derived one, but the one to fix is this one.
        response["pattern_requested"] = explicit_pattern
    if outcome.get("rearmed"):
        # Our own prompt guess was replaced mid-call (an interpreter started or
        # exited); the screen, not the caller, was out of date.
        response["rearmed"] = True
    if missed:
        # Tell the caller what the screen really ends at, so the next call can
        # use it -- or leave prompt_pattern empty and let it be detected.
        hint = str(terminal_info.get("prompt_pattern") or "")
        if hint and hint != response["pattern"]:
            response["pattern_hint"] = hint
    if outcome["completion"] == "cancelled":
        note = ("(interrupted: this exec_cli call was cancelled; the command "
                "it sent may have been stopped before it finished)")
        response["stdout"] = f"{result}\n\n{note}" if result else note
    elif outcome["timed_out"]:
        response["note"] = ("no completion signal within the wait window: the "
                            "command may still be running -- call exec_cli with "
                            "command='' to read its further output")
    if not response["stdout"]:
        response["stdout"] = "(empty output)"
    return response
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
# Built-in *client-side* tool: AskUser
#
# A client-side tool is declared to the model and executed by the client, so
# there is no callable here on purpose -- the service must never run it.  Its
# name / description / parameters come from runtime.client_tools, which the web
# and TUI clients mirror; test_client_tool_catalogue.py keeps the three copies
# from drifting apart.
#
# Like every other built-in it is registered at start-up and deliberately not
# written to tools.json (ToolRegistry.save() skips builtin=True), so a fresh
# install can use it without registering anything by hand.  local_shell is NOT
# here: letting the service hand shell execution over to a client has to be
# something the client itself opts into.
# ---------------------------------------------------------------------------
ASKUSER_TOOL_CONFIG = ToolConfig(
    tool_id=ASKUSER_CLIENT_TOOL["name"],
    tool_type=CLIENT_SIDE_TOOL_TYPE,
    name=ASKUSER_CLIENT_TOOL["name"],
    description=ASKUSER_CLIENT_TOOL["description"],
    parameters=copy.deepcopy(ASKUSER_CLIENT_TOOL["parameters"]),
    builtin=True,
)

CLIENT_SIDE_BUILTIN_TOOLS = [ASKUSER_TOOL_CONFIG]


# ---------------------------------------------------------------------------
# Misc tool cluster (exec_cli, fetch).  read_image's callable is injected at
# register time, so the facade appends (READ_IMAGE_TOOL_CONFIG, None) after
# this list to preserve the original BUILTIN_TOOLS ordering.
# ---------------------------------------------------------------------------
MISC_TOOLS = [
    (CLI_TOOL_CONFIG, _exec_cli),
    (FETCH_TOOL_CONFIG, _fetch_url),
]
