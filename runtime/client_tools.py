"""Canonical catalogue of the client-side tools this project ships.

A ``tool_type="client_side"`` row tells the agent service how to show a tool to
the model, and the service never runs it: when the model calls one, the loop
ends normally and the call is handed back to whoever is driving the session.
That means every front end needs its own implementation, so the same contract
exists three times on purpose:

======================  ==========================================================
``runtime/client_tools.py``   this module — the specification, the built-in
                              registration (``builtin_configs()``) and the
                              cross-implementation contract test
``web/src/lib/client-tools.js``  browser implementation (custom dialog)
``tui/client_tools.py``       terminal implementation (question modal + shell)
======================  ==========================================================

AskUser is registered as a built-in tool at start-up
(``runtime.builtin_tools.register_builtin_tools``), so it is available without
anyone registering it by hand, and being built in keeps it out of
``tools.json``.

``tests/test_client_tool_catalogue.py`` keeps the three in agreement on the
names, parameters and required fields, so a front end can never silently drift
away from what the model is told.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# AskUser — ask the human a question and return the option they picked
# ---------------------------------------------------------------------------

ASKUSER_PARAMETERS: Dict[str, Any] = {
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
}

ASKUSER_CLIENT_TOOL: Dict[str, Any] = {
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
    "parameters": copy.deepcopy(ASKUSER_PARAMETERS),
}

# ---------------------------------------------------------------------------
# local_shell — run a command on the machine the *client* runs on
# ---------------------------------------------------------------------------
#
# Only useful in the narrow case where the model drives a remote agent service
# while its real working environment is the caller's machine: the terminal
# front end (``tui/ --local-shell``) is the one that offers it, and it is the
# reason the tool-edit form's example is a question rather than a shell command.

LOCAL_SHELL_PARAMETERS: Dict[str, Any] = {
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
}

LOCAL_SHELL_CLIENT_TOOL: Dict[str, Any] = {
    "name": "local_shell",
    "description": (
        "Run a shell command on the machine this conversation is being driven from "
        "(the user's own machine, not the agent service host): bash on Linux/macOS, "
        "PowerShell on Windows. Returns exit_code, stdout and stderr. Available only "
        "when the user's client offers it - use it for things that have to happen "
        "where the user is sitting (opening a local file, checking local tooling), "
        "never for work the agent service can do itself."
    ),
    "parameters": copy.deepcopy(LOCAL_SHELL_PARAMETERS),
}

#: Tools this project ships for its own front ends.
CATALOGUE: List[Dict[str, Any]] = [ASKUSER_CLIENT_TOOL, LOCAL_SHELL_CLIENT_TOOL]


def definition(name: str) -> Dict[str, Any]:
    """Return a copy of the catalogue entry called *name* (``{}`` if unknown)."""
    for entry in CATALOGUE:
        if entry.get("name") == name:
            return copy.deepcopy(entry)
    return {}


def validate(entry: Dict[str, Any]) -> str:
    """Return ``""`` when *entry* is a usable client tool definition, else why."""
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        return "name is required"
    description = entry.get("description")
    if not isinstance(description, str) or not description.strip():
        return "description is required"
    parameters = entry.get("parameters")
    if not isinstance(parameters, dict):
        return "parameters must be a JSON Schema object"
    if parameters.get("type") != "object":
        return 'parameters.type must be "object"'
    properties = parameters.get("properties")
    if not isinstance(properties, dict) or not properties:
        return "parameters.properties must list at least one property"
    required = parameters.get("required", [])
    if not isinstance(required, list) or any(k not in properties for k in required):
        return "parameters.required must name declared properties"
    return ""


def builtin_configs() -> List[Any]:
    """The built-in ``ToolConfig`` rows this project ships for its front ends.

    ``runtime.builtin_tools.register_builtin_tools()`` registers them at
    start-up, the same way it registers ``read_file`` or ``exec_shell``, so a
    fresh install needs no manual registration.  Only AskUser is built in:
    ``local_shell`` hands command execution to the user's machine, which has to
    be something the client opts into (``tui --local-shell``).

    Built-in rows are also never written to ``tools.json`` --
    ``ToolRegistry.save()`` skips ``builtin=True`` -- so nothing to clean up
    there either.
    """
    from runtime.builtin_tools_misc import CLIENT_SIDE_BUILTIN_TOOLS

    return list(CLIENT_SIDE_BUILTIN_TOOLS)


if __name__ == "__main__":  # pragma: no cover - operator convenience
    import argparse

    parser = argparse.ArgumentParser(
        description="Show the shipped client-side tool definitions"
    )
    parser.add_argument(
        "--name", action="append", dest="names",
        help="only show this tool name; repeatable (default: every catalogue entry)",
    )
    args = parser.parse_args()
    names = args.names or [entry["name"] for entry in CATALOGUE]
    print(json.dumps([definition(n) for n in names], indent=2, ensure_ascii=False))
