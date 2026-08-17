"""Built-in coding/file tools for the Agent Service.

This module holds the coding-oriented built-in tools and their shared
infrastructure:

  - file journal (git-backed baselines + gzip sidecar blobs) and undo
  - path validation and the syntax-check linter
  - read_file / write_file / edit_file / search_code / exec_shell / undo
  - the subprocess registry used by the abort handler (kill_active_process)

The tool configs and callables are aggregated and registered by
``runtime.builtin_tools`` (the facade module).
"""

import fnmatch
import gzip
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import datetime
from typing import Optional

logger = logging.getLogger("runtime.builtin_tools")

if sys.platform != "win32":
    import fcntl

from runtime.common import SYSTEM_ENCODING
from runtime.common import (
    set_request_context,
    get_request_context,
    get_workspace,
    utc_now_iso as _utc_now_iso,
    parse_iso_timestamp as _parse_journal_timestamp,
    sha256_bytes as _sha256_bytes,
    safe_rel_path as _safe_rel_path,
    atomic_write_json as _atomic_write_json,
    kill_process_group,
)
from runtime.models import ToolConfig

# Shared registry mapping session_id → subprocess.Popen for the currently
# executing exec_cli command.  Populated by _exec_shell so that the abort
# handler (which runs in the HTTPServer thread) can kill the process.
_active_processes: dict[str, subprocess.Popen] = {}
_active_processes_lock = threading.Lock()

# Flag set by kill_active_process (called from the abort handler) so that
# _exec_shell can distinguish a real user abort from a command that happened
# to be killed by a signal on its own (e.g. ``pkill -f "xxx"`` suicides).
_abort_was_called_for_session: dict[str, bool] = {}
_abort_was_called_lock = threading.Lock()


def _was_killed_by_abort_handler(session_id: str | None, proc: subprocess.Popen) -> bool:
    """Check if the process was killed by a deliberate user abort.

    Uses the flag set by kill_active_process() rather than inspecting the
    return code, because a command that kills itself (e.g. ``pkill -f x``
    matching its own process) could terminate with any signal.  Only the
    explicit abort-handler path is a true "user abort".
    """
    if session_id is None:
        return False
    with _abort_was_called_lock:
        was_called = _abort_was_called_for_session.pop(session_id, False)
    return was_called


def kill_active_process(session_id: str) -> bool:
    """Kill the shell process associated with *session_id*.

    Called from the abort handler in a different thread.  Returns True if a
    process was found and killed, False otherwise.

    Sets the abort-flag for *session_id* so that _exec_shell can later
    distinguish a deliberate user abort from a command that killed itself
    (e.g. ``pkill -f "x"`` matching its own process).
    """
    with _active_processes_lock:
        proc = _active_processes.pop(session_id, None)
    if proc is None:
        return False

    # Flag this session as a deliberate user abort *before* killing, so the
    # flag is visible to _exec_shell even if this thread is preempted.
    with _abort_was_called_lock:
        _abort_was_called_for_session[session_id] = True

    try:
        # Kill the entire process group so child processes are also terminated.
        kill_process_group(proc)
        return True
    except (ProcessLookupError, OSError):
        return False


def _get_file_journal_manager(workspace: str) -> '_FileJournalManager':
    session_id = get_request_context("session_id")
    user_message_timestamp = get_request_context("user_message_timestamp")
    session_dir = get_request_context("session_dir")

    journal_manager = get_request_context("file_journal_manager")
    if (
        journal_manager is None
        or journal_manager.workspace != workspace
        or journal_manager.session_id != session_id
        or journal_manager.session_dir != session_dir
        or journal_manager.user_message_timestamp != user_message_timestamp
    ):
        journal_manager = _FileJournalManager(
            workspace,
            session_id=session_id,
            user_message_timestamp=user_message_timestamp,
            session_dir=session_dir,
        )
        set_request_context(file_journal_manager=journal_manager)
    return journal_manager
# Resolve /tmp once so that symlinked setups (e.g. macOS where /tmp -> /private/tmp)
# are handled correctly.  Any path under the *real* /tmp is unconditionally allowed
# regardless of workspace boundaries, so tools can always use it as a scratch /
# data-exchange directory.
_REAL_TMP = os.path.realpath("/tmp")


def _validate_path(workspace: str, raw_path: str) -> str:
    """Resolve *raw_path* and verify it stays inside *workspace*.

    Paths under ``/tmp`` (after resolution) are **always** permitted,
    regardless of the current workspace.  This allows tools to use
    ``/tmp`` as a reliable data-exchange / scratch directory.

    Returns the resolved absolute path on success.

    Raises
    ------
    ValueError
        With an ``error_code`` attribute set to either
        ``"PathTraversalDenied"`` or ``"AbsolutePathDenied"``.
    """
    if os.path.isabs(raw_path):
        resolved = os.path.realpath(raw_path)
    else:
        resolved = os.path.realpath(os.path.join(workspace, raw_path))

    # /tmp and anything underneath it is always allowed
    if resolved == _REAL_TMP or resolved.startswith(_REAL_TMP + os.sep):
        return resolved

    _workspace_prefix = workspace if workspace.endswith(os.sep) else workspace + os.sep
    if not (resolved == workspace or resolved.startswith(_workspace_prefix)):
        if os.path.isabs(raw_path):
            err = ValueError("Absolute paths outside workspace are not permitted")
            err.error_code = "AbsolutePathDenied"  # type: ignore[attr-defined]
        else:
            err = ValueError("Path escapes the workspace boundary")
            err.error_code = "PathTraversalDenied"  # type: ignore[attr-defined]
        raise err
    return resolved


def _journal_turn_key(value: Optional[str]) -> tuple[str, str, bool]:
    dt = _parse_journal_timestamp(value)
    if dt is None:
        dt = datetime.datetime.utcnow()
        timestamp = dt.replace(microsecond=0).isoformat()
        return dt.strftime("%y%m%d_%H%M%S"), timestamp, True
    return dt.strftime("%y%m%d_%H%M%S"), value or dt.isoformat(), False


def _flatten_journal_path(rel_path: str, role: str) -> str:
    safe_rel = _safe_rel_path(rel_path)
    flat = re.sub(r"[\\/]+", "-", safe_rel)
    flat = re.sub(r"[^A-Za-z0-9._-]", "_", flat) or "file"
    short_hash = hashlib.sha256(safe_rel.encode("utf-8")).hexdigest()[:8]
    return f"{flat}.{short_hash}.{role}.gz"


def _file_mode(path: str) -> str:
    mode = os.lstat(path).st_mode
    return "100755" if mode & stat.S_IXUSR else "100644"


def _capture_file_state(path: str) -> dict:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"exists": False}
    is_symlink = stat.S_ISLNK(st.st_mode)
    if is_symlink:
        data = os.readlink(path).encode("utf-8", errors="surrogateescape")
    else:
        with open(path, "rb") as fh:
            data = fh.read()
    return {
        "exists": True,
        "data": data,
        "mode": "100755" if st.st_mode & stat.S_IXUSR else "100644",
        "is_symlink": is_symlink,
    }


def _journal_ref_matches_state(blob_ref: object, state: dict) -> bool:
    """Return whether a journal reference describes *state* exactly."""
    if not isinstance(blob_ref, dict):
        return False
    ref_exists = bool(blob_ref.get("exists"))
    state_exists = bool(state.get("exists"))
    if ref_exists != state_exists:
        return False
    if not state_exists:
        return True
    data = state.get("data", b"")
    return (
        blob_ref.get("sha256") == _sha256_bytes(data)
        and blob_ref.get("size") == len(data)
        and blob_ref.get("mode", "100644") == state.get("mode", "100644")
        and bool(blob_ref.get("is_symlink")) == bool(state.get("is_symlink"))
    )


def _journal_refs_equal(left: object, right: object) -> bool:
    """Compare two journal references without reading their backing blobs."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    left_exists = bool(left.get("exists"))
    right_exists = bool(right.get("exists"))
    if left_exists != right_exists:
        return False
    if not left_exists:
        return True
    return (
        left.get("sha256") == right.get("sha256")
        and left.get("size") == right.get("size")
        and left.get("mode", "100644") == right.get("mode", "100644")
        and bool(left.get("is_symlink")) == bool(right.get("is_symlink"))
    )


def _restore_file_state(path: str, state: dict) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    if not state.get("exists"):
        return
    data = state.get("data", b"")
    if state.get("is_symlink"):
        target = data.decode("utf-8", errors="surrogateescape")
        os.symlink(target, path)
        return
    fd, tmp_path = tempfile.mkstemp(dir=parent or None)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_path, path)
        tmp_path = None
        os.chmod(path, 0o755 if state.get("mode") == "100755" else 0o644)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


def _blob_ref_from_state(state: dict, journal_dir: str, rel_path: str, role: str) -> dict:
    if not state.get("exists"):
        return {"exists": False}
    data = state.get("data", b"")
    blob_name = _flatten_journal_path(rel_path, role)
    blob_rel = os.path.join("files", blob_name)
    blob_path = os.path.join(journal_dir, blob_rel)
    _write_gzip_blob(blob_path, data)
    return {
        "exists": True,
        "store": "sidecar",
        "file": blob_rel.replace(os.sep, "/"),
        "sha256": _sha256_bytes(data),
        "size": len(data),
        "compression": "gzip",
        "mode": state.get("mode", "100644"),
        "is_symlink": bool(state.get("is_symlink")),
    }


def _write_gzip_blob(path: str, raw: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    compressed = gzip.compress(raw, compresslevel=6)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(compressed)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


def _read_gzip_blob(path: str, expected_sha256: Optional[str] = None) -> bytes:
    with open(path, "rb") as fh:
        raw = gzip.decompress(fh.read())
    if expected_sha256 and _sha256_bytes(raw) != expected_sha256:
        raise ValueError("Sidecar blob sha256 mismatch")
    return raw


class _ManifestLock:
    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._fh = open(self.path, "w")
        if sys.platform != "win32":
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh is not None:
            if sys.platform != "win32":
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()


class _FileJournalManager:
    def __init__(
        self,
        workspace: str,
        session_id: Optional[str] = None,
        user_message_timestamp: Optional[str] = None,
        session_dir: Optional[str] = None,
    ) -> None:
        self.workspace = os.path.realpath(workspace)
        self.session_id = session_id
        self.user_message_timestamp = user_message_timestamp
        self.session_dir = session_dir
        self.disabled = os.environ.get("DISABLE_FILE_JOURNAL", "false").lower() == "true"
        self.turn_key, self.timestamp, self.timestamp_fallback_used = _journal_turn_key(user_message_timestamp)
        self.journal_id = f"{session_id or 'stateless'}/{self.turn_key}"
        if session_dir:
            self.journal_dir = os.path.join(session_dir, "file_journals", self.turn_key)
            self.manifest_path = os.path.join(self.journal_dir, "manifest.json")
            self.lock_path = self.manifest_path + ".lock"
        else:
            self.journal_dir = None
            self.manifest_path = None
            self.lock_path = None

    def _skipped(self, reason: str) -> dict:
        return {"skipped": True, "reason": reason, "turn_key": self.turn_key, "journal_id": self.journal_id}

    def response_metadata(self) -> dict:
        if self.disabled:
            return self._skipped("disabled")
        if not self.session_dir:
            return self._skipped("no_session_dir")
        return {"journal_id": self.journal_id, "turn_key": self.turn_key, "session_id": self.session_id}

    def ensure_baseline(self, tool_name: str, file_path: str) -> dict:
        if self.disabled:
            return self._skipped("disabled")
        if not self.session_dir or not self.manifest_path or not self.journal_dir or not self.lock_path:
            return self._skipped("no_session_dir")
        try:
            resolved_path = _validate_path(self.workspace, file_path)
            rel_path = _safe_rel_path(os.path.relpath(resolved_path, self.workspace))
            with _ManifestLock(self.lock_path):
                manifest = self._load_manifest()
                manifest["status"] = "active"
                manifest["finalized"] = False
                manifest.pop("finalized_at", None)
                manifest.pop("finalize_errors", None)
                files = manifest.setdefault("files", {})
                entry = files.get(rel_path)
                if entry is None:
                    entry = {"path": rel_path, "tools": []}
                    files[rel_path] = entry
                if tool_name not in entry.setdefault("tools", []):
                    entry["tools"].append(tool_name)
                if "baseline" not in entry:
                    entry["baseline"] = self._baseline_ref(resolved_path, rel_path)
                self._save_manifest(manifest)
            return self.response_metadata()
        except Exception as exc:
            logger.warning("File journal baseline failed for %s: %s", file_path, exc)
            return {"error": "JournalFailed", "message": "Could not save baseline before modifying file"}

    def record_after(self, tool_name: str, file_path: str) -> dict:
        if self.disabled:
            return self._skipped("disabled")
        if not self.session_dir or not self.manifest_path or not self.journal_dir or not self.lock_path:
            return self._skipped("no_session_dir")
        try:
            resolved_path = _validate_path(self.workspace, file_path)
            rel_path = _safe_rel_path(os.path.relpath(resolved_path, self.workspace))
            with _ManifestLock(self.lock_path):
                manifest = self._load_manifest()
                manifest["status"] = "active"
                manifest["finalized"] = False
                manifest.pop("finalized_at", None)
                manifest.pop("finalize_errors", None)
                files = manifest.setdefault("files", {})
                entry = files.setdefault(rel_path, {"path": rel_path, "tools": []})
                if tool_name not in entry.setdefault("tools", []):
                    entry["tools"].append(tool_name)
                if "baseline" not in entry:
                    entry["baseline"] = {"exists": False}
                entry["after"] = _blob_ref_from_state(_capture_file_state(resolved_path), self.journal_dir, rel_path, "after")
                self._save_manifest(manifest)
            return self.response_metadata()
        except Exception as exc:
            logger.warning("File journal after snapshot failed for %s: %s", file_path, exc)
            return {"error": "JournalFailed", "message": "Could not save after snapshot"}

    def finalize(self) -> dict:
        """Reconcile all registered files with their final workspace state.

        Tool-level snapshots remain useful for live inspection, but arbitrary
        shell commands may modify a registered file afterwards.  Finalization
        is the turn-level consistency barrier: missing or stale ``after``
        snapshots are refreshed, and entries whose final state equals their
        baseline are removed as no-ops.  The operation is idempotent.
        """
        if self.disabled:
            return self._skipped("disabled")
        if not self.session_dir or not self.manifest_path or not self.journal_dir or not self.lock_path:
            return self._skipped("no_session_dir")
        if not os.path.isfile(self.manifest_path):
            return self.response_metadata()

        refreshed: list[str] = []
        removed: list[str] = []
        errors: list[dict] = []
        with _ManifestLock(self.lock_path):
            manifest = self._load_manifest()
            files = manifest.setdefault("files", {})
            if not isinstance(files, dict):
                files = {}
                manifest["files"] = files

            for rel_path, entry in list(files.items()):
                if not isinstance(entry, dict) or not isinstance(entry.get("baseline"), dict):
                    errors.append({"path": rel_path, "error": "missing_baseline"})
                    continue
                try:
                    safe_rel = _safe_rel_path(str(rel_path).replace("\\", "/"))
                    resolved_path = _validate_path(self.workspace, safe_rel)
                    current_state = _capture_file_state(resolved_path)
                    if not _journal_ref_matches_state(entry.get("after"), current_state):
                        entry["after"] = _blob_ref_from_state(
                            current_state, self.journal_dir, safe_rel, "after"
                        )
                        refreshed.append(safe_rel)
                    if _journal_refs_equal(entry["baseline"], entry["after"]):
                        files.pop(rel_path, None)
                        removed.append(safe_rel)
                except Exception as exc:
                    logger.warning("File journal finalize failed for %s: %s", rel_path, exc)
                    errors.append({"path": str(rel_path), "error": str(exc)})

            manifest["status"] = "finalized" if not errors else "finalize_failed"
            manifest["finalized"] = not errors
            manifest["finalized_at"] = _utc_now_iso()
            if errors:
                manifest["finalize_errors"] = errors
            else:
                manifest.pop("finalize_errors", None)
            self._save_manifest(manifest)

        return {
            **self.response_metadata(),
            "finalized": not errors,
            "refreshed_files": refreshed,
            "removed_files": removed,
            "errors": errors,
        }

    def flush(self) -> None:
        # Kept as the lifecycle hook used by inference cleanup.
        self.finalize()

    def _load_manifest(self) -> dict:
        if self.manifest_path and os.path.isfile(self.manifest_path):
            with open(self.manifest_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        now = _utc_now_iso()
        return {
            "version": 1,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "timestamp_fallback_used": self.timestamp_fallback_used,
            "turn_key": self.turn_key,
            "workspace": self.workspace,
            "created_at": now,
            "updated_at": now,
            "status": "active",
            "finalized": False,
            "files": {},
        }

    def _save_manifest(self, manifest: dict) -> None:
        manifest["updated_at"] = _utc_now_iso()
        _atomic_write_json(self.manifest_path, manifest)  # type: ignore[arg-type]

    def _baseline_ref(self, resolved_path: str, rel_path: str) -> dict:
        state = _capture_file_state(resolved_path)
        if not state.get("exists"):
            return {"exists": False}
        git_ref = self._git_baseline_ref(rel_path, state)
        if git_ref is not None:
            return git_ref
        return _blob_ref_from_state(state, self.journal_dir or "", rel_path, "baseline")

    def _git(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(["git"] + args, cwd=self.workspace, capture_output=True, text=False)

    def _git_text(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(["git"] + args, cwd=self.workspace, capture_output=True, text=True,
                              encoding=SYSTEM_ENCODING, errors='replace')

    def _git_baseline_ref(self, rel_path: str, state: dict) -> Optional[dict]:
        if state.get("is_symlink") or not os.path.isdir(os.path.join(self.workspace, ".git")):
            return None
        status = self._git_text(["status", "--porcelain", "--", rel_path])
        if status.returncode != 0 or (status.stdout or "").strip():
            return None
        oid_result = self._git_text(["rev-parse", f"HEAD:{rel_path}"])
        if oid_result.returncode != 0:
            return None
        commit_result = self._git_text(["rev-parse", "HEAD"])
        cat_result = self._git(["cat-file", "-p", (oid_result.stdout or "").strip()])
        if commit_result.returncode != 0 or cat_result.returncode != 0:
            return None
        ls_result = self._git_text(["ls-tree", "HEAD", "--", rel_path])
        mode = state.get("mode", "100644")
        if ls_result.returncode == 0 and (ls_result.stdout or "").strip():
            mode = (ls_result.stdout or "").split()[0]
        raw = cat_result.stdout or ""
        return {
            "exists": True,
            "store": "git",
            "oid": (oid_result.stdout or "").strip(),
            "git_object_format": "sha1",
            "git_commit": (commit_result.stdout or "").strip(),
            "git_path": rel_path,
            "sha256": _sha256_bytes(raw),
            "size": len(raw),
            "mode": mode,
            "is_symlink": False,
        }


class _PathValidator:
    def __init__(self, workspace: str) -> None:
        self.workspace = os.path.realpath(workspace)

    def validate(self, raw_path: str) -> str:
        return _validate_path(self.workspace, raw_path)



class _Linter:
    """Run a basic syntax check on a file after editing.

    The linter command is selected by file extension.  Unknown extensions
    always pass.  This class never raises an exception.
    """

    # Map of file extension → command template (%s is replaced by the path)
    _COMMANDS: dict[str, list[str]] = {
        ".py": ["python", "-m", "py_compile"],
        ".js": ["node", "--check"],
        ".jsx": ["node", "--check"],
        ".ts": ["npx", "--yes", "tsc", "--noEmit", "--allowJs", "--checkJs"],
        ".tsx": ["npx", "--yes", "tsc", "--noEmit", "--allowJs", "--checkJs"],
        ".json": ["python", "-c", "import sys,json; json.load(open(sys.argv[1]),'r')"],
        ".yaml": ["python", "-c",
                  "import sys; import importlib; yaml=importlib.import_module('yaml'); "
                  "yaml.safe_load(open(sys.argv[1]))"],
        ".yml": ["python", "-c",
                 "import sys; import importlib; yaml=importlib.import_module('yaml'); "
                 "yaml.safe_load(open(sys.argv[1]))"],
        ".sh": ["bash", "-n"],
    }

    def check(self, path: str) -> tuple[bool, str]:
        """Check *path* for syntax errors.

        Returns ``(True, "")`` for unknown extensions or when the check
        passes.  Returns ``(False, <output>)`` when the check fails.
        Never raises.
        """
        try:
            ext = os.path.splitext(path)[1].lower()
            cmd_template = self._COMMANDS.get(ext)
            if cmd_template is None:
                return (True, "")

            cmd = cmd_template + [path]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding=SYSTEM_ENCODING, errors='replace',
                timeout=30,
            )
            if result.returncode == 0:
                return (True, (result.stdout or "") + (result.stderr or ""))
            return (False, ((result.stdout or "") + (result.stderr or "")).strip())
        except Exception as exc:
            # Never raise — treat unexpected errors as a pass so that the
            # linter doesn't block edits when the tool is unavailable.
            logger.warning("_Linter.check(%r) raised unexpectedly: %s", path, exc)
            return (True, "")


# ---------------------------------------------------------------------------
# Task 4.1 — _read_file implementation
# ---------------------------------------------------------------------------

def _read_file(path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> str:
    """Read a file and return its contents as line-numbered JSON output."""

    def error(code: str, message: str) -> str:
        return json.dumps({"error": code, "message": message})

    workspace = get_workspace()
    check_path_for_read = os.environ.get("CHECK_PATH_FOR_READ", "false").lower() == "true"

    if check_path_for_read:
        try:
            resolved_path = _validate_path(workspace, path)
        except ValueError as exc:
            return error(exc.error_code, str(exc))  # type: ignore[attr-defined]
    else:
        if os.path.isabs(path):
            resolved_path = os.path.realpath(path)
        else:
            resolved_path = os.path.realpath(os.path.join(workspace, path))

    if not os.path.isfile(resolved_path):
        return error("FileNotFound", f"The specified file `{resolved_path}` does not exist")

    with open(resolved_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    total_lines = len(lines)

    if start_line is not None and end_line is not None and start_line > end_line:
        return error("InvalidRange", "start_line must be less than or equal to end_line")
    if start_line is not None and not 1 <= start_line <= total_lines:
        return error("LineOutOfRange", "Line number is out of file bounds")
    if end_line is not None and not 1 <= end_line <= total_lines:
        end_line = total_lines

    if start_line is None and end_line is None:
        threshold = int(os.environ.get("READ_TRUNCATION_LINES", 1000))
        start, end = 1, min(total_lines, threshold)
        truncated = total_lines > threshold
        # If we're truncating by READ_TRUNCATION_LINES, track omitted lines
        if truncated:
            omitted_lines = total_lines - threshold
        else:
            omitted_lines = 0
    else:
        start, end = start_line or 1, end_line or total_lines
        truncated = False
        omitted_lines = 0

    selected_lines = lines[start - 1:end]
    
    # Apply output limits (EXEC_OUTPUT_LINE_LIMIT and EXEC_OUTPUT_COLUMN_LIMIT)
    # similar to exec_shell and search_code
    output_line_limit = int(os.environ.get("EXEC_OUTPUT_LINE_LIMIT", 1000))
    max_line_length = int(os.environ.get("EXEC_OUTPUT_COLUMN_LIMIT", 1000))
    
    # Track if any truncation occurs
    line_truncated = False
    column_truncated = False
    
    # Store the initial omitted_lines from READ_TRUNCATION_LINES
    read_truncation_omitted = omitted_lines
    
    # Truncate lines if needed (EXEC_OUTPUT_LINE_LIMIT)
    if len(selected_lines) > output_line_limit:
        truncated = True
        omitted_lines = total_lines - output_line_limit
        selected_lines = selected_lines[:output_line_limit]
        line_truncated = True
    
    # Build content with line numbers and apply column limit
    content_parts = []
    for line_number, line in enumerate(selected_lines, start=start):
        # Remove trailing newline for consistent formatting
        line_content = line.rstrip('\n').rstrip('\r\n')
        
        # Apply column limit to each line
        if len(line_content) > max_line_length:
            line_content = line_content[:max_line_length - 3] + "..."
            column_truncated = True
        
        content_parts.append(f"{line_number}: {line_content}")
    
    # If column was truncated, mark as truncated
    if column_truncated:
        truncated = True
    
    content = "\n".join(content_parts)
    
    # Add truncation notice if output was truncated by line count
    if line_truncated:
        content += f"\n[...output truncated: {omitted_lines} lines omitted...]"
    
    # Calculate final omitted_lines for the result
    if line_truncated:
        # Use the EXEC_OUTPUT_LINE_LIMIT based omission
        final_omitted_lines = omitted_lines
    elif read_truncation_omitted > 0:
        # Use the READ_TRUNCATION_LINES based omission
        final_omitted_lines = read_truncation_omitted
    else:
        # If only column was truncated or no truncation, no lines were omitted
        final_omitted_lines = 0
    
    result: dict = {
        "content": content,
        "total_lines": total_lines,
        "truncated": truncated,
    }
    if truncated:
        result["omitted_lines"] = final_omitted_lines

    return json.dumps(result)


# Task 4.2 — Register read_file tool
READ_FILE_TOOL_CONFIG = ToolConfig(
    tool_id="read_file",
    tool_type="function",
    name="read_file",
    description=(
        "Read a file from the workspace and return its contents with line numbers. "
        "Optionally specify start_line and/or end_line to read a range."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file (relative to workspace)",
            },
            "start_line": {
                "type": "integer",
                "description": "First line to return (1-indexed, inclusive)",
            },
            "end_line": {
                "type": "integer",
                "description": "Last line to return (1-indexed, inclusive)",
            },
        },
        "required": ["path"],
    },
    builtin=True,
)


# ---------------------------------------------------------------------------
# Task 5.1 — _write_file implementation
# ---------------------------------------------------------------------------

def _write_file(path: str, content: str) -> str:
    """Write content to a file atomically with a pre-write file journal snapshot.

    Args:
        path: Path to the file (relative to workspace or absolute within it).
        content: The content to write to the file (UTF-8 string).

    Returns:
        JSON string with keys: file, bytes_written, journal on success.
        On error, returns JSON string with keys: error, message.
    """
    import tempfile

    workspace = get_workspace()
    encoded = content.encode("utf-8")
    bytes_written = len(encoded)

    # Always write to a temp file first so content is never lost even when
    # path validation fails (content may be large / expensive in tokens).
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix="write_file_", suffix=".tmp", dir="/tmp"
        )
        try:
            with os.fdopen(fd, "wb") as tmp_file:
                tmp_file.write(encoded)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
    except Exception as exc:
        return json.dumps({"error": "WriteFailure", "message": str(exc)})

    try:
        resolved_path = _validate_path(workspace, path)
    except ValueError as exc:
        # Validation failed – temp file in /tmp is kept; point caller at it.
        return json.dumps({
            "error": exc.error_code,  # type: ignore[attr-defined]
            "message": (
                f"{exc}. Content was NOT written to the requested path "
                f"but has been saved to temporary file: {tmp_path}."
                f" Please use a file move command to move it to a relative path under the workspace."
            ),
        })

    journal_manager = _get_file_journal_manager(workspace)
    backup = _capture_file_state(resolved_path)
    journal_result = journal_manager.ensure_baseline("write_file", resolved_path)
    if isinstance(journal_result, dict) and journal_result.get("error"):
        return json.dumps(journal_result)

    # Create parent directories if they don't exist
    parent_dir = os.path.dirname(resolved_path)
    try:
        os.makedirs(parent_dir, exist_ok=True)
    except OSError as exc:
        return json.dumps({"error": "WriteFailure", "message": str(exc)})

    # Move temp file to target.  shutil.move uses os.rename (atomic) when
    # /tmp and the target are on the same filesystem, falling back to
    # copy+delete otherwise.
    try:
        shutil.move(tmp_path, resolved_path)
        tmp_path = None  # moved successfully, no cleanup needed
    except OSError as exc:
        return json.dumps({"error": "WriteFailure", "message": str(exc)})
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    journal_result = journal_manager.record_after("write_file", resolved_path)
    if isinstance(journal_result, dict) and journal_result.get("error"):
        try:
            _restore_file_state(resolved_path, backup)
        except Exception:
            return json.dumps({
                "error": "JournalFailed",
                "message": "Could not save after snapshot and failed to restore file",
            })
        return json.dumps({
            "error": "JournalFailed",
            "message": "Could not save after snapshot; file was restored to pre-call state",
        })

    # Compute relative path from workspace root for the response
    rel_path = os.path.relpath(resolved_path, workspace)

    journal_meta = journal_manager.response_metadata()
    return json.dumps({
        "file": rel_path,
        "bytes_written": bytes_written,
        "journal_id": journal_meta.get("journal_id"),
        "journal": journal_meta,
    })


# Task 5.2 — Register write_file tool
WRITE_FILE_TOOL_CONFIG = ToolConfig(
    tool_id="write_file",
    tool_type="function",
    name="write_file",
    description=(
        "Write content to a file in the workspace atomically. "
        "Creates parent directories if they don't exist. "
        "A file journal snapshot is saved before writing so the change can be reviewed or reverted with the session."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file (relative to workspace)",
            },
            "content": {
                "type": "string",
                "description": "Content to write to the file (UTF-8 string)",
            },
        },
        "required": ["path", "content"],
    },
    builtin=True,
)

# ---------------------------------------------------------------------------
# Task 7.1 & 7.2 — _edit_file implementation (search_replace + diff modes)
# ---------------------------------------------------------------------------

def _edit_file(
    path: str,
    mode: str,
    old_str: Optional[str] = None,
    new_str: Optional[str] = None,
    patch: Optional[str] = None,
) -> str:
    """Edit a file using search_replace or diff mode.

    Args:
        path: Path to the file (relative to workspace or absolute within it).
        mode: Either 'search_replace' or 'diff'.
        old_str: (search_replace mode) The text block to find and replace.
        new_str: (search_replace mode) The replacement text block.
        patch: (diff mode) A unified diff patch string to apply.

    Returns:
        JSON string with keys: file, lines_added, lines_removed, file_modified on success.
        On error, returns JSON string with keys: error, message.
    """
    workspace = get_workspace()

    try:
        resolved_path = _validate_path(workspace, path)
    except ValueError as exc:
        return json.dumps({"error": exc.error_code, "message": str(exc)})  # type: ignore[attr-defined]

    # Check file exists
    if not os.path.isfile(resolved_path):
        return json.dumps({"error": "FileNotFound", "message": f"The specified file `{resolved_path}` does not exist"})

    journal_manager = _get_file_journal_manager(workspace)
    backup = _capture_file_state(resolved_path)
    journal_result = journal_manager.ensure_baseline("edit_file", resolved_path)
    if isinstance(journal_result, dict) and journal_result.get("error"):
        return json.dumps(journal_result)

    rel_path = os.path.relpath(resolved_path, workspace)

    result = None
    if mode == "search_replace":
        result = _edit_file_search_replace(
            resolved_path, rel_path, workspace, old_str, new_str, backup
        )
    elif mode == "diff":
        result = _edit_file_diff(
            resolved_path, rel_path, workspace, patch, backup
        )
    else:
        return json.dumps({"error": "InvalidMode", "message": f"Unknown mode: {mode!r}. Use 'search_replace' or 'diff'."})

    if result and isinstance(result, str):
        try:
            result_data = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result
        if "error" in result_data:
            return result
        journal_result = journal_manager.record_after("edit_file", resolved_path)
        if isinstance(journal_result, dict) and journal_result.get("error"):
            try:
                _restore_file_state(resolved_path, backup)
            except Exception:
                return json.dumps({
                    "error": "JournalFailed",
                    "message": "Could not save after snapshot and failed to restore file",
                })
            return json.dumps({
                "error": "JournalFailed",
                "message": "Could not save after snapshot; file was restored to pre-call state",
            })
        journal_meta = journal_manager.response_metadata()
        result_data["journal_id"] = journal_meta.get("journal_id")
        result_data["journal"] = journal_meta
        return json.dumps(result_data)

    return result


def _strip_lines(text: str) -> list[str]:
    """Return a list of lines with leading/trailing whitespace stripped."""
    return [line.strip() for line in text.splitlines()]


def _find_first_occurrence(file_lines: list[str], old_str: str) -> Optional[int]:
    """Find the start index (0-based) of the first occurrence of old_str in file_lines.

    Uses whitespace-tolerant matching: leading/trailing whitespace on each line
    is ignored when comparing.

    Returns the 0-based line index of the first matching line, or None if not found.
    """
    old_stripped = _strip_lines(old_str)
    if not old_stripped:
        return None

    n_old = len(old_stripped)
    n_file = len(file_lines)

    file_stripped = [line.strip() for line in file_lines]

    for i in range(n_file - n_old + 1):
        if file_stripped[i:i + n_old] == old_stripped:
            return i
    return None


def _edit_file_search_replace(
    resolved_path: str,
    rel_path: str,
    workspace: str,
    old_str: Optional[str],
    new_str: Optional[str],
    backup: dict,
) -> str:
    """Perform search_replace edit on the file."""
    if old_str is None:
        return json.dumps({"error": "LineNotFound", "message": "old_str parameter is required for search_replace mode"})
    if new_str is None:
        new_str = ""

    # Read file content
    with open(resolved_path, "r", encoding="utf-8", errors="replace") as f:
        file_lines = f.readlines()

    # Find first occurrence using whitespace-tolerant matching
    match_start = _find_first_occurrence(file_lines, old_str)
    if match_start is None:
        return json.dumps({"error": "LineNotFound", "message": "Could not find text block in the specified file"})

    old_line_count = len(old_str.splitlines()) if old_str else 0
    # Ensure we handle old_str that doesn't end with newline
    # The matched block spans file_lines[match_start : match_start + old_line_count]

    # Build new content: lines before + new_str lines + lines after
    new_str_lines = new_str.splitlines(keepends=True)
    # If new_str doesn't end with newline but the replaced block did, preserve trailing newline
    if new_str_lines and not new_str_lines[-1].endswith("\n"):
        # Check if the last replaced line had a newline
        last_replaced_idx = match_start + old_line_count - 1
        if last_replaced_idx < len(file_lines) and file_lines[last_replaced_idx].endswith("\n"):
            new_str_lines[-1] += "\n"
    elif not new_str_lines and old_line_count > 0:
        # Replacing with empty string — remove the lines entirely
        pass

    new_file_lines = (
        file_lines[:match_start]
        + new_str_lines
        + file_lines[match_start + old_line_count:]
    )

    new_content = "".join(new_file_lines)

    # Write back
    try:
        import tempfile
        parent_dir = os.path.dirname(resolved_path)
        fd, tmp_path = tempfile.mkstemp(dir=parent_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            os.unlink(tmp_path)
            raise
        os.replace(tmp_path, resolved_path)
    except OSError as exc:
        return json.dumps({"error": "WriteFailure", "message": str(exc)})

    # Run linter
    linter = _Linter()
    passed, lint_output = linter.check(resolved_path)
    if not passed:
        _restore_file_state(resolved_path, backup)
        return json.dumps({"error": "LintFailed", "message": lint_output})

    # Calculate lines added/removed
    new_line_count = len(new_str.splitlines()) if new_str else 0
    lines_removed = old_line_count
    lines_added = new_line_count

    # Detect if file content actually changed
    old_content = backup.get("data", b"")
    try:
        with open(resolved_path, "rb") as f:
            new_content = f.read()
    except FileNotFoundError:
        # File was deleted during edit (unlikely but handle gracefully)
        new_content = b""
    file_modified = (old_content != new_content)

    return json.dumps({
        "file": rel_path,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "file_modified": file_modified,
    })


def _strip_diff_fence(patch: str) -> str:
    lines = patch.splitlines(keepends=True)
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is None or not lines[first].lstrip().startswith("```"):
        return patch

    last = next((i for i in range(len(lines) - 1, first, -1) if lines[i].strip()), None)
    if last is not None and lines[last].strip() == "```":
        return "".join(lines[first + 1:last])
    return patch


def _find_line_block(file_lines: list[str], block: list[str], start: int = 0) -> Optional[int]:
    """Find a block of logical lines in file_lines using whitespace-tolerant matching."""
    if not block:
        return start
    stripped_file = [line.rstrip("\n").strip() for line in file_lines]
    stripped_block = [line.strip() for line in block]
    n = len(stripped_block)
    for i in range(max(start, 0), len(stripped_file) - n + 1):
        if stripped_file[i:i + n] == stripped_block:
            return i
    return None


def _format_hunk_header(old_start: int, old_count: int, new_start: int, new_count: int) -> str:
    old_range = str(old_start) if old_count == 1 else f"{old_start},{old_count}"
    new_range = str(new_start) if new_count == 1 else f"{new_start},{new_count}"
    return f"@@ -{old_range} +{new_range} @@"


def _is_added_line(line: str) -> bool:
    return line.startswith("+")


def _is_removed_line(line: str) -> bool:
    return line.startswith("-")


def _count_hunk_lines(hunk_lines: list[str]) -> tuple[int, int]:
    old_count = 0
    new_count = 0
    for line in hunk_lines:
        if _is_added_line(line):
            new_count += 1
        elif _is_removed_line(line):
            old_count += 1
        elif line.startswith("\\"):
            continue
        else:
            old_count += 1
            new_count += 1
    return old_count, new_count


def _rewrite_unified_hunk_counts(patch: str) -> str:
    """Rewrite hunk line counts to match the hunk body.

    LLMs often produce otherwise-valid unified diffs with stale @@ -a,b +c,d @@
    counts.  The external patch command treats those as malformed, so normalize
    the counts before invoking it.
    """
    lines = patch.splitlines()
    out: list[str] = []
    i = 0
    hunk_re = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$")
    while i < len(lines):
        line = lines[i]
        match = hunk_re.match(line)
        if not match:
            out.append(line)
            i += 1
            continue

        body: list[str] = []
        i += 1
        while i < len(lines):
            next_line = lines[i]
            if next_line.startswith("@@ ") or next_line.startswith("--- "):
                break
            if not next_line.startswith((" ", "+", "-", "\\")):
                # Some generated diffs omit the required leading space on
                # context lines.  Add it so the hunk is syntactically valid.
                next_line = f" {next_line}"
            body.append(next_line)
            i += 1

        old_count, new_count = _count_hunk_lines(body)
        old_start = int(match.group(1))
        new_start = int(match.group(2))
        suffix = match.group(3) or ""
        out.append(_format_hunk_header(old_start, old_count, new_start, new_count) + suffix)
        out.extend(body)

    return "\n".join(out) + ("\n" if patch.endswith("\n") or out else "")


def _build_located_hunk(file_lines: list[str], patch_lines: list[str], search_start: int = 0) -> tuple[Optional[list[str]], int]:
    old_block = [line[1:] if _is_removed_line(line) else line[1:] if line.startswith(" ") else line for line in patch_lines if not _is_added_line(line)]
    if not old_block:
        # Pure insertion with no context is ambiguous; let patch report a useful
        # diagnostic instead of inventing a location.
        return None, search_start

    match_start = _find_line_block(file_lines, old_block, search_start)
    if match_start is None:
        return None, search_start

    old_count, new_count = _count_hunk_lines(patch_lines)
    hunk = [_format_hunk_header(match_start + 1, old_count, match_start + 1, new_count)]
    for line in patch_lines:
        if _is_added_line(line) or _is_removed_line(line) or line.startswith("\\"):
            hunk.append(line)
        else:
            hunk.append(f" {line[1:] if line.startswith(' ') else line}")
    return hunk, match_start + max(old_count, 1)


def _convert_begin_patch_format(patch: str, rel_path: str, resolved_path: Optional[str] = None) -> str:
    """Convert common *** Begin Patch update hunks to unified diff.

    The Begin Patch DSL frequently uses bare @@ markers as anchors, e.g. one
    anchor for the containing function and another anchor for the insertion
    point.  Treat anchor-only sections as location hints and emit only hunks that
    actually contain +/- changes, with line numbers located from the target file.
    """
    file_lines: list[str] = []
    if resolved_path:
        try:
            with open(resolved_path, "r", encoding="utf-8", errors="replace") as f:
                file_lines = f.readlines()
        except OSError:
            file_lines = []

    lines = patch.splitlines()
    result: list[str] = []
    current_file = rel_path
    section: list[str] = []
    search_start = 0
    in_file = False

    def flush_section() -> None:
        nonlocal section, search_start
        if not section:
            return
        has_change = any(_is_added_line(line) or _is_removed_line(line) for line in section)
        if has_change:
            hunk, next_start = _build_located_hunk(file_lines, section, search_start)
            if hunk is None:
                # Fall back to a syntactically valid hunk; patch will diagnose
                # any context mismatch.
                old_count, new_count = _count_hunk_lines(section)
                hunk = [_format_hunk_header(1, old_count, 1, new_count), *section]
            else:
                search_start = next_start
            result.extend(hunk)
        else:
            found = _find_line_block(file_lines, section, search_start)
            if found is not None:
                search_start = found + len(section)
        section = []

    for line in lines:
        if line.startswith("*** Begin Patch") or line.startswith("*** End Patch"):
            continue
        if line.startswith("*** Update File:"):
            flush_section()
            current_file = line.split(":", 1)[1].strip() or rel_path
            result = [f"--- {current_file}", f"+++ {current_file}"]
            in_file = True
            continue
        if line.startswith("*** Add File:") or line.startswith("*** Delete File:"):
            # Keep unsupported operations syntactically simple. edit_file already
            # targets an existing single file, so update hunks are the useful case.
            flush_section()
            current_file = line.split(":", 1)[1].strip() or rel_path
            result = [f"--- {current_file}", f"+++ {current_file}"]
            in_file = True
            continue
        if line.startswith("@@"):
            # Bare @@ markers are anchors in Begin Patch DSL.  Numbered @@
            # headers are also treated as section boundaries; we recalculate the
            # final location/counts from the target file below.
            flush_section()
            continue
        if in_file:
            section.append(line)

    flush_section()
    if not result:
        return ""
    return _rewrite_unified_hunk_counts("\n".join(result) + "\n")


def _normalize_patch_for_path(patch: str, rel_path: str, resolved_path: Optional[str] = None) -> str:
    patch = _strip_diff_fence(patch)

    # Handle "*** Begin Patch" format (used by some LLMs)
    if "*** Begin Patch" in patch:
        patch = _convert_begin_patch_format(patch, rel_path, resolved_path)

    lines = patch.splitlines(keepends=True)
    first_text = next((line.lstrip() for line in lines if line.strip()), "")
    if first_text.startswith("@@ "):
        patch = f"--- {rel_path}\n+++ {rel_path}\n" + patch
        return _rewrite_unified_hunk_counts(patch)

    normalized = []
    before_first_hunk = True
    for line in lines:
        if before_first_hunk and line.startswith("--- "):
            suffix = "\n" if line.endswith("\n") else ""
            normalized.append(f"--- {rel_path}{suffix}")
            continue
        if before_first_hunk and line.startswith("+++ "):
            suffix = "\n" if line.endswith("\n") else ""
            normalized.append(f"+++ {rel_path}{suffix}")
            continue
        if line.startswith("@@ "):
            before_first_hunk = False
        normalized.append(line)
    return _rewrite_unified_hunk_counts("".join(normalized))


def _patch_process_output(result: subprocess.CompletedProcess) -> str:
    output = "\n".join(
        part.strip() for part in (result.stdout or "", result.stderr or "") if part and part.strip()
    )
    
    if not output:
        return "Patch did not apply cleanly"
    
    # Add helpful context for common errors
    if "Only garbage was found" in output:
        return f"{output}\n\nHint: The patch format appears to be invalid. Make sure to use standard unified diff format with proper --- and +++ file headers."
    
    if "unexpectedly ends" in output:
        return f"{output}\n\nHint: The patch appears to be incomplete. Make sure all hunks are properly terminated."
    
    if "patch: ****" in output:
        return f"{output}\n\nHint: The patch format is invalid. Consider using search_replace mode instead."
    
    return output


def _cleanup_patch_artifacts(resolved_path: str) -> None:
    for suffix in (".orig", ".rej"):
        try:
            os.unlink(resolved_path + suffix)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Failed to remove patch artifact: %s", resolved_path + suffix)


def _restore_patched_file(resolved_path: str, backup: dict) -> None:
    _restore_file_state(resolved_path, backup)
    _cleanup_patch_artifacts(resolved_path)


def _run_patch(workspace: str, patch: str, dry_run: bool) -> subprocess.CompletedProcess:
    args = ["patch", "--batch", "--forward", "-p0"]
    if dry_run:
        args.append("--dry-run")
    return subprocess.run(
        args,
        input=patch,
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding=SYSTEM_ENCODING, errors='replace',
    )


def _edit_file_diff(
    resolved_path: str,
    rel_path: str,
    workspace: str,
    patch: Optional[str],
    backup: dict,
) -> str:
    """Apply a unified diff patch to the file."""
    if patch is None:
        return json.dumps({"error": "PatchFailed", "message": "patch parameter is required for diff mode"})

    normalized_patch = _normalize_patch_for_path(patch, rel_path, resolved_path)

    try:
        dry_run = _run_patch(workspace, normalized_patch, dry_run=True)
    except FileNotFoundError:
        return json.dumps({"error": "PatchFailed", "message": "patch command not found"})

    if dry_run.returncode != 0:
        _cleanup_patch_artifacts(resolved_path)
        return json.dumps({"error": "PatchFailed", "message": _patch_process_output(dry_run)})

    result = _run_patch(workspace, normalized_patch, dry_run=False)
    if result.returncode != 0:
        _restore_patched_file(resolved_path, backup)
        return json.dumps({"error": "PatchFailed", "message": _patch_process_output(result)})

    _cleanup_patch_artifacts(resolved_path)

    linter = _Linter()
    passed, lint_output = linter.check(resolved_path)
    if not passed:
        _restore_patched_file(resolved_path, backup)
        return json.dumps({"error": "LintFailed", "message": lint_output})

    lines_added = 0
    lines_removed = 0
    for line in normalized_patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines_added += 1
        elif line.startswith("-") and not line.startswith("---"):
            lines_removed += 1

    # Detect if file content actually changed
    old_content = backup.get("data", b"")
    try:
        with open(resolved_path, "rb") as f:
            new_content = f.read()
    except FileNotFoundError:
        # File was deleted during edit (unlikely but handle gracefully)
        new_content = b""
    file_modified = (old_content != new_content)

    return json.dumps({
        "file": rel_path,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "file_modified": file_modified,
    })


# Task 7.3 — Register edit_file tool
EDIT_FILE_TOOL_CONFIG = ToolConfig(
    tool_id="edit_file",
    tool_type="function",
    name="edit_file",
    description=(
        "Edit a file in the workspace using search_replace or diff mode. "
        "In search_replace mode, finds the first occurrence of old_str and replaces it with new_str. "
        "In diff mode, applies a unified diff patch. "
        "A file journal snapshot is saved before editing. "
        "Syntax is checked after editing; if it fails the edit is reverted to its pre-call state."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file (relative to workspace)",
            },
            "mode": {
                "type": "string",
                "enum": ["search_replace", "diff"],
                "description": "Edit mode: 'search_replace' (recommended) or 'diff'",
            },
            "old_str": {
                "type": "string",
                "description": "(search_replace mode) The text block to find and replace",
            },
            "new_str": {
                "type": "string",
                "description": "(search_replace mode) The replacement text block",
            },
            "patch": {
                "type": "string",
                "description": "(diff mode) A unified diff patch string to apply",
            },
        },
        "required": ["path", "mode"],
    },
    builtin=True,
)

# ---------------------------------------------------------------------------
# Task 8.1 — _search_code implementation
# ---------------------------------------------------------------------------

def _split_patterns(pattern: str | None) -> list[str]:
    """Split a pattern string by | into a list of non-empty normalized patterns."""
    if not pattern:
        return []
    return [p.strip().replace("\\", "/") for p in pattern.split("|") if p.strip()]


def _search_glob_matches(rel_path: str, pattern: str) -> bool:
    """Best-effort path glob matching shared by rg and grep result filtering."""
    normalized_path = rel_path.replace("\\", "/")
    if normalized_path.startswith("./"):
        normalized_path = normalized_path[2:]
    normalized_pattern = pattern.strip().replace("\\", "/")
    if normalized_pattern in {"*", "**", "**/*"}:
        return True

    basename = os.path.basename(normalized_path)
    if fnmatch.fnmatch(normalized_path, normalized_pattern):
        return True
    if fnmatch.fnmatch(basename, normalized_pattern):
        return True
    if normalized_pattern.startswith("**/"):
        suffix = normalized_pattern[3:]
        return fnmatch.fnmatch(normalized_path, suffix) or fnmatch.fnmatch(basename, suffix)
    return False


def _search_path_allowed(rel_path: str, include_patterns: list[str], exclude_patterns: list[str]) -> bool:
    """Apply user include/exclude globs after command execution.

    This is required for grep because GNU grep applies ``--include`` and
    ``--exclude`` to basenames only, while rg-style globs are path-aware.
    """
    normalized_path = rel_path.replace("\\", "/")
    if normalized_path.startswith("./"):
        normalized_path = normalized_path[2:]
    if include_patterns and not any(_search_glob_matches(normalized_path, pat) for pat in include_patterns):
        return False
    if exclude_patterns and any(_search_glob_matches(normalized_path, pat) for pat in exclude_patterns):
        return False
    return True


def _grep_include_args(patterns: list[str]) -> list[str]:
    """Convert path-aware include globs into safe basename-only grep includes."""
    args: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        basename_pattern = pattern.rsplit("/", 1)[-1]
        if basename_pattern in {"", "*", "**"} or basename_pattern in seen:
            continue
        seen.add(basename_pattern)
        args.append(f"--include={basename_pattern}")
    return args


def _grep_exclude_args(patterns: list[str]) -> list[str]:
    """Return grep excludes only for basename globs to avoid over-exclusion."""
    args: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        if "/" in pattern:
            continue
        if pattern in {"", "*", "**"} or pattern in seen:
            continue
        seen.add(pattern)
        args.append(f"--exclude={pattern}")
    return args


def _search_code(query: str, include: Optional[str] = None, exclude: Optional[str] = None) -> str:
    """Search the workspace codebase for a regex pattern using ripgrep or grep.

    Args:
        query: A regular expression pattern to search for.
        include: Optional glob pattern to restrict search to matching files.
                 Multiple patterns can be separated by | (e.g. '*.svelte|*.js').
        exclude: Optional glob pattern to exclude matching files from search.
                 Multiple patterns can be separated by | (e.g. '*.log|*.bak').

    Returns:
        JSON string with keys: results, truncated, total_found on success.
        On error, returns JSON string with keys: error, message.
    """
    # Validate the query as a valid regex
    try:
        re.compile(query)
    except re.error as exc:
        return json.dumps({"error": "InvalidQuery", "message": str(exc)})

    # Get workspace
    workspace = get_workspace()

    max_results = int(os.environ.get("SEARCH_MAX_RESULTS", 100))
    max_context_length = int(os.environ.get("EXEC_OUTPUT_COLUMN_LIMIT", 1000))

    # Default excludes
    default_excludes = [".git", "node_modules", "dist"]
    include_patterns = _split_patterns(include)
    exclude_patterns = _split_patterns(exclude)

    # Try ripgrep first
    if shutil.which("rg") is not None:
        cmd = ["rg", "--json"]
        # Add default excludes
        for excl in default_excludes:
            cmd += ["--glob", f"!{excl}"]
        # Add user-specified include patterns (support | as OR)
        for pat in include_patterns:
            cmd += ["--glob", pat]
        # Add user-specified exclude patterns (support | as OR)
        for pat in exclude_patterns:
            cmd += ["--glob", f"!{pat}"]
        # Explicit path is required: when stdin is not a TTY, rg may otherwise
        # read stdin instead of recursively searching the workspace.
        cmd += ["-e", query, "."]

        try:
            result = subprocess.run(
                cmd,
                cwd=workspace,
                capture_output=True,
                text=True,
                encoding=SYSTEM_ENCODING, errors='replace',
            )
        except Exception as exc:
            return json.dumps({"error": "SearchToolNotFound", "message": str(exc)})

        results = []
        total_found = 0

        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if obj.get("type") != "match":
                continue

            data = obj.get("data", {})
            file_path = data.get("path", {}).get("text", "")
            # Make path relative to workspace
            if os.path.isabs(file_path):
                file_path = os.path.relpath(file_path, workspace)
            if not _search_path_allowed(file_path, include_patterns, exclude_patterns):
                continue

            total_found += 1
            if len(results) < max_results:
                line_number = data.get("line_number", 0)
                submatches = data.get("submatches", [])
                column = submatches[0].get("start", 0) if submatches else 0
                context = data.get("lines", {}).get("text", "").rstrip("\n").rstrip("\r\n")
                # Smart truncation: ensure search keywords are preserved
                if len(context) > max_context_length and submatches:
                    # Use submatches to determine where to truncate
                    match_start = submatches[0].get("start", 0)
                    match_end = submatches[0].get("end", 0)
                    
                    # Calculate truncation range around the match
                    context_half = max_context_length // 2
                    start = max(0, match_start - context_half)
                    end = min(len(context), match_end + context_half)
                    
                    # Ensure we don't exceed max_context_length
                    if end - start > max_context_length:
                        # Adjust to fit within limit while keeping match
                        start = max(0, end - max_context_length)
                    
                    context = context[start:end]
                    
                    # Add truncation markers
                    if start > 0:
                        context = "..." + context
                    if end < len(data.get("lines", {}).get("text", "").rstrip("\n").rstrip("\r\n")):
                        context = context + "..."
                elif len(context) > max_context_length:
                    # Fallback: simple truncation if no submatches
                    context = context[:max_context_length] + "..."
                results.append({
                    "file": file_path,
                    "line": line_number,
                    "column": column,
                    "context": context,
                })

        truncated = total_found > max_results
        return json.dumps({
            "results": results,
            "truncated": truncated,
            "total_found": total_found,
        })

    # Fall back to grep
    if shutil.which("grep") is not None:
        cmd = ["grep", "-r", "-n"]
        for excl in default_excludes:
            cmd += [f"--exclude-dir={excl}"]
        # GNU grep --include/--exclude only match basenames.  Use safe command
        # narrowing and apply authoritative path-glob filtering in Python below.
        cmd += _grep_include_args(include_patterns)
        cmd += _grep_exclude_args(exclude_patterns)
        # Add the query and search path. Use -e to prevent option injection when
        # a user regex starts with '-' and -I to ignore binary files.
        cmd += ["-I", "-E", "-e", query, "."]

        try:
            result = subprocess.run(
                cmd,
                cwd=workspace,
                capture_output=True,
                text=True,
                encoding=SYSTEM_ENCODING, errors='replace',
            )
        except Exception as exc:
            return json.dumps({"error": "SearchToolNotFound", "message": str(exc)})

        results = []
        total_found = 0

        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            # grep output format: filename:line_number:content
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            file_path, line_num_str, context = parts[0], parts[1], parts[2]
            # Make path relative (grep may prefix with ./)
            if file_path.startswith("./"):
                file_path = file_path[2:]
            if not _search_path_allowed(file_path, include_patterns, exclude_patterns):
                continue
            try:
                line_number = int(line_num_str)
            except ValueError:
                continue

            total_found += 1
            if len(results) < max_results:
                # Smart truncation for grep (no submatches info available)
                if len(context) > max_context_length:
                    # Try to find the query pattern in context for smart truncation
                    try:
                        # Find the first match of the query in context
                        match = re.search(query, context)
                        if match:
                            # Smart truncation around the match
                            context_half = max_context_length // 2
                            start = max(0, match.start() - context_half)
                            end = min(len(context), match.end() + context_half)
                            
                            # Ensure we don't exceed max_context_length
                            if end - start > max_context_length:
                                start = max(0, end - max_context_length)
                            
                            context = context[start:end]
                            
                            # Add truncation markers
                            if start > 0:
                                context = "..." + context
                            if end < len(parts[2]):
                                context = context + "..."
                        else:
                            # Fallback: simple truncation
                            context = context[:max_context_length] + "..."
                    except re.error:
                        # If regex is invalid, use simple truncation
                        context = context[:max_context_length] + "..."
                results.append({
                    "file": file_path,
                    "line": line_number,
                    "column": 0,
                    "context": context,
                })

        truncated = total_found > max_results
        return json.dumps({
            "results": results,
            "truncated": truncated,
            "total_found": total_found,
            "fallback": "grep",
        })

    # Neither rg nor grep available
    return json.dumps({
        "error": "SearchToolNotFound",
        "message": "Neither ripgrep nor grep is available in PATH",
    })


# Task 8.2 — Register search_code tool
SEARCH_CODE_TOOL_CONFIG = ToolConfig(
    tool_id="search_code",
    tool_type="function",
    name="search_code",
    description=(
        "Search the workspace codebase for a regex pattern. "
        "Returns structured results with file, line, column, and context. "
        "Uses ripgrep if available, falls back to grep. "
        "Automatically excludes .git, node_modules, and dist directories. "
        "Supports | in query for regex alternation (OR). "
        "Multiple include/exclude patterns can be separated by |."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A regular expression pattern to search for",
            },
            "include": {
                "type": "string",
                "description": "Optional glob pattern to restrict search to matching file paths. Multiple patterns can be separated by | (e.g. '*.svelte|*.js' for file extensions, 'src/**' to limit to a directory, 'src/**/*.py' for specific files in a directory).",
            },
            "exclude": {
                "type": "string",
                "description": "Optional glob pattern to exclude matching file paths from the search. Multiple patterns can be separated by | (e.g. '*_test.py|*.pyc' to skip test files and bytecode, 'vendor/**' to skip a directory).",
            },
        },
        "required": ["query"],
    },
    builtin=True,
)

# ---------------------------------------------------------------------------
# Task 10.1 — _exec_shell implementation
# ---------------------------------------------------------------------------

def _exec_shell(command: str, timeout: Optional[int] = None, background: bool = False) -> str:
    """Execute a shell command in the workspace with output limits.

    Args:
        command: The shell command to execute.
        timeout: Optional timeout in seconds. If None, uses EXEC_DEFAULT_TIMEOUT.
        background: If True and platform is Windows, run command in background using Start-Process.

    Returns:
        JSON string with keys: exit_code, stdout, stderr, truncated on success.
        When truncated, also includes omitted_lines.
        On error, returns JSON string with keys: error, message (and exit_code: null for Timeout).
    """
    # Reject empty command
    if not command or not command.strip():
        return json.dumps({"error": "EmptyCommand", "message": "Command must not be empty"})

    # Handle background execution on Windows
    if sys.platform == "win32":
        start_prefix = "start /B "
        start_index = command.find(start_prefix)
        if start_index != -1:
            command = command[start_index + len(start_prefix):]
            background = True

        if background:
            try:
                parts = shlex.split(command)
                if not parts:
                    return json.dumps({"error": "EmptyCommand", "message": "Command must not be empty"})

                program = parts[0]
                arguments = parts[1:] if len(parts) > 1 else []

                # Escape single quotes for PowerShell (double them)
                def escape_ps(s):
                    return s.replace("'", "''")

                # Build Start-Process command via PowerShell
                if arguments:
                    arg_str = " ".join(arguments)
                    ps_command = f"Start-Process -WindowStyle Hidden '{escape_ps(program)}' -ArgumentList '{escape_ps(arg_str)}'"
                else:
                    ps_command = f"Start-Process -WindowStyle Hidden '{escape_ps(program)}'"

                # Wrap in powershell.exe -Command
                command = f"powershell.exe -Command \"{ps_command}\""
            except ValueError as e:
                return json.dumps({"error": "CommandParseError", "message": f"Failed to parse command: {e}"})

    # Read configuration
    output_line_limit = int(os.environ.get("EXEC_OUTPUT_LINE_LIMIT", 1000))
    max_line_length = int(os.environ.get("EXEC_OUTPUT_COLUMN_LIMIT", 1000))
    if timeout is None:
        timeout_val = int(os.environ.get("EXEC_DEFAULT_TIMEOUT", 30))
    else:
        timeout_val = int(timeout)

    # Get workspace
    workspace = get_workspace()

    # Build environment: inherit current env, override TERM=dumb
    env = os.environ.copy()
    env["TERM"] = "dumb"

    # Use Popen with start_new_session=True so the abort handler can kill
    # the entire process group via kill_active_process().
    session_id = get_request_context("session_id")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding=SYSTEM_ENCODING, errors='replace',
            start_new_session=sys.platform != "win32",
            creationflags=creationflags,
        )
    except Exception as exc:
        return json.dumps({"error": "SpawnFailed", "message": str(exc)})

    # Register so the abort handler can kill it from another thread.
    if session_id:
        with _active_processes_lock:
            _active_processes[session_id] = proc

    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_val)
        except subprocess.TimeoutExpired:
            # Timeout — kill the process group.
            kill_process_group(proc)
            stdout, stderr = proc.communicate()
            return json.dumps({
                "error": "Timeout",
                "message": f"Command exceeded timeout of {timeout_val} seconds",
                "exit_code": None,
            })
    finally:
        # Unregister.
        if session_id:
            with _active_processes_lock:
                _active_processes.pop(session_id, None)

    # Check if the process was killed by a deliberate user abort (flag-based,
    # not returncode-based — commands like ``pkill -f "xxx"`` may suicide and
    # get any signal, which should NOT be reported as a user abort).
    if _was_killed_by_abort_handler(session_id, proc):
        return json.dumps({
            "error": "Aborted",
            "message": "Command was aborted by user",
            "exit_code": proc.returncode,
        })

    # Truncate combined output to output_line_limit lines
    # Handle case where communicate() returns None (e.g., on Windows after taskkill)
    stdout = stdout or ""
    stderr = stderr or ""
    stdout_lines = stdout.splitlines(keepends=True)
    stderr_lines = stderr.splitlines(keepends=True)
    
    # Limit line length for each line
    def truncate_line(line):
        """Truncate a line if it exceeds max_line_length."""
        if len(line) > max_line_length:
            # Keep the newline character if present
            if line.endswith('\n'):
                return line[:max_line_length - 3] + "...\n"
            else:
                return line[:max_line_length - 3] + "..."
        return line
    
    stdout_lines = [truncate_line(line) for line in stdout_lines]
    stderr_lines = [truncate_line(line) for line in stderr_lines]
    
    total_lines = len(stdout_lines) + len(stderr_lines)

    truncated = False
    omitted_lines = 0

    if total_lines > output_line_limit:
        truncated = True
        omitted_lines = total_lines - output_line_limit
        # Allocate lines: fill stdout first, then stderr with remaining budget
        if len(stdout_lines) >= output_line_limit:
            stdout_lines = stdout_lines[:output_line_limit]
            stderr_lines = []
        else:
            remaining = output_line_limit - len(stdout_lines)
            stderr_lines = stderr_lines[:remaining]
        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)
        # Append truncation notice to stdout
        stdout += f"\n[...output truncated: {omitted_lines} lines omitted...]"
    else:
        # Reassemble lines even when not truncated, so line-length truncation takes effect
        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)

    response: dict = {
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": truncated,
    }
    if truncated:
        response["omitted_lines"] = omitted_lines

    return json.dumps(response)


# Task 10.2 — Register exec_shell tool
EXEC_SHELL_TOOL_CONFIG = ToolConfig(
    tool_id="exec_shell",
    tool_type="function",
    name="exec_shell",
    description=(
        "Execute a "
        + ("Windows cmd shell " if sys.platform == "win32" else "")
        + "command in the workspace directory. Runs in non-interactive mode (TERM=dumb). "
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute",
            },
            "timeout": {
                "type": "integer",
                "description": "Optional timeout in seconds (default: EXEC_DEFAULT_TIMEOUT)",
            },
        },
        "required": ["command"],
    },
    builtin=True,
)

# Add background parameter only on Windows
if sys.platform == "win32":
    EXEC_SHELL_TOOL_CONFIG.parameters["properties"]["background"] = {
        "type": "boolean",
        "description": "Run command in background mode.",
        "default": False,
    }

# ---------------------------------------------------------------------------
# Task 11.1 — _undo implementation
# ---------------------------------------------------------------------------

def _undo() -> str:
    """Undo the latest file-journal turn for the current session without changing conversation history.

    Returns:
        JSON string with keys: turn_key, restored_files on success.
        On error, returns JSON string with keys: error, message.
    """
    workspace = get_workspace()
    session_dir = get_request_context("session_dir")
    session_id = get_request_context("session_id")
    if not session_dir:
        return json.dumps({
            "error": "NoSessionJournal",
            "message": "No current session directory is available for file journal undo",
        })
    try:
        from runtime.context_manager import undo_latest_file_journal_turn
        result = undo_latest_file_journal_turn(workspace, session_dir, session_id=session_id)
    except Exception as exc:
        result = {"error": "UndoFailed", "message": str(exc)}
    return json.dumps(result)


# Task 11.2 — Register undo tool
UNDO_TOOL_CONFIG = ToolConfig(
    tool_id="undo",
    tool_type="function",
    name="undo",
    description=(
        "Undo the latest file journal turn in the current session without changing conversation history. "
        "Restores files to that turn's baseline and marks the turn as undone."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
    builtin=True,
)



# ---------------------------------------------------------------------------
# Coding/file tool cluster (read_file, write_file, edit_file, search_code,
# exec_shell, undo).  Aggregated by runtime.builtin_tools into BUILTIN_TOOLS.
# ---------------------------------------------------------------------------
CODING_TOOLS = [
    (READ_FILE_TOOL_CONFIG, _read_file),
    (WRITE_FILE_TOOL_CONFIG, _write_file),
    (EDIT_FILE_TOOL_CONFIG, _edit_file),
    (SEARCH_CODE_TOOL_CONFIG, _search_code),
    (EXEC_SHELL_TOOL_CONFIG, _exec_shell),
    (UNDO_TOOL_CONFIG, _undo),
]
