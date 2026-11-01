"""Contract tests for the SWE-bench-Live follow-ups to the built-in tools.

Each case pins behaviour the run showed was wrong, so it must not regress:

  * edit_file echoes a bounded unified diff instead of only line counts
  * an edit that changes nothing is a NoChange error, not a success
  * a whitespace-only mismatch is repaired only when the shift is uniform
  * the post-edit linter reverts only for defects in the edited file itself
  * exec_shell reports files a command changed outside the edit tools
  * TOOL_EXEC_TIMEOUT / EXEC_DEFAULT_TIMEOUT defaults stay correctly ordered
"""

from __future__ import annotations

import json as _json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.builtin_tools_coding import (  # noqa: E402
    _Linter,
    _edit_file,
    _exec_shell,
)
from runtime.runtime import (  # noqa: E402
    _get_default_command_exec_timeout,
    _get_tool_exec_timeout,
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_WORKSPACE", str(tmp_path))
    yield tmp_path


def _write(ws: Path, name: str, text: str) -> Path:
    path = ws / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------- timeout guard
def test_tool_exec_timeout_default_is_above_the_command_default(monkeypatch):
    monkeypatch.delenv("TOOL_EXEC_TIMEOUT", raising=False)
    monkeypatch.delenv("EXEC_DEFAULT_TIMEOUT", raising=False)
    assert _get_tool_exec_timeout() == 200.0
    # the tool's own deadline must fire first, otherwise the abandoned command
    # keeps writing to the workspace after the model was told it timed out
    assert _get_default_command_exec_timeout() < _get_tool_exec_timeout()
    assert _get_default_command_exec_timeout() == 180


def test_command_default_follows_the_guard(monkeypatch):
    monkeypatch.delenv("EXEC_DEFAULT_TIMEOUT", raising=False)
    monkeypatch.setenv("TOOL_EXEC_TIMEOUT", "60")
    assert _get_default_command_exec_timeout() == 40
    monkeypatch.setenv("EXEC_DEFAULT_TIMEOUT", "15")
    assert _get_default_command_exec_timeout() == 15
