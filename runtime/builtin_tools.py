"""Built-in tools for the Agent Service.

Facade module: the implementations live in the sibling modules

  - runtime/builtin_tools_coding.py  (file/code/exec tools + journal infra)
  - runtime/builtin_tools_agent.py   (delegate / talk_to)
  - runtime/builtin_tools_misc.py    (exec_cli / fetch / read_image)

This module aggregates ``BUILTIN_TOOLS``, provides ``register_builtin_tools``,
and re-exports every name that other modules / tests / examples import from
``runtime.builtin_tools`` so existing ``from runtime.builtin_tools import ...``
statements keep working.

``subprocess`` / ``shutil`` are kept in this namespace (and not used here)
because several tests patch the shared module objects through this facade,
e.g. ``monkeypatch.setattr(_bt.subprocess, "run", ...)`` and
``patch("runtime.builtin_tools.shutil.which", ...)``.
"""

import logging
import shutil  # noqa: F401  (re-exported so tests can patch through the facade)
import subprocess  # noqa: F401  (re-exported so tests can patch through the facade)

from runtime.common import _thread_local
from runtime.registry import ToolRegistry

from runtime.builtin_tools_coding import (  # noqa: F401
    _FileJournalManager,
    _Linter,
    _ManifestLock,
    _PathValidator,
    _REAL_TMP,
    _active_processes,
    _active_processes_lock,
    _atomic_write_json,
    _blob_ref_from_state,
    _capture_file_state,
    _edit_file,
    _exec_shell,
    _file_mode,
    _flatten_journal_path,
    _get_file_journal_manager,
    _journal_turn_key,
    _read_file,
    _read_gzip_blob,
    _restore_file_state,
    _search_code,
    _undo,
    _validate_path,
    _was_terminated_by_signal,
    _write_file,
    _write_gzip_blob,
    EDIT_FILE_TOOL_CONFIG,
    EXEC_SHELL_TOOL_CONFIG,
    READ_FILE_TOOL_CONFIG,
    SEARCH_CODE_TOOL_CONFIG,
    UNDO_TOOL_CONFIG,
    WRITE_FILE_TOOL_CONFIG,
    CODING_TOOLS,
    kill_active_process,
)

from runtime.builtin_tools_agent import (  # noqa: F401
    DELEGATE_TOOL_CONFIG,
    TALK_TO_TOOL_CONFIG,
    _make_delegate_fn,
    _make_talk_to_fn,
    _no_runtime_delegate,
    resolve_tool_ids,
)

from runtime.builtin_tools_misc import (  # noqa: F401
    CLI_TOOL_CONFIG,
    FETCH_TOOL_CONFIG,
    READ_IMAGE_TOOL_CONFIG,
    _drain_terminal_buffer,
    _exec_cli,
    _fetch_url,
    _make_read_image_fn,
    _strip_terminal_noise,
    execute_command_in_terminal,
    MISC_TOOLS,
)

logger = logging.getLogger("runtime.builtin_tools")

# ---------------------------------------------------------------------------
# BUILTIN_TOOLS aggregation
#
# Preserves the original registration order:
# exec_cli, fetch, read_file, write_file, edit_file, search_code, exec_shell,
# undo, read_image.  read_image's callable is injected at register time
# (runtime-dependent), so it is appended as (config, None) exactly like before.
# ---------------------------------------------------------------------------
BUILTIN_TOOLS = MISC_TOOLS + CODING_TOOLS + [(READ_IMAGE_TOOL_CONFIG, None)]


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

    # Register delegate and talk_to tools with runtime-aware callable
    if runtime is not None:
        delegate_fn = _make_delegate_fn(runtime, _thread_local)
        read_image_fn = _make_read_image_fn(runtime)
        talk_to_fn = _make_talk_to_fn(runtime, _thread_local)
    else:
        delegate_fn = _no_runtime_delegate
        read_image_fn = _no_runtime_delegate
        talk_to_fn = _no_runtime_delegate
    tool_registry.register(DELEGATE_TOOL_CONFIG, callable_fn=delegate_fn)
    tool_registry.register(READ_IMAGE_TOOL_CONFIG, callable_fn=read_image_fn)
    tool_registry.register(TALK_TO_TOOL_CONFIG, callable_fn=talk_to_fn)
    ids.append("delegate")
    ids.append("talk_to")

    return ids
