"""Tests for runtime.builtin_tools — new builtin tool functions.

This file covers:
  - read_file, write_file, edit_file, search_code, exec_shell, undo

Test infrastructure:
  - `workspace` fixture: temporary directory with git init + initial commit
    and AGENTS_WORKSPACE environment variable set.
"""

import json
import os
import re
import subprocess
import tempfile
import pytest

from runtime.builtin_tools import (
    _FileJournalManager,
    _flatten_journal_path,
    _journal_turn_key,
    _PathValidator,
    _thread_local,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace(monkeypatch, tmp_path):
    """Provide a temporary git-initialised workspace.

    Steps:
    1. Create a temporary directory (provided by pytest's tmp_path).
    2. Run `git init` inside it.
    3. Configure git user name and email (required for commits).
    4. Create an initial README commit so the repo has at least one commit.
    5. Set the AGENTS_WORKSPACE environment variable to the temp dir path.
    6. Yield the Path object for use in tests.
    7. Cleanup is handled automatically by tmp_path.
    """
    ws = tmp_path

    # Initialise git repository
    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)

    # Configure git identity (needed for commits in a clean environment)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=ws, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=ws, check=True, capture_output=True,
    )

    # Create an initial commit
    readme = ws / "README.md"
    readme.write_text("# Test Workspace\n")
    subprocess.run(["git", "add", "README.md"], cwd=ws, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=ws, check=True, capture_output=True,
    )

    # Expose the workspace path via environment variable
    monkeypatch.setenv("AGENTS_WORKSPACE", str(ws))

    yield ws


@pytest.fixture
def journal_context(monkeypatch, tmp_path):
    session_dir = tmp_path / "chat_data" / "session1"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(_thread_local, "session_id", "session1", raising=False)
    monkeypatch.setattr(_thread_local, "session_dir", str(session_dir), raising=False)
    monkeypatch.setattr(_thread_local, "user_message_timestamp", "2026-05-11T10:20:30", raising=False)
    monkeypatch.setattr(_thread_local, "file_journal_manager", None, raising=False)
    yield session_dir
    monkeypatch.setattr(_thread_local, "file_journal_manager", None, raising=False)
    monkeypatch.setattr(_thread_local, "session_id", None, raising=False)
    monkeypatch.setattr(_thread_local, "session_dir", None, raising=False)
    monkeypatch.setattr(_thread_local, "user_message_timestamp", None, raising=False)



# ---------------------------------------------------------------------------
# Tests for _PathValidator
# ---------------------------------------------------------------------------

@pytest.fixture
def no_tmp_bypass(monkeypatch):
    """Prevent /tmp bypass in _validate_path."""
    import runtime.builtin_tools as _bt
    monkeypatch.setattr(_bt, "_REAL_TMP", "/__nonexistent_tmp_bypass__")


class TestPathValidator:
    """Unit tests for _PathValidator.validate()."""

    def test_valid_relative_path_inside_workspace(self, workspace):
        """Returns resolved absolute path for a valid relative path inside workspace."""
        validator = _PathValidator(str(workspace))
        result = validator.validate("subdir/file.txt")
        expected = os.path.realpath(os.path.join(str(workspace), "subdir", "file.txt"))
        assert result == expected

    def test_valid_absolute_path_inside_workspace(self, workspace):
        """Returns resolved absolute path for a valid absolute path inside workspace."""
        validator = _PathValidator(str(workspace))
        abs_path = str(workspace / "some_file.py")
        result = validator.validate(abs_path)
        assert result == os.path.realpath(abs_path)

    def test_raises_path_traversal_denied_for_escape_path(self, workspace, no_tmp_bypass):
        """Raises ValueError with error_code='PathTraversalDenied' for ../escape paths."""
        validator = _PathValidator(str(workspace))
        with pytest.raises(ValueError) as exc_info:
            validator.validate("../escape")
        assert exc_info.value.error_code == "PathTraversalDenied"

    def test_raises_path_traversal_denied_for_deep_escape(self, workspace, no_tmp_bypass):
        """Raises ValueError with error_code='PathTraversalDenied' for deeply nested escape."""
        validator = _PathValidator(str(workspace))
        with pytest.raises(ValueError) as exc_info:
            validator.validate("subdir/../../escape")
        assert exc_info.value.error_code == "PathTraversalDenied"

    def test_raises_absolute_path_denied_for_path_outside_workspace(self, workspace, tmp_path, no_tmp_bypass):
        """Raises ValueError with error_code='AbsolutePathDenied' for absolute paths outside workspace."""
        # Use a different temp directory as the "outside" path
        outside_dir = tmp_path.parent
        validator = _PathValidator(str(workspace))
        # Find an absolute path that is definitely outside the workspace
        outside_path = "/tmp/outside_file.txt"
        # Make sure it's not inside workspace
        ws_str = str(workspace)
        if outside_path.startswith(ws_str):
            outside_path = "/etc/passwd"
        with pytest.raises(ValueError) as exc_info:
            validator.validate(outside_path)
        assert exc_info.value.error_code == "AbsolutePathDenied"

    def test_workspace_root_itself_is_valid(self, workspace):
        """The workspace root path itself is a valid path."""
        validator = _PathValidator(str(workspace))
        result = validator.validate(".")
        assert result == os.path.realpath(str(workspace))




class TestFileJournalManager:
    def test_turn_key_parses_iso_variants(self):
        assert _journal_turn_key("2026-05-11T10:20:30")[0] == "260511_102030"
        assert _journal_turn_key("2026-05-11T10:20:30.123")[0] == "260511_102030"
        assert _journal_turn_key("2026-05-11T10:20:30Z")[0] == "260511_102030"
        assert _journal_turn_key("2026-05-11T18:20:30+08:00")[0] == "260511_102030"

    def test_flattened_path_includes_hash_and_safe_chars(self):
        filename = _flatten_journal_path("src/foo bar.py", "baseline")
        assert filename.startswith("src-foo_bar.py.")
        assert filename.endswith(".baseline.gz")
        assert re.match(r"^[A-Za-z0-9._-]+$", filename)

    def test_clean_tracked_baseline_uses_git_and_after_uses_sidecar(self, workspace, journal_context):
        tracked = workspace / "tracked.txt"
        tracked.write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=workspace, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add tracked"], cwd=workspace, check=True, capture_output=True)

        manager = _FileJournalManager(str(workspace), "session1", "2026-05-11T10:20:30", str(journal_context))
        baseline = manager.ensure_baseline("edit_file", str(tracked))
        assert "error" not in baseline
        tracked.write_text("after\n", encoding="utf-8")
        after = manager.record_after("edit_file", str(tracked))
        assert "error" not in after

        manifest_path = journal_context / "file_journals" / "260511_102030" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = manifest["files"]["tracked.txt"]
        assert entry["baseline"]["store"] == "git"
        assert entry["after"]["store"] == "sidecar"

    def test_dirty_baseline_uses_sidecar(self, workspace, journal_context):
        dirty = workspace / "dirty.txt"
        dirty.write_text("dirty before\n", encoding="utf-8")
        manager = _FileJournalManager(str(workspace), "session1", "2026-05-11T10:20:30", str(journal_context))
        result = manager.ensure_baseline("edit_file", str(dirty))
        assert "error" not in result
        manifest_path = journal_context / "file_journals" / "260511_102030" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["files"]["dirty.txt"]["baseline"]["store"] == "sidecar"


# ---------------------------------------------------------------------------
# Task 4.3 — Unit tests for _read_file
# ---------------------------------------------------------------------------

import json as _json
from runtime.builtin_tools import _read_file


class TestReadFileUnit:
    """Unit tests for _read_file()."""

    def test_file_not_found_returns_error(self, workspace):
        """Returns FileNotFound error when the file does not exist."""
        result = _json.loads(_read_file("nonexistent_file.txt"))
        assert result["error"] == "FileNotFound"
        assert "message" in result

    def test_start_line_greater_than_end_line_returns_invalid_range(self, workspace):
        """Returns InvalidRange error when start_line > end_line."""
        # Create a file with some lines
        test_file = workspace / "test.txt"
        test_file.write_text("line1\nline2\nline3\nline4\nline5\n")
        result = _json.loads(_read_file("test.txt", start_line=5, end_line=2))
        assert result["error"] == "InvalidRange"
        assert "message" in result

    def test_start_line_out_of_bounds_returns_line_out_of_range(self, workspace):
        """Returns LineOutOfRange error when start_line exceeds total lines."""
        test_file = workspace / "test.txt"
        test_file.write_text("line1\nline2\nline3\n")
        result = _json.loads(_read_file("test.txt", start_line=10))
        assert result["error"] == "LineOutOfRange"
        assert "message" in result

    def test_end_line_out_of_bounds_clamps_to_file_end(self, workspace):
        """end_line larger than total lines is clamped to total_lines."""
        test_file = workspace / "test.txt"
        test_file.write_text("line1\nline2\nline3\n")
        result = _json.loads(_read_file("test.txt", end_line=100))
        assert "error" not in result
        assert result["total_lines"] == 3
        assert "1: line1" in result["content"]
        assert "2: line2" in result["content"]
        assert "3: line3" in result["content"]

    def test_start_line_zero_returns_line_out_of_range(self, workspace):
        """Returns LineOutOfRange error when start_line is 0 (below 1)."""
        test_file = workspace / "test.txt"
        test_file.write_text("line1\nline2\n")
        result = _json.loads(_read_file("test.txt", start_line=0))
        assert result["error"] == "LineOutOfRange"
        assert "message" in result

    def test_path_traversal_returns_error_when_strict_path_check_enabled(self, workspace, monkeypatch, no_tmp_bypass):
        """With strict path validation enabled, ../ traversal is denied."""
        monkeypatch.setenv("CHECK_PATH_FOR_READ", "true")
        result = _json.loads(_read_file("../escape.txt"))
        assert result["error"] == "PathTraversalDenied"

    def test_successful_read_returns_line_numbered_content(self, workspace):
        """Successful read returns content with line numbers."""
        test_file = workspace / "hello.txt"
        test_file.write_text("alpha\nbeta\ngamma\n")
        result = _json.loads(_read_file("hello.txt"))
        assert result["truncated"] is False
        assert result["total_lines"] == 3
        assert "1: alpha" in result["content"]
        assert "2: beta" in result["content"]
        assert "3: gamma" in result["content"]

    def test_range_read_returns_only_requested_lines(self, workspace):
        """Range read returns only the requested lines."""
        test_file = workspace / "range.txt"
        test_file.write_text("a\nb\nc\nd\ne\n")
        result = _json.loads(_read_file("range.txt", start_line=2, end_line=4))
        assert result["total_lines"] == 5
        assert "2: b" in result["content"]
        assert "3: c" in result["content"]
        assert "4: d" in result["content"]
        assert "1: a" not in result["content"]
        assert "5: e" not in result["content"]


# ---------------------------------------------------------------------------
# Task 4.4 — Property test P3: read_file returns correct line-numbered output
# ---------------------------------------------------------------------------

from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st


# Feature: builtin-tools, Property 3: read_file returns correct line-numbered output
class TestReadFilePropertyP3:
    """**Validates: Requirements 3.1**"""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        lines=st.lists(
            st.text(
                alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\n\r\x00"),
                min_size=0,
                max_size=80,
            ),
            min_size=1,
            max_size=50,
        )
    )
    def test_read_file_line_numbered_output(self, workspace, lines):
        """Each line in the output has the correct 'N: content' format."""
        # Write the file
        content = "\n".join(lines) + "\n"
        test_file = workspace / "prop3_test.txt"
        test_file.write_text(content, encoding="utf-8")

        result = _json.loads(_read_file("prop3_test.txt"))

        assert "error" not in result, f"Unexpected error: {result}"
        assert result["total_lines"] == len(lines)

        # Parse the returned content and verify each line has correct format
        returned_content = result["content"]
        # Use split("\n") instead of splitlines() to avoid splitting on control chars
        # The content ends with "\n" so we strip the trailing empty element
        returned_lines = returned_content.split("\n")
        if returned_lines and returned_lines[-1] == "":
            returned_lines = returned_lines[:-1]

        # When not truncated, all lines should be present
        if not result["truncated"]:
            assert len(returned_lines) == len(lines)
            for i, (returned_line, original_line) in enumerate(zip(returned_lines, lines), start=1):
                expected_prefix = f"{i}: "
                assert returned_line.startswith(expected_prefix), (
                    f"Line {i} does not start with '{expected_prefix}': {returned_line!r}"
                )
                actual_content = returned_line[len(expected_prefix):]
                assert actual_content == original_line, (
                    f"Line {i} content mismatch: expected {original_line!r}, got {actual_content!r}"
                )


# ---------------------------------------------------------------------------
# Task 4.5 — Property test P4: range slice returns exactly the requested lines
# ---------------------------------------------------------------------------

# Feature: builtin-tools, Property 4: range slice returns exactly the requested lines
class TestReadFilePropertyP4:
    """**Validates: Requirements 3.2, 3.3, 3.4**"""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        lines=st.lists(
            st.text(
                alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\n\r\x00"),
                min_size=1,
                max_size=60,
            ),
            min_size=2,
            max_size=30,
        ),
        start_offset=st.integers(min_value=0),
        end_offset=st.integers(min_value=0),
    )
    def test_range_slice_returns_exactly_requested_lines(self, workspace, lines, start_offset, end_offset):
        """Range slice returns exactly the lines in [start_line, end_line]."""
        n = len(lines)
        # Derive valid start/end within [1, n]
        start_line = (start_offset % n) + 1
        end_line = (end_offset % n) + 1
        if start_line > end_line:
            start_line, end_line = end_line, start_line

        content = "\n".join(lines) + "\n"
        test_file = workspace / "prop4_test.txt"
        test_file.write_text(content, encoding="utf-8")

        result = _json.loads(_read_file("prop4_test.txt", start_line=start_line, end_line=end_line))

        assert "error" not in result, f"Unexpected error: {result}"
        assert result["total_lines"] == n

        returned_content = result["content"]
        # Use split("\n") instead of splitlines() to avoid splitting on control chars
        returned_lines = returned_content.split("\n")
        if returned_lines and returned_lines[-1] == "":
            returned_lines = returned_lines[:-1]

        expected_lines = lines[start_line - 1:end_line]
        assert len(returned_lines) == len(expected_lines), (
            f"Expected {len(expected_lines)} lines, got {len(returned_lines)}"
        )

        for i, (returned_line, original_line) in enumerate(zip(returned_lines, expected_lines)):
            line_num = start_line + i
            expected_prefix = f"{line_num}: "
            assert returned_line.startswith(expected_prefix), (
                f"Line {line_num} does not start with '{expected_prefix}': {returned_line!r}"
            )
            actual_content = returned_line[len(expected_prefix):]
            assert actual_content == original_line


# ---------------------------------------------------------------------------
# Task 4.6 — Property test P5: truncation when file exceeds threshold
# ---------------------------------------------------------------------------

# Feature: builtin-tools, Property 5: truncation when file exceeds threshold
class TestReadFilePropertyP5:
    """**Validates: Requirements 3.5**"""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        threshold=st.integers(min_value=2, max_value=20),
        extra=st.integers(min_value=1, max_value=10),
        line_content=st.text(
            alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\n\r\x00"),
            min_size=1,
            max_size=40,
        ),
    )
    def test_truncation_when_file_exceeds_threshold(self, workspace, monkeypatch, threshold, extra, line_content):
        """When file has more lines than threshold and no range is given, truncated=true and omitted_lines is correct."""
        total = threshold + extra
        lines = [f"{line_content}_{i}" for i in range(total)]
        content = "\n".join(lines) + "\n"

        test_file = workspace / "prop5_test.txt"
        test_file.write_text(content, encoding="utf-8")

        # Set READ_TRUNCATION_LINES to the threshold value
        monkeypatch.setenv("READ_TRUNCATION_LINES", str(threshold))

        result = _json.loads(_read_file("prop5_test.txt"))

        assert "error" not in result, f"Unexpected error: {result}"
        assert result["total_lines"] == total
        assert result["truncated"] is True
        assert result["omitted_lines"] == extra

        # Verify only threshold lines are returned
        returned_lines = result["content"].split("\n")
        if returned_lines and returned_lines[-1] == "":
            returned_lines = returned_lines[:-1]
        assert len(returned_lines) == threshold


# ---------------------------------------------------------------------------
# Task 5.3 — Unit tests for _write_file
# ---------------------------------------------------------------------------

from runtime.builtin_tools import _write_file


class TestWriteFileUnit:
    """Unit tests for _write_file()."""

    def test_parent_directory_auto_created(self, workspace):
        """Parent directory that doesn't exist is automatically created."""
        result = _json.loads(_write_file("new_dir/subdir/file.txt", "hello"))
        assert "error" not in result, f"Unexpected error: {result}"
        assert (workspace / "new_dir" / "subdir" / "file.txt").exists()

    def test_successful_write_returns_correct_bytes_written(self, workspace):
        """Successful write returns correct bytes_written for UTF-8 content."""
        content = "Hello, World!\n"
        expected_bytes = len(content.encode("utf-8"))
        result = _json.loads(_write_file("output.txt", content))
        assert "error" not in result, f"Unexpected error: {result}"
        assert result["bytes_written"] == expected_bytes

    def test_successful_write_returns_journal_metadata(self, workspace, journal_context):
        """Successful write returns journal metadata."""
        result = _json.loads(_write_file("output2.txt", "some content"))
        assert "error" not in result, f"Unexpected error: {result}"
        assert "commit_id" not in result
        assert result["journal"]["turn_key"] == "260511_102030"
        assert result["journal_id"] == "session1/260511_102030"

    def test_file_content_is_correct_after_writing(self, workspace):
        """The file content on disk matches what was written."""
        content = "line1\nline2\nline3\n"
        _write_file("verify.txt", content)
        actual = (workspace / "verify.txt").read_text(encoding="utf-8")
        assert actual == content

    def test_path_traversal_returns_error(self, workspace, no_tmp_bypass):
        """Returns PathTraversalDenied error for path traversal attempts."""
        result = _json.loads(_write_file("../escape.txt", "bad"))
        assert result["error"] == "PathTraversalDenied"

    def test_response_file_field_is_relative_path(self, workspace):
        """The 'file' field in the response is a relative path from workspace root."""
        result = _json.loads(_write_file("subdir/myfile.txt", "content"))
        assert "error" not in result, f"Unexpected error: {result}"
        # Should be a relative path, not absolute
        assert not os.path.isabs(result["file"])
        assert result["file"] == os.path.join("subdir", "myfile.txt")

    def test_unicode_content_bytes_written_is_utf8_length(self, workspace):
        """bytes_written reflects UTF-8 byte count, not character count."""
        content = "こんにちは"  # 5 chars, 15 bytes in UTF-8
        result = _json.loads(_write_file("unicode.txt", content))
        assert "error" not in result, f"Unexpected error: {result}"
        assert result["bytes_written"] == len(content.encode("utf-8"))


# ---------------------------------------------------------------------------
# Task 5.4 — Property test P6: write then read back content is consistent
# ---------------------------------------------------------------------------

# Feature: builtin-tools, Property 6: write then read back content is consistent
class TestWriteFilePropertyP6:
    """**Validates: Requirements 4.1, 4.2**"""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
    @given(
        rel_path=st.from_regex(r"[a-zA-Z0-9_][a-zA-Z0-9_\-]{0,10}\.[a-z]{1,4}", fullmatch=True),
        content=st.text(
            # Exclude \x00 (null bytes) and \r (carriage return) since _read_file
            # opens files in text mode which normalizes \r to \n on all platforms.
            alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00\r"),
            min_size=0,
            max_size=500,
        ),
    )
    def test_write_then_read_content_is_consistent(self, workspace, rel_path, content):
        """Writing content and reading it back returns the same content."""
        write_result = _json.loads(_write_file(rel_path, content))
        assert "error" not in write_result, f"Write failed: {write_result}"

        # Read back using _read_file — it returns line-numbered content,
        # so we reconstruct the original content from the numbered lines.
        read_result = _json.loads(_read_file(rel_path))
        assert "error" not in read_result, f"Read failed: {read_result}"

        # Reconstruct original content from line-numbered output
        # Each line is formatted as "{n}: {original_line}"
        # We need to strip the prefix and rejoin
        returned_content_raw = read_result["content"]

        # Split on newlines, strip line-number prefixes, rejoin
        numbered_lines = returned_content_raw.split("\n")
        # The last element may be empty if content ends with \n
        reconstructed_lines = []
        for numbered_line in numbered_lines:
            if not numbered_line:
                continue
            # Strip "N: " prefix
            colon_space = numbered_line.index(": ")
            reconstructed_lines.append(numbered_line[colon_space + 2:])

        # Reconstruct: join lines with \n
        # If original content ended with \n, the last reconstructed line is empty
        # and we need to add a trailing \n
        if content == "":
            # Empty content: file has 0 lines, read_file returns empty content
            assert returned_content_raw == ""
        else:
            # Non-empty: reconstruct and compare
            # The file was written as UTF-8; read_file reads it back line by line
            # We verify bytes_written matches
            assert write_result["bytes_written"] == len(content.encode("utf-8"))

            # Verify the actual file content on disk matches what was written
            actual_on_disk = (workspace / rel_path).read_text(encoding="utf-8")
            assert actual_on_disk == content


# ---------------------------------------------------------------------------
# Task 7.4 — Unit tests for _edit_file
# ---------------------------------------------------------------------------

from runtime.builtin_tools import _edit_file


class TestEditFileUnit:
    """Unit tests for _edit_file()."""

    def test_old_str_not_found_returns_line_not_found(self, workspace):
        """Returns LineNotFound error when old_str is not in the file."""
        test_file = workspace / "edit_test.py"
        test_file.write_text("def foo():\n    return 1\n")
        result = _json.loads(_edit_file("edit_test.py", "search_replace",
                                        old_str="def bar():\n    return 2\n",
                                        new_str="def bar():\n    return 99\n"))
        assert result["error"] == "LineNotFound"
        assert "message" in result

    def test_patch_does_not_apply_returns_patch_failed(self, workspace):
        """Returns PatchFailed error when the patch does not apply cleanly."""
        test_file = workspace / "patch_test.py"
        test_file.write_text("def foo():\n    return 1\n")
        # A patch that references lines that don't exist
        bad_patch = (
            "--- a/patch_test.py\n"
            "+++ b/patch_test.py\n"
            "@@ -10,3 +10,3 @@\n"
            "-def nonexistent():\n"
            "-    return 0\n"
            "+def nonexistent():\n"
            "+    return 99\n"
        )
        result = _json.loads(_edit_file("patch_test.py", "diff", patch=bad_patch))
        assert result["error"] == "PatchFailed"
        assert "message" in result

    def test_diff_mode_applies_patch_with_plain_file_header(self, workspace):
        """diff mode accepts unified diffs without a/ and b/ path prefixes."""
        test_file = workspace / "plain_header.txt"
        test_file.write_text("a\nb\nc\n", encoding="utf-8")
        patch = (
            "--- plain_header.txt\n"
            "+++ plain_header.txt\n"
            "@@ -1,3 +1,3 @@\n"
            " a\n"
            "-b\n"
            "+B\n"
            " c\n"
        )

        result = _json.loads(_edit_file("plain_header.txt", "diff", patch=patch))

        assert "error" not in result, f"Unexpected error: {result}"
        assert test_file.read_text(encoding="utf-8") == "a\nB\nc\n"

    def test_diff_mode_applies_hunk_only_patch_to_target_path(self, workspace):
        """diff mode accepts a hunk-only patch and applies it to the path argument."""
        test_file = workspace / "hunk_only.txt"
        test_file.write_text("a\nb\nc\n", encoding="utf-8")
        patch = (
            "@@ -1,3 +1,3 @@\n"
            " a\n"
            "-b\n"
            "+B\n"
            " c\n"
        )

        result = _json.loads(_edit_file("hunk_only.txt", "diff", patch=patch))

        assert "error" not in result, f"Unexpected error: {result}"
        assert test_file.read_text(encoding="utf-8") == "a\nB\nc\n"

    def test_diff_mode_rewrites_bad_hunk_counts(self, workspace):
        """diff mode fixes stale unified diff hunk counts before invoking patch."""
        test_file = workspace / "bad_counts.txt"
        test_file.write_text("a\nb\nc\n", encoding="utf-8")
        patch = (
            "--- a/bad_counts.txt\n"
            "+++ b/bad_counts.txt\n"
            "@@ -1,99 +1,99 @@\n"
            " a\n"
            "-b\n"
            "+B\n"
            " c\n"
        )

        result = _json.loads(_edit_file("bad_counts.txt", "diff", patch=patch))

        assert "error" not in result, f"Unexpected error: {result}"
        assert test_file.read_text(encoding="utf-8") == "a\nB\nc\n"

    def test_diff_mode_applies_begin_patch_with_bare_anchor_sections(self, workspace):
        """Begin Patch bare @@ anchor sections are located but not emitted as hunks."""
        test_file = workspace / "begin_patch.py"
        test_file.write_text("def f():\n    value = 1\n    return value\n", encoding="utf-8")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: begin_patch.py\n"
            "@@\n"
            "def f():\n"
            "@@\n"
            "    return value\n"
            "+\n"
            "+def g():\n"
            "+    return 2\n"
            "*** End Patch\n"
        )

        result = _json.loads(_edit_file("begin_patch.py", "diff", patch=patch))

        assert "error" not in result, f"Unexpected error: {result}"
        assert test_file.read_text(encoding="utf-8") == (
            "def f():\n"
            "    value = 1\n"
            "    return value\n"
            "\n"
            "def g():\n"
            "    return 2\n"
        )

    def test_diff_mode_returns_patch_diagnostics(self, workspace):
        """PatchFailed includes patch output so callers can diagnose failures."""
        test_file = workspace / "diagnostic.txt"
        test_file.write_text("a\nb\nc\n", encoding="utf-8")
        patch = (
            "--- diagnostic.txt\n"
            "+++ diagnostic.txt\n"
            "@@ -1,3 +1,3 @@\n"
            " a\n"
            "-missing\n"
            "+B\n"
            " c\n"
        )

        result = _json.loads(_edit_file("diagnostic.txt", "diff", patch=patch))

        assert result["error"] == "PatchFailed"
        assert result["message"] != "Patch did not apply cleanly"
        assert test_file.read_text(encoding="utf-8") == "a\nb\nc\n"
        assert not (workspace / "diagnostic.txt.rej").exists()

    def test_search_replace_success(self, workspace, journal_context):
        """Successful search_replace returns file, journal metadata, and lines_changed."""
        test_file = workspace / "success_edit.py"
        test_file.write_text("x = 1\ny = 2\nz = 3\n")
        result = _json.loads(_edit_file("success_edit.py", "search_replace",
                                        old_str="y = 2",
                                        new_str="y = 99"))
        assert "error" not in result, f"Unexpected error: {result}"
        assert result["file"] == "success_edit.py"
        assert "commit_id" not in result
        assert result["journal"]["turn_key"] == "260511_102030"
        assert "lines_added" in result
        assert "lines_removed" in result
        assert "file_modified" in result

    def test_search_replace_modifies_file_content(self, workspace):
        """search_replace actually modifies the file on disk."""
        test_file = workspace / "modify_test.py"
        test_file.write_text("a = 1\nb = 2\nc = 3\n")
        _edit_file("modify_test.py", "search_replace",
                   old_str="b = 2",
                   new_str="b = 42")
        content = test_file.read_text(encoding="utf-8")
        assert "b = 42" in content
        assert "b = 2" not in content

    def test_search_replace_replaces_only_first_occurrence(self, workspace):
        """search_replace replaces only the first occurrence of old_str."""
        test_file = workspace / "first_only.py"
        test_file.write_text("x = 1\nx = 1\nx = 1\n")
        _edit_file("first_only.py", "search_replace",
                   old_str="x = 1",
                   new_str="x = 99")
        content = test_file.read_text(encoding="utf-8")
        assert content.count("x = 99") == 1
        assert content.count("x = 1") == 2

    def test_path_traversal_returns_error(self, workspace, no_tmp_bypass):
        """Returns PathTraversalDenied error for path traversal attempts."""
        result = _json.loads(_edit_file("../escape.py", "search_replace",
                                        old_str="x", new_str="y"))
        assert result["error"] == "PathTraversalDenied"

    def test_file_not_found_returns_error(self, workspace):
        """Returns FileNotFound error when the file does not exist."""
        result = _json.loads(_edit_file("nonexistent.py", "search_replace",
                                        old_str="x", new_str="y"))
        assert result["error"] == "FileNotFound"


# ---------------------------------------------------------------------------
# Task 7.5 — Property test P7: search_replace correctly transforms file content
# ---------------------------------------------------------------------------

# Feature: builtin-tools, Property 7: search_replace correctly transforms file content
class TestEditFilePropertyP7:
    """**Validates: Requirements 5.1**"""

    # Use a safe printable ASCII alphabet to avoid Unicode line-separator characters
    # (e.g. \x85 NEL, \x1e RS, \x0c FF) that Python's str.strip() or readlines()
    # would treat as whitespace or line separators, causing unexpected mismatches.
    _SAFE_ALPHA = st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters="!#$%&*+,-./:;<=>?@[]^_`{|}~",
    )

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
    @given(
        # Use a unique prefix/suffix so target won't appear in them by construction
        unique_id=st.integers(min_value=1000, max_value=9999),
        target=st.text(alphabet=_SAFE_ALPHA, min_size=1, max_size=30),
        replacement=st.text(alphabet=_SAFE_ALPHA, min_size=1, max_size=30),
    )
    def test_search_replace_transforms_content(self, workspace, unique_id, target, replacement):
        """search_replace correctly replaces the target text with the replacement."""
        # Build content where target appears exactly once, surrounded by unique lines
        # that cannot contain target (they use a numeric prefix that target won't have)
        prefix_line = f"PREFIX_{unique_id}_START"
        suffix_line = f"SUFFIX_{unique_id}_END"
        # Ensure target doesn't accidentally appear in prefix/suffix lines
        assume(target not in prefix_line)
        assume(target not in suffix_line)

        content = f"{prefix_line}\n{target}\n{suffix_line}\n"
        test_file = workspace / "prop7_test.txt"
        test_file.write_text(content, encoding="utf-8")

        result = _json.loads(_edit_file("prop7_test.txt", "search_replace",
                                        old_str=target,
                                        new_str=replacement))

        assert "error" not in result, f"Unexpected error: {result}"

        # Verify the file was modified correctly: the target line is gone,
        # replaced by the replacement line
        new_content = test_file.read_text(encoding="utf-8")
        new_lines = new_content.splitlines()

        # The prefix and suffix lines should still be present
        assert prefix_line in new_lines, f"Prefix line missing from result"
        assert suffix_line in new_lines, f"Suffix line missing from result"

        # The replacement should be present
        assert replacement in new_lines, (
            f"Replacement {replacement!r} not found in file lines: {new_lines}"
        )

        # The original target line should be gone (unless replacement == target)
        if replacement != target:
            # Count occurrences: target should not appear as a standalone line
            # (it may appear as a substring of replacement, which is fine)
            assert target not in new_lines, (
                f"Target {target!r} still present as a line after replacement"
            )


# ---------------------------------------------------------------------------
# Task 7.6 — Property test P8: whitespace-tolerant matching
# ---------------------------------------------------------------------------

# Feature: builtin-tools, Property 8: whitespace-tolerant matching
class TestEditFilePropertyP8:
    """**Validates: Requirements 5.2**"""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
    @given(
        line_content=st.text(
            # Use only printable ASCII to avoid characters that Python treats as
            # line separators (e.g. \x85 NEL, \x0c FF) which would cause readlines()
            # to split lines differently than expected.
            alphabet=st.characters(
                whitelist_categories=("Lu", "Ll", "Nd", "Po", "Sm"),
                whitelist_characters="_",
            ),
            min_size=1,
            max_size=40,
        ),
        file_indent=st.integers(min_value=0, max_value=8),
        search_indent=st.integers(min_value=0, max_value=8),
        replacement=st.text(
            alphabet=st.characters(
                whitelist_categories=("Lu", "Ll", "Nd", "Po", "Sm"),
                whitelist_characters="_ ",
            ),
            min_size=1,
            max_size=30,
        ),
    )
    def test_whitespace_tolerant_matching(self, workspace, line_content, file_indent, search_indent, replacement):
        """old_str with different leading/trailing whitespace still matches the file content."""
        # Write file with file_indent spaces before the line
        file_line = " " * file_indent + line_content
        content = f"before\n{file_line}\nafter\n"
        test_file = workspace / "prop8_test.txt"
        test_file.write_text(content, encoding="utf-8")

        # Search with search_indent spaces (different from file_indent)
        search_line = " " * search_indent + line_content

        result = _json.loads(_edit_file("prop8_test.txt", "search_replace",
                                        old_str=search_line,
                                        new_str=replacement))

        # The match should succeed because whitespace-tolerant matching strips leading/trailing spaces
        assert "error" not in result, (
            f"Expected match to succeed with file_indent={file_indent}, "
            f"search_indent={search_indent}, line={line_content!r}, "
            f"but got error: {result}"
        )

        # Verify the replacement was applied
        new_content = test_file.read_text(encoding="utf-8")
        assert replacement in new_content, (
            f"Replacement {replacement!r} not found in file after edit"
        )


# ---------------------------------------------------------------------------
# Task 7.7 — Property test P9: diff mode correctly applies patch
# ---------------------------------------------------------------------------

# Feature: builtin-tools, Property 9: diff mode correctly applies patch
class TestEditFilePropertyP9:
    """**Validates: Requirements 5.3**"""

    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
    @given(
        original_lines=st.lists(
            # Use only printable ASCII to avoid Unicode line-separator characters
            # (e.g. \x0c FF, \x85 NEL) that splitlines() treats as line breaks,
            # causing line-count mismatches between the patch and the file.
            st.text(
                alphabet=st.characters(
                    whitelist_categories=("Lu", "Ll", "Nd"),
                    whitelist_characters="!#$%&*+,-./:;<=>?@[]^_`{|}~ ",
                ),
                min_size=1,
                max_size=40,
            ),
            min_size=3,
            max_size=10,
        ),
        replace_idx=st.integers(min_value=0, max_value=2),
        new_line=st.text(
            alphabet=st.characters(
                whitelist_categories=("Lu", "Ll", "Nd"),
                whitelist_characters="!#$%&*+,-./:;<=>?@[]^_`{|}~ ",
            ),
            min_size=1,
            max_size=40,
        ),
    )
    def test_diff_mode_applies_patch(self, workspace, original_lines, replace_idx, new_line):
        """diff mode correctly applies a valid unified diff patch."""
        assume(len(original_lines) > replace_idx)
        assume(original_lines[replace_idx] != new_line)

        # Write the original file — use .txt extension to avoid linter running on
        # arbitrary content (which would cause LintFailed for non-Python syntax)
        content = "\n".join(original_lines) + "\n"
        test_file = workspace / "prop9_test.txt"
        test_file.write_text(content, encoding="utf-8")

        # Build a valid unified diff patch
        old_line = original_lines[replace_idx]
        # Context: use up to 3 lines around the change
        ctx_start = max(0, replace_idx - 1)
        ctx_end = min(len(original_lines), replace_idx + 2)
        context_before = original_lines[ctx_start:replace_idx]
        context_after = original_lines[replace_idx + 1:ctx_end]

        hunk_old_start = ctx_start + 1  # 1-indexed
        hunk_old_count = len(context_before) + 1 + len(context_after)
        hunk_new_count = len(context_before) + 1 + len(context_after)

        hunk_lines = []
        for l in context_before:
            hunk_lines.append(f" {l}")
        hunk_lines.append(f"-{old_line}")
        hunk_lines.append(f"+{new_line}")
        for l in context_after:
            hunk_lines.append(f" {l}")

        patch = (
            f"--- a/prop9_test.txt\n"
            f"+++ b/prop9_test.txt\n"
            f"@@ -{hunk_old_start},{hunk_old_count} +{hunk_old_start},{hunk_new_count} @@\n"
            + "\n".join(hunk_lines) + "\n"
        )

        result = _json.loads(_edit_file("prop9_test.txt", "diff", patch=patch))

        assert "error" not in result, f"Unexpected error: {result}\nPatch:\n{patch}"

        # Verify the file was modified correctly
        new_content = test_file.read_text(encoding="utf-8")
        new_file_lines = new_content.splitlines()
        assert new_file_lines[replace_idx] == new_line, (
            f"Expected line {replace_idx} to be {new_line!r}, got {new_file_lines[replace_idx]!r}"
        )


# ---------------------------------------------------------------------------
# Task 7.8 — Property test P10: lint failure causes file rollback
# ---------------------------------------------------------------------------

# Feature: builtin-tools, Property 10: lint failure causes file rollback
class TestEditFilePropertyP10:
    """**Validates: Requirements 5.4, 5.5**"""

    @settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
    @given(
        func_name=st.from_regex(r"[a-z][a-z0-9_]{0,10}", fullmatch=True),
        return_val=st.integers(min_value=0, max_value=999),
    )
    def test_lint_failure_causes_rollback(self, workspace, func_name, return_val):
        """When edit introduces invalid Python syntax, the file is rolled back to original."""
        # Write a valid Python file
        original_content = f"def {func_name}():\n    return {return_val}\n"
        test_file = workspace / "lint_test.py"
        test_file.write_text(original_content, encoding="utf-8")

        # Attempt to replace the function body with invalid Python syntax
        invalid_replacement = f"def {func_name}():\n    return (\n"  # unclosed parenthesis

        result = _json.loads(_edit_file("lint_test.py", "search_replace",
                                        old_str=f"def {func_name}():\n    return {return_val}",
                                        new_str=f"def {func_name}():\n    return ("))

        # The edit should fail with LintFailed
        assert result.get("error") == "LintFailed", (
            f"Expected LintFailed error, got: {result}"
        )

        # The file should be rolled back to the original content
        current_content = test_file.read_text(encoding="utf-8")
        assert current_content == original_content, (
            f"File was not rolled back. Expected:\n{original_content!r}\nGot:\n{current_content!r}"
        )


# ---------------------------------------------------------------------------
# Task 8.3 — Unit tests for _search_code
# ---------------------------------------------------------------------------

import shutil as _shutil
from unittest.mock import patch as _mock_patch
from runtime.builtin_tools import _search_code


class TestSearchCodeUnit:
    """Unit tests for _search_code()."""

    def test_no_matches_returns_empty_results(self, workspace):
        """When no files match the query, returns empty results array."""
        # Create a file that won't match the query
        test_file = workspace / "no_match.txt"
        test_file.write_text("hello world\nfoo bar\n")

        result = _json.loads(_search_code("ZZZNOMATCHZZZ"))
        assert "error" not in result, f"Unexpected error: {result}"
        assert result["results"] == []
        assert result["truncated"] is False
        assert result["total_found"] == 0

    def test_rg_not_available_falls_back_to_grep(self, workspace, monkeypatch):
        """When rg is not available, falls back to grep and includes 'fallback': 'grep'."""
        # Create a file with known content
        test_file = workspace / "fallback_test.txt"
        test_file.write_text("hello_unique_pattern_xyz\n")

        # Mock shutil.which to return None for rg but a path for grep
        original_which = _shutil.which

        def mock_which(name):
            if name == "rg":
                return None
            return original_which(name)

        with _mock_patch("runtime.builtin_tools.shutil.which", side_effect=mock_which):
            result = _json.loads(_search_code("hello_unique_pattern_xyz"))

        assert "error" not in result, f"Unexpected error: {result}"
        assert result.get("fallback") == "grep"
        assert result["total_found"] >= 1

    def test_grep_fallback_supports_path_aware_include_globs(self, workspace):
        """grep fallback must not pass path globs like **/*.py directly to --include."""
        nested = workspace / "runtime"
        nested.mkdir()
        target = nested / "fallback_path_glob.py"
        other = workspace / "fallback_path_glob.txt"
        target.write_text("path_glob_unique_pattern\n")
        other.write_text("path_glob_unique_pattern\n")

        original_which = _shutil.which

        def mock_which(name):
            if name == "rg":
                return None
            return original_which(name)

        with _mock_patch("runtime.builtin_tools.shutil.which", side_effect=mock_which):
            result = _json.loads(_search_code("path_glob_unique_pattern", include="runtime/*.py"))

        assert "error" not in result, f"Unexpected error: {result}"
        assert result.get("fallback") == "grep"
        assert result["total_found"] == 1
        assert result["results"][0]["file"] == "runtime/fallback_path_glob.py"

    def test_both_unavailable_returns_search_tool_not_found(self, workspace):
        """When both rg and grep are unavailable, returns SearchToolNotFound error."""
        def mock_which(name):
            return None

        with _mock_patch("runtime.builtin_tools.shutil.which", side_effect=mock_which):
            result = _json.loads(_search_code("anything"))

        assert result["error"] == "SearchToolNotFound"
        assert "message" in result

    def test_invalid_regex_returns_invalid_query(self, workspace):
        """When query is an invalid regex, returns InvalidQuery error."""
        result = _json.loads(_search_code("[invalid(regex"))
        assert result["error"] == "InvalidQuery"
        assert "message" in result

    def test_valid_search_returns_required_fields(self, workspace):
        """Successful search returns results with file, line, column, context fields."""
        test_file = workspace / "fields_test.txt"
        test_file.write_text("find_this_pattern\n")

        result = _json.loads(_search_code("find_this_pattern"))
        assert "error" not in result, f"Unexpected error: {result}"
        if result["total_found"] > 0:
            r = result["results"][0]
            assert "file" in r
            assert "line" in r
            assert "column" in r
            assert "context" in r


# ---------------------------------------------------------------------------
# Task 8.4 — Property test P11: all results match query and contain required fields
# ---------------------------------------------------------------------------

# Feature: builtin-tools, Property 11: all results match query and contain required fields
class TestSearchCodePropertyP11:
    """**Validates: Requirements 6.1**"""

    @settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
    @given(
        pattern=st.from_regex(r"[a-z]{4,8}_[a-z]{4,8}", fullmatch=True),
        num_files=st.integers(min_value=1, max_value=5),
    )
    def test_all_results_match_query_and_have_required_fields(self, workspace, pattern, num_files):
        """All returned results match the query pattern and contain file/line/column/context fields."""
        # Create files with known content containing the pattern
        for i in range(num_files):
            f = workspace / f"p11_test_{i}.txt"
            f.write_text(f"line before\n{pattern}\nline after\n")

        result = _json.loads(_search_code(pattern))
        assert "error" not in result, f"Unexpected error: {result}"

        compiled = re.compile(pattern)
        for r in result["results"]:
            # Each result must have required fields
            assert "file" in r, f"Missing 'file' field in result: {r}"
            assert "line" in r, f"Missing 'line' field in result: {r}"
            assert "column" in r, f"Missing 'column' field in result: {r}"
            assert "context" in r, f"Missing 'context' field in result: {r}"
            # The context must match the query pattern
            assert compiled.search(r["context"]) is not None, (
                f"Result context {r['context']!r} does not match pattern {pattern!r}"
            )


# ---------------------------------------------------------------------------
# Task 8.5 — Property test P12: excluded directories have no results
# ---------------------------------------------------------------------------

# Feature: builtin-tools, Property 12: excluded directories have no results
class TestSearchCodePropertyP12:
    """**Validates: Requirements 6.2**"""

    @settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
    @given(
        excluded_dir=st.sampled_from(["node_modules", ".git", "dist"]),
        pattern=st.from_regex(r"excluded_[a-z]{4,8}_content", fullmatch=True),
    )
    def test_excluded_directories_have_no_results(self, workspace, excluded_dir, pattern):
        """Files in default excluded directories (.git, node_modules, dist) are not returned."""
        # Create a file in the excluded directory with the pattern
        excl_dir = workspace / excluded_dir
        excl_dir.mkdir(exist_ok=True)
        excl_file = excl_dir / "excluded_file.txt"
        excl_file.write_text(f"{pattern}\n")

        # Also create a file outside the excluded dir to ensure search works
        normal_file = workspace / "normal_file.txt"
        normal_file.write_text("normal content without the pattern\n")

        result = _json.loads(_search_code(pattern))
        assert "error" not in result, f"Unexpected error: {result}"

        # No results should come from the excluded directory
        for r in result["results"]:
            file_path = r["file"]
            # Normalize path separators
            file_path_normalized = file_path.replace("\\", "/")
            assert not file_path_normalized.startswith(f"{excluded_dir}/"), (
                f"Result from excluded directory {excluded_dir!r}: {file_path!r}"
            )
            assert excluded_dir not in file_path_normalized.split("/"), (
                f"Result path contains excluded directory {excluded_dir!r}: {file_path!r}"
            )


# ---------------------------------------------------------------------------
# Task 8.6 — Property test P13: include/exclude filtering works
# ---------------------------------------------------------------------------

# Feature: builtin-tools, Property 13: include/exclude filtering works
class TestSearchCodePropertyP13:
    """**Validates: Requirements 6.3, 6.4**"""

    @settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
    @given(
        pattern=st.from_regex(r"filter_[a-z]{4,8}_test", fullmatch=True),
    )
    def test_include_exclude_filtering_works(self, workspace, pattern):
        """Include/exclude glob patterns correctly filter which files are searched."""
        # Create files with different extensions, all containing the pattern
        py_file = workspace / "filter_test.py"
        txt_file = workspace / "filter_test.txt"
        js_file = workspace / "filter_test.js"

        py_file.write_text(f"{pattern}\n")
        txt_file.write_text(f"{pattern}\n")
        js_file.write_text(f"{pattern}\n")

        # Search with include=*.py — should only return .py files
        result_py = _json.loads(_search_code(pattern, include="*.py"))
        assert "error" not in result_py, f"Unexpected error: {result_py}"
        for r in result_py["results"]:
            assert r["file"].endswith(".py"), (
                f"Non-.py file returned with include=*.py: {r['file']!r}"
            )

        # Search with exclude=*.py — should not return .py files
        result_no_py = _json.loads(_search_code(pattern, exclude="*.py"))
        assert "error" not in result_no_py, f"Unexpected error: {result_no_py}"
        for r in result_no_py["results"]:
            assert not r["file"].endswith(".py"), (
                f".py file returned with exclude=*.py: {r['file']!r}"
            )


# ---------------------------------------------------------------------------
# Task 8.7 — Property test P14: truncation when results exceed MAX_RESULTS
# ---------------------------------------------------------------------------

# Feature: builtin-tools, Property 14: truncation when results exceed MAX_RESULTS
class TestSearchCodePropertyP14:
    """**Validates: Requirements 6.5**"""

    @settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
    @given(
        max_results=st.integers(min_value=2, max_value=5),
        extra=st.integers(min_value=1, max_value=5),
        pattern=st.from_regex(r"truncate_[a-z]{4,8}_match", fullmatch=True),
    )
    def test_truncation_when_results_exceed_max_results(self, workspace, monkeypatch, max_results, extra, pattern):
        """When total matches exceed MAX_RESULTS, truncated=true and only MAX_RESULTS results returned."""
        total_files = max_results + extra

        # Create files each containing the pattern (one match per file)
        for i in range(total_files):
            f = workspace / f"p14_test_{i}.txt"
            f.write_text(f"{pattern}\n")

        # Set SEARCH_MAX_RESULTS to max_results
        monkeypatch.setenv("SEARCH_MAX_RESULTS", str(max_results))

        result = _json.loads(_search_code(pattern))
        assert "error" not in result, f"Unexpected error: {result}"

        assert result["truncated"] is True, (
            f"Expected truncated=true with {total_files} files and max_results={max_results}, "
            f"got total_found={result['total_found']}"
        )
        assert len(result["results"]) == max_results, (
            f"Expected {max_results} results, got {len(result['results'])}"
        )
        assert result["total_found"] >= total_files


# ---------------------------------------------------------------------------
# Task 10.3 — Unit tests for _exec_shell
# ---------------------------------------------------------------------------

from runtime.builtin_tools import _exec_shell


class TestExecuteCommandUnit:
    """Unit tests for _exec_shell()."""

    def test_empty_command_returns_empty_command_error(self, workspace):
        """Empty command string returns EmptyCommand error."""
        result = _json.loads(_exec_shell(""))
        assert result["error"] == "EmptyCommand"
        assert "message" in result

    def test_whitespace_only_command_returns_empty_command_error(self, workspace):
        """Whitespace-only command string returns EmptyCommand error."""
        result = _json.loads(_exec_shell("   "))
        assert result["error"] == "EmptyCommand"
        assert "message" in result

    def test_exit_1_returns_exit_code_1_without_error_key(self, workspace):
        """Command 'exit 1' returns exit_code=1 with no 'error' key in response."""
        result = _json.loads(_exec_shell("exit 1"))
        assert "error" not in result, f"Unexpected error key: {result}"
        assert result["exit_code"] == 1

    def test_echo_term_outputs_dumb(self, workspace):
        """Command 'echo $TERM' outputs 'dumb' (TERM env var is set to dumb)."""
        result = _json.loads(_exec_shell("echo $TERM"))
        assert "error" not in result, f"Unexpected error: {result}"
        assert "dumb" in result["stdout"]


# ---------------------------------------------------------------------------
# Task 10.4 — Property test P15: cwd is workspace and response has required fields
# ---------------------------------------------------------------------------

# Feature: builtin-tools, Property 15: cwd is workspace and response has required fields
class TestExecuteCommandPropertyP15:
    """**Validates: Requirements 7.1, 7.6**"""

    @settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
    @given(st.just(None))  # single-shot property, no interesting variation needed
    def test_cwd_is_workspace_and_response_has_required_fields(self, workspace, _):
        """Running 'pwd' returns the workspace path and response has exit_code/stdout/stderr/truncated."""
        result = _json.loads(_exec_shell("pwd"))

        assert "error" not in result, f"Unexpected error: {result}"

        # Required fields must be present
        assert "exit_code" in result, "Missing 'exit_code' field"
        assert "stdout" in result, "Missing 'stdout' field"
        assert "stderr" in result, "Missing 'stderr' field"
        assert "truncated" in result, "Missing 'truncated' field"

        # cwd should be the workspace
        ws_real = os.path.realpath(str(workspace))
        stdout_stripped = result["stdout"].strip()
        assert os.path.realpath(stdout_stripped) == ws_real, (
            f"Expected cwd={ws_real!r}, got stdout={stdout_stripped!r}"
        )


# ---------------------------------------------------------------------------
# Task 10.5 — Property test P16: timeout forces termination
# ---------------------------------------------------------------------------

# Feature: builtin-tools, Property 16: timeout forces termination
class TestExecuteCommandPropertyP16:
    """**Validates: Requirements 7.3, 7.7**"""

    @settings(max_examples=5, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
    @given(st.just(None))  # single-shot property
    def test_timeout_forces_termination(self, workspace, _):
        """A sleep command with a very short timeout returns a Timeout error."""
        result = _json.loads(_exec_shell("sleep 60", timeout=1))

        assert result.get("error") == "Timeout", (
            f"Expected Timeout error, got: {result}"
        )
        assert result.get("exit_code") is None, (
            f"Expected exit_code=null for timeout, got: {result.get('exit_code')}"
        )
        assert "60" in result.get("message", "") or "1" in result.get("message", ""), (
            f"Timeout message should mention timeout duration: {result.get('message')}"
        )


# ---------------------------------------------------------------------------
# Task 10.6 — Property test P17: output truncation when exceeding limit
# ---------------------------------------------------------------------------

# Feature: builtin-tools, Property 17: output truncation when exceeding limit
class TestExecuteCommandPropertyP17:
    """**Validates: Requirements 7.5**"""

    @settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
    @given(
        limit=st.integers(min_value=2, max_value=10),
        extra=st.integers(min_value=1, max_value=5),
    )
    def test_output_truncation_when_exceeding_limit(self, workspace, monkeypatch, limit, extra):
        """When command output exceeds EXEC_OUTPUT_LINE_LIMIT, truncated=true is returned."""
        total_lines = limit + extra
        # Generate a command that produces exactly total_lines lines of output
        command = f"seq 1 {total_lines}"

        monkeypatch.setenv("EXEC_OUTPUT_LINE_LIMIT", str(limit))

        result = _json.loads(_exec_shell(command))

        assert "error" not in result, f"Unexpected error: {result}"
        assert result["truncated"] is True, (
            f"Expected truncated=true with {total_lines} lines and limit={limit}, "
            f"got: {result}"
        )
        assert "omitted_lines" in result, "Missing 'omitted_lines' field when truncated"
        assert result["omitted_lines"] == extra, (
            f"Expected omitted_lines={extra}, got {result['omitted_lines']}"
        )


# ---------------------------------------------------------------------------
# Task 11.3 — Unit tests for _undo
# ---------------------------------------------------------------------------

from runtime.builtin_tools import _undo


class TestUndoUnit:
    """Unit tests for _undo()."""

    def test_no_session_journal_returns_error(self, monkeypatch, workspace):
        monkeypatch.setenv("AGENTS_WORKSPACE", str(workspace))
        monkeypatch.setattr(_thread_local, "session_dir", None, raising=False)

        result = _json.loads(_undo())

        assert result["error"] == "NoSessionJournal"

    def test_undo_latest_journal_turn_restores_and_moves_files(self, workspace, journal_context):
        target = workspace / "undo_target.txt"
        write_result = _json.loads(_write_file("undo_target.txt", "created by agent\n"))
        assert "error" not in write_result, f"write_file failed: {write_result}"
        assert target.exists()

        result = _json.loads(_undo())

        assert "error" not in result, f"undo failed: {result}"
        assert result["turn_key"] == "260511_102030"
        assert result["restored_files"] == ["undo_target.txt"]
        assert not target.exists()
        manifest_path = journal_context / "file_journals" / "260511_102030" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert "files" not in manifest
        assert "undo_target.txt" in manifest["undone_files"]

        second = _json.loads(_undo())
        assert second["error"] == "NoJournalToUndo"


# ---------------------------------------------------------------------------
# Task 12 — Integration tests
# ---------------------------------------------------------------------------


class TestIntegration:
    """Integration tests using real git and real subprocesses."""

    # -----------------------------------------------------------------------
    # Task 12.1 — write_file file journal cycle
    # -----------------------------------------------------------------------

    def test_write_file_creates_journal_and_keeps_worktree_dirty(self, workspace, journal_context):
        """write_file creates a sidecar journal and leaves the change visible to git diff."""
        target = "integration_new_file.txt"
        target_path = workspace / target
        assert not target_path.exists(), "Pre-condition: file should not exist before write"

        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=workspace, check=True, capture_output=True, text=True
        ).stdout.strip()
        write_result = _json.loads(_write_file(target, "hello from integration test\n"))
        assert "error" not in write_result, f"write_file failed: {write_result}"
        assert target_path.exists(), "File should exist after write_file"

        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=workspace, check=True, capture_output=True, text=True
        ).stdout.strip()
        assert head_after == head_before
        assert "commit_id" not in write_result

        manifest_path = journal_context / "file_journals" / "260511_102030" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = manifest["files"][target]
        assert entry["baseline"] == {"exists": False}
        assert entry["after"]["store"] == "sidecar"

    # -----------------------------------------------------------------------
    # Task 12.2 — edit_file journal cycle
    # -----------------------------------------------------------------------

    def test_edit_file_journal_keeps_first_baseline_and_latest_after(self, workspace, journal_context):
        """multiple edits in one turn preserve the first baseline and update latest after."""
        target = "integration_edit_target.txt"
        target_path = workspace / target
        target_path.write_text("line one\nline two\nline three\n", encoding="utf-8")

        first = _json.loads(_edit_file(
            target,
            "search_replace",
            old_str="line two",
            new_str="line TWO",
        ))
        assert "error" not in first, f"edit_file failed: {first}"
        second = _json.loads(_edit_file(
            target,
            "search_replace",
            old_str="line three",
            new_str="line THREE",
        ))
        assert "error" not in second, f"edit_file failed: {second}"

        manifest_path = journal_context / "file_journals" / "260511_102030" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = manifest["files"][target]
        assert entry["baseline"]["store"] == "sidecar"
        assert entry["after"]["store"] == "sidecar"
        assert target_path.read_text(encoding="utf-8") == "line one\nline TWO\nline THREE\n"

    # -----------------------------------------------------------------------
    # Task 12.3 — search_code real ripgrep search
    # -----------------------------------------------------------------------

    def test_search_code_finds_known_pattern_in_created_files(self, workspace):
        """search_code with a real ripgrep search finds the expected file and line."""
        unique_pattern = "INTEGRATION_SEARCH_MARKER_XYZ_42"

        # Create files: one with the pattern, one without
        match_file = workspace / "search_match.txt"
        no_match_file = workspace / "search_no_match.txt"

        match_file.write_text(f"before\n{unique_pattern}\nafter\n")
        no_match_file.write_text("nothing to see here\n")

        result = _json.loads(_search_code(unique_pattern))
        assert "error" not in result, f"search_code failed: {result}"

        # At least one result must be found
        assert result["total_found"] >= 1, (
            f"Expected at least 1 result for pattern {unique_pattern!r}, got 0"
        )

        # Find the result that points to our match file
        matching_results = [
            r for r in result["results"]
            if "search_match.txt" in r["file"]
        ]
        assert len(matching_results) >= 1, (
            f"Expected a result pointing to 'search_match.txt', got: {result['results']}"
        )

        hit = matching_results[0]

        # Verify line number (pattern is on line 2)
        assert hit["line"] == 2, (
            f"Expected line=2 for the match, got line={hit['line']}"
        )

        # Verify context contains the matched text
        assert unique_pattern in hit["context"], (
            f"Expected context to contain {unique_pattern!r}, got: {hit['context']!r}"
        )

    # -----------------------------------------------------------------------
    # Task 12.4 — exec_shell real subprocess
    # -----------------------------------------------------------------------

    def test_execute_command_echo_hello_integration_test(self, workspace):
        """exec_shell runs a real subprocess and returns the expected output."""
        result = _json.loads(_exec_shell("echo hello_integration_test"))

        # exit_code must be 0
        assert result.get("exit_code") == 0, (
            f"Expected exit_code=0, got: {result.get('exit_code')}"
        )

        # stdout must contain the echoed string
        assert "hello_integration_test" in result.get("stdout", ""), (
            f"Expected 'hello_integration_test' in stdout, got: {result.get('stdout')!r}"
        )

        # All required fields must be present
        assert "exit_code" in result, "Missing 'exit_code' field"
        assert "stdout" in result, "Missing 'stdout' field"
        assert "stderr" in result, "Missing 'stderr' field"
        assert "truncated" in result, "Missing 'truncated' field"

    # -----------------------------------------------------------------------
    # Task 12.5 — write_file journal does not commit
    # -----------------------------------------------------------------------

    def test_write_file_does_not_create_git_commit(self, workspace, journal_context):
        """After write_file, git log remains at the user's previous commit."""
        before = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        write_result = _json.loads(_write_file("git_log_test.txt", "content for git log test\n"))
        assert "error" not in write_result, f"write_file failed: {write_result}"

        after = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert after == before
