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
    _changed_files_report,
    _diff_preview,
    _edit_file,
    _exec_shell,
    _git_workspace_state,
    _shift_indent_str,
    _uniform_indent_shift,
)
from runtime.runtime import (  # noqa: E402
    _get_default_command_exec_timeout,
    _get_max_infer_output_chars,
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


# ---------------------------------------------------------------- diff echo
def test_edit_success_carries_unified_diff(workspace):
    _write(workspace, "a.py", "def f():\n    return 1\n")
    res = _json.loads(_edit_file(
        "a.py", "search_replace", old_str="    return 1", new_str="    return 2",
    ))
    assert res.get("file_modified") is True, res
    assert res["matched_at_line"] == 2
    # the model must be able to see what it wrote without re-reading the file
    assert "-    return 1" in res["diff"]
    assert "+    return 2" in res["diff"]
    assert res["diff_truncated"] is False


def test_diff_is_truncated_for_large_edits(workspace):
    old = "".join(f"line{i}\n" for i in range(200))
    new = "".join(f"changed{i}\n" for i in range(200))
    _write(workspace, "big.txt", old)
    res = _json.loads(_edit_file("big.txt", "search_replace", old_str=old, new_str=new))
    assert res["diff_truncated"] is True, res
    assert len(res["diff"]) < 4000, len(res["diff"])


# ------------------------------------------------------------- NoChange error
def test_identical_strings_are_reported_as_no_change(workspace):
    path = _write(workspace, "same.py", "x = 1\n")
    before = path.read_bytes()
    res = _json.loads(_edit_file(
        "same.py", "search_replace", old_str="x = 1", new_str="x = 1",
    ))
    assert res.get("error") == "NoChange", res
    assert path.read_bytes() == before


def test_context_only_patch_is_reported_as_no_change(workspace):
    """A diff whose +/- lines are identical used to be reported as a success."""
    path = _write(workspace, "ctx.py", "a\nb\nc\n")
    before = path.read_bytes()
    patch = (
        "--- a/ctx.py\n"
        "+++ b/ctx.py\n"
        "@@ -1,3 +1,3 @@\n"
        " a\n"
        "-b\n"
        "+b\n"
        " c\n"
    )
    res = _json.loads(_edit_file("ctx.py", "diff", patch=patch))
    assert res.get("error") == "NoChange", res
    assert path.read_bytes() == before


# ------------------------------------------------------- whitespace handling
def _read_lines(ws: Path, name: str):
    return (ws / name).read_text(encoding="utf-8").splitlines()


def test_uniform_indent_shift_is_repaired(workspace):
    # every line of old_str is one space deeper than the file
    _write(workspace, "p.json", '{\n  "a": {\n    "b": 1\n  }\n}\n')
    res = _json.loads(_edit_file(
        "p.json", "search_replace",
        old_str='   "a": {\n     "b": 1\n   }',
        new_str='   "a": {\n     "b": 2\n   }',
    ))
    assert "error" not in res, res
    assert res["indent_shift_applied"] == -1
    # new_str was shifted by the same amount: relative structure preserved
    assert _read_lines(workspace, "p.json") == ['{', '  "a": {', '    "b": 2', '  }', '}']


def test_non_uniform_indent_mismatch_is_still_refused(workspace):
    _write(workspace, "p.py", "def f():\n    if x:\n        return 1\n")
    res = _json.loads(_edit_file(
        "p.py", "search_replace",
        old_str="  if x:\n        return 1",       # deltas: -2 and 0
        new_str="  if x:\n        return 2",
    ))
    assert res.get("error") == "LineNotFound", res
    assert "缩进差量不一致" in res["message"] or "多个匹配" in res["message"], res
    assert _read_lines(workspace, "p.py") == ["def f():", "    if x:", "        return 1"]


def test_shift_that_would_dedent_below_zero_is_refused(workspace):
    _write(workspace, "p.py", "def f():\n    return 1\n")
    res = _json.loads(_edit_file(
        "p.py", "search_replace",
        old_str="        return 1",   # file has 4, old_str has 8 -> delta -4
        new_str="return 2",          # has no indent to remove
    ))
    assert res.get("error") == "LineNotFound", res
    assert _read_lines(workspace, "p.py") == ["def f():", "    return 1"]


def test_uniform_shift_does_not_clobber_python_block_structure():
    lines = ["    if x:", "        return 1", "", "    return 0"]
    delta, reason = _uniform_indent_shift([f"{l}\n" for l in lines], "\n".join(lines), 0)
    assert delta is None and reason, reason          # identical -> no shift needed
    shifted = _shift_indent_str("if x:\n    return 1", 4)
    assert shifted == "    if x:\n        return 1"
    assert _shift_indent_str("if x:", -4) is None


def test_diff_preview_shape():
    out = _diff_preview("a\nb\n", "a\nc\n", "f.txt")
    assert out["diff_truncated"] is False
    assert "-b" in out["diff"] and "+c" in out["diff"]
    assert _diff_preview("a\n", "a\n", "f.txt") == {"diff": "", "diff_truncated": False}


# ------------------------------------------------------------------- linter
def _check(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return _Linter().check(str(path))


def test_json_edits_are_not_reverted_by_a_broken_checker(tmp_path):
    ok, out = _check(tmp_path, "a.json", '{"a": [1, 2], "b": null}')
    assert ok, out
    ok, out = _check(tmp_path, "bom.json", "\ufeff" + '{"a": 1}')
    assert ok, out


def test_invalid_json_still_fails(tmp_path):
    ok, out = _check(tmp_path, "bad.json", '{"a": 1,}')
    assert not ok and "invalid JSON" in out


def test_cloudformation_yaml_tags_and_multi_document_pass(tmp_path):
    ok, out = _check(tmp_path, "t.yaml",
                     "Conditions:\n  IsProd: !Equals [!Ref Env, prod]\n"
                     "Resources:\n  R: !Ref Foo\n")
    assert ok, out
    ok, out = _check(tmp_path, "m.yaml", "---\na: 1\n---\nb: !Custom x\n")
    assert ok, out


def test_yaml_syntax_error_still_fails(tmp_path):
    ok, out = _check(tmp_path, "bad.yaml", "a: [1, 2\n")
    assert not ok and "invalid YAML" in out


def test_python_syntax_error_still_fails(tmp_path):
    ok, out = _check(tmp_path, "bad.py", "def f():\nreturn 1\n")
    assert not ok


def test_project_wide_typecheck_does_not_revert_the_edit():
    linter = _Linter()
    out = ('node_modules/@rjsf/utils/lib/x.d.ts(1,8): error TS1259: unrelated\n'
           'src/other.ts(3,1): error TS2304: pre-existing\n')
    ok, msg = linter._judge_project_check("/repo/src/app.tsx", out)
    assert ok and "ignored" in msg
    ok, _ = linter._judge_project_check(
        "/repo/src/app.tsx", "src/app.tsx(1,2): error TS2322: mine\n")
    assert not ok
    ok, _ = linter._judge_project_check(
        "/repo/src/app.tsx", "npm ERR! code ENOTFOUND\n")
    assert ok


def test_linter_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("EDIT_LINT_ENABLED", "0")
    ok, _ = _check(tmp_path, "bad.py", "def f():\nreturn 1\n")
    assert ok


# ------------------------------------------------------- exec change reporting
def test_changed_files_report_groups_by_status():
    prev = {"a.py": " M", "gone.py": " M"}
    now = {"a.py": " M", "new.py": "??"}
    payload = _changed_files_report(prev, now)
    assert payload is not None
    groups = payload["changed_files"]
    assert "new.py" in groups["??"]
    assert "gone.py" in groups["D"]
    assert "a.py" not in _json.dumps(groups)


def test_changed_files_report_is_silent_when_nothing_changed():
    state = {"a.py": " M"}
    assert _changed_files_report(state, dict(state)) is None


def test_exec_shell_reports_files_written_by_a_command(workspace, monkeypatch):
    ws = str(workspace)
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    (workspace / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=ws, check=True)

    res = _json.loads(_exec_shell("printf 'y = 2\\n' > generated.py"))
    assert res["exit_code"] == 0, res
    assert "generated.py" in _json.dumps(res.get("changed_files")), res

    # a command that touches nothing reports nothing
    res2 = _json.loads(_exec_shell("true"))
    assert "changed_files" not in res2, res2

    # attribution: an edit_file change is not blamed on the next command
    _edit_file("tracked.py", "search_replace", old_str="x = 1", new_str="x = 2")
    res3 = _json.loads(_exec_shell("true"))
    assert "changed_files" not in res3, res3


def test_exec_shell_change_report_can_be_disabled(workspace, monkeypatch):
    ws = str(workspace)
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    monkeypatch.setenv("EXEC_REPORT_CHANGES", "0")
    assert _git_workspace_state(ws) is None
    res = _json.loads(_exec_shell("printf 'z\\n' > f.txt"))
    assert "changed_files" not in res, res


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


def test_infer_output_cap_default_and_disable(monkeypatch):
    monkeypatch.delenv("MAX_INFER_OUTPUT_CHARS", raising=False)
    assert _get_max_infer_output_chars() == 65536
    monkeypatch.setenv("MAX_INFER_OUTPUT_CHARS", "1000")
    assert _get_max_infer_output_chars() == 1000
    monkeypatch.setenv("MAX_INFER_OUTPUT_CHARS", "0")
    assert _get_max_infer_output_chars() is None
