"""Shared utilities for the Agent Service runtime.

This module provides common, low-level utilities used across the runtime
package: timestamp helpers, hashing, token estimation, atomic file I/O,
lightweight YAML front-matter parsing, image conversion, and a thread-local
request-context bus.

Zero third-party dependencies — only Python standard library.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import locale
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.request
from typing import List, Optional, Union


# ---------------------------------------------------------------------------
# Label parsing
# ---------------------------------------------------------------------------

def parse_labels(labels_input: Union[str, List, None]) -> List[str]:
    """Parse labels input into a list of unique strings.

    Args:
        labels_input: Can be a string (comma-separated), list, or None.

    Returns:
        List of unique label strings, preserving order (first occurrence kept).
    """
    if labels_input is None:
        return []
    if isinstance(labels_input, list):
        seen: set[str] = set()
        result: list[str] = []
        for label in labels_input:
            label_str = str(label).strip()
            if label_str and label_str not in seen:
                seen.add(label_str)
                result.append(label_str)
        return result
    if isinstance(labels_input, str):
        if not labels_input.strip():
            return []
        parts = re.split(r',\s*', labels_input)
        seen: set[str] = set()
        result: list[str] = []
        for part in parts:
            part = part.strip()
            if part and part not in seen:
                seen.add(part)
                result.append(part)
        return result
    # For other types, convert to string and recurse
    return parse_labels(str(labels_input))


# ---------------------------------------------------------------------------
# Timestamp utilities
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """Return current local wall-clock time as ``YYYY-MM-DDThh:mm:ss``."""
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def utc_now_iso() -> str:
    """Return current UTC time as an ISO 8601 string (no microseconds)."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def session_timestamp() -> str:
    """Return a compact session-style timestamp ``YYMMDD_HHmmss``."""
    return datetime.datetime.now().strftime("%y%m%d_%H%M%S")


def parse_iso_timestamp(value: Optional[str]) -> Optional[datetime.datetime]:
    """Parse an ISO 8601 timestamp string into a naive UTC datetime.

    Returns ``None`` if *value* is empty or cannot be parsed.
    """
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def sha256_bytes(raw: bytes) -> str:
    """Return the hex-encoded SHA-256 digest of *raw*."""
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Rough token count estimate: ``len(text) // 4``.

    Intentionally approximate; used only for budget control.
    """
    return len(text) // 4


# ---------------------------------------------------------------------------
# Atomic file I/O
# ---------------------------------------------------------------------------


def atomic_write_json(path: str, data: dict) -> None:
    """Atomically serialise *data* as pretty-printed JSON to *path*.

    Writes to a temporary file first, then ``os.replace`` for crash safety.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.write("\n")
        os.replace(tmp_path, path)
        tmp_path = ""
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


def atomic_write_text(path: str, text: str) -> None:
    """Atomically write *text* to *path*.

    Writes to a temporary file first, then ``os.replace`` for crash safety.
    """
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def safe_rel_path(rel_path: str) -> str:
    """Validate that *rel_path* is a safe relative path (no traversal).

    Returns the normalised forward-slash form on success.

    Raises:
        ValueError: If *rel_path* escapes the root.
    """
    rel_path = rel_path.replace(os.sep, "/")
    if (
        os.path.isabs(rel_path)
        or rel_path == ".."
        or rel_path.startswith("../")
        or "/../" in f"/{rel_path}/"
    ):
        raise ValueError(f"Unsafe journal path: {rel_path}")
    return rel_path


# ---------------------------------------------------------------------------
# Lightweight YAML front-matter parser
# ---------------------------------------------------------------------------


def _parse_yaml_value(raw: str) -> object:
    """Parse a single scalar YAML value (string or integer)."""
    raw = raw.strip()
    if (raw.startswith('"') and raw.endswith('"')) or (
        raw.startswith("'") and raw.endswith("'")
    ):
        return raw[1:-1]
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


def _parse_yaml_block(block: str, indent: int) -> dict:
    """Recursively parse an indented YAML block into a dict."""
    result: dict = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        stripped = line.lstrip(" ")
        current_indent = len(line) - len(stripped)

        if current_indent != indent:
            break

        if stripped.startswith("- "):
            raise ValueError(
                f"Invalid front-matter: unexpected list item at indent {indent}: {line!r}"
            )

        if ":" not in stripped:
            raise ValueError(
                f"Invalid front-matter: expected 'key: value' but got: {line!r}"
            )

        colon_pos = stripped.index(":")
        key = stripped[:colon_pos].strip()
        value_part = stripped[colon_pos + 1 :]

        if not key:
            raise ValueError(
                f"Invalid front-matter: empty key in line: {line!r}"
            )

        if value_part.strip():
            inline = value_part.strip()
            if inline == "[]":
                result[key] = []
            elif inline == "{}":
                result[key] = {}
            else:
                result[key] = _parse_yaml_value(value_part)
            i += 1
            continue

        j = i + 1
        child_lines = []
        while j < len(lines):
            next_line = lines[j]
            if not next_line.strip():
                j += 1
                continue
            next_stripped = next_line.lstrip(" ")
            next_indent = len(next_line) - len(next_stripped)
            if next_indent <= indent:
                break
            child_lines.append(next_line)
            j += 1

        if not child_lines:
            result[key] = ""
            i += 1
            continue

        first_child = child_lines[0].lstrip(" ")
        child_indent = len(child_lines[0]) - len(child_lines[0].lstrip(" "))

        if first_child.startswith("- "):
            items = []
            for cl in child_lines:
                cl_stripped = cl.lstrip(" ")
                cl_indent = len(cl) - len(cl_stripped)
                if cl_indent == child_indent and cl_stripped.startswith("- "):
                    items.append(_parse_yaml_value(cl_stripped[2:]))
                elif cl_indent > child_indent:
                    raise ValueError(
                        f"Invalid front-matter: unexpected indentation in list under key '{key}': {cl!r}"
                    )
                else:
                    raise ValueError(
                        f"Invalid front-matter: inconsistent list indentation under key '{key}': {cl!r}"
                    )
            result[key] = items
        else:
            child_block = "\n".join(child_lines)
            result[key] = _parse_yaml_block(child_block, indent=child_indent)

        i = j

    return result


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Parse a front-matter + body document.

    The document must start with ``---`` on its own line, followed by YAML
    key-value pairs, and closed by another ``---`` line.  Everything after
    the closing ``---`` is returned as *body_text*.

    Supported YAML subset:

    * String values (quoted or unquoted)
    * Integer values
    * Lists (``- item`` format, one item per line)
    * Nested dicts (indented ``key: value`` pairs)

    Args:
        text: Raw document text.

    Returns:
        A ``(yaml_dict, body_text)`` tuple.

    Raises:
        ValueError: When the front-matter is missing, malformed, or the
            closing ``---`` delimiter is absent.
    """
    if not text.startswith("---"):
        raise ValueError(
            "Invalid front-matter: document must start with '---' delimiter"
        )

    rest = text[3:]
    if rest.startswith("\r\n"):
        rest = rest[2:]
    elif rest.startswith("\n"):
        rest = rest[1:]
    else:
        raise ValueError(
            "Invalid front-matter: '---' must be followed by a newline"
        )

    close_match = re.search(r"^---[ \t]*$", rest, re.MULTILINE)
    if close_match is None:
        raise ValueError(
            "Invalid front-matter: missing closing '---' delimiter"
        )

    yaml_block = rest[: close_match.start()]
    body_text = rest[close_match.end() :]
    if body_text.startswith("\r\n"):
        body_text = body_text[2:]
    elif body_text.startswith("\n"):
        body_text = body_text[1:]

    result = _parse_yaml_block(yaml_block, indent=0)
    return result, body_text


def serialize_yaml_value(value: object, indent: int = 0) -> str:
    """Serialize a Python value to a YAML front-matter string fragment.

    For dicts and non-empty lists the returned string is multi-line and
    already includes the *indent* prefix on every line.  For scalars and
    empty collections it returns a single-line string (no leading indent).
    """
    prefix = " " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = []
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                child = serialize_yaml_value(v, indent=indent + 2)
                lines.append(f"{prefix}{k}:\n{child}")
            else:
                lines.append(f"{prefix}{k}: {serialize_yaml_value(v)}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = []
        for item in value:
            lines.append(f"{prefix}- {serialize_yaml_value(item)}")
        return "\n".join(lines)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    s = str(value)
    if not s or any(c in s for c in (":", "#", '"', "'", "\n", "\r")):
        escaped = s.replace('"', '\\"')
        return f'"{escaped}"'
    return s


def build_front_matter(front_matter: dict) -> str:
    """Render a dict as a YAML front-matter block (between ``---`` delimiters)."""
    lines = ["---"]
    for key, value in front_matter.items():
        if isinstance(value, list) and value:
            serialized = serialize_yaml_value(value, indent=2)
            lines.append(f"{key}:")
            lines.append(serialized)
        elif isinstance(value, dict) and value:
            serialized = serialize_yaml_value(value, indent=2)
            lines.append(f"{key}:")
            lines.append(serialized)
        else:
            lines.append(f"{key}: {serialize_yaml_value(value)}")
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Image / Base64 conversion
# ---------------------------------------------------------------------------


def is_likely_base64(value: str) -> bool:
    """判断字符串是否看起来像 base64 编码内容。

    检测逻辑：
    1. 长度必须大于阈值（默认 1024 字符，可通过 ``BASE64_CHECK_THRESHOLD`` 调整）
    2. 只包含 base64 合法字符（A-Z, a-z, 0-9, +, /, =）
    3. 末尾可能有 0-2 个 '=' 填充符

    Args:
        value: 要检测的字符串

    Returns:
        True 如果看起来像 base64，False 否则
    """
    if not isinstance(value, str):
        return False

    # 长度检查：base64 编码的文件内容通常很长
    if len(value) < int(os.environ.get("BASE64_CHECK_THRESHOLD", "1024")):
        return False

    # 字符合法性检查：只包含 base64 字符集
    # 移除末尾的 '=' 填充符后检查
    content = value.rstrip('=')
    if not content:
        return False

    # base64 字符集：A-Z, a-z, 0-9, +, /
    import string
    valid_chars = set(string.ascii_letters + string.digits + '+/')
    return all(c in valid_chars for c in content)


def convert_image_to_base64(img_data: str) -> str:
    """Convert an image source (URL, local path, or data URI) to a raw base64 string."""
    if img_data.startswith("data:"):
        return img_data.split(",", 1)[1]

    if img_data.startswith("http://") or img_data.startswith("https://"):
        try:
            with urllib.request.urlopen(img_data) as response:
                return base64.b64encode(response.read()).decode("utf-8")
        except Exception as e:
            raise ValueError(f"Failed to download image: {e}")

    # 没有路径分隔符、或者长度超过阈值（看起来像 base64），直接返回
    if ('/' not in img_data and '\\' not in img_data) or is_likely_base64(img_data):
        return img_data  # 已经是 base64，无需再编码

    expanded_path = os.path.expanduser(img_data)
    # 相对路径时，基于当前会话工作区解析
    if not os.path.isabs(expanded_path):
        expanded_path = os.path.join(get_workspace(), expanded_path)
    try:
        with open(expanded_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except (FileNotFoundError, PermissionError, IsADirectoryError) as e:
        raise ValueError(f"Failed to read image: {e}")


# ---------------------------------------------------------------------------
# Data directory
# ---------------------------------------------------------------------------

DATA_DIR: str = os.environ.get(
    "AGENTS_RUNTIME_DIR",
    os.path.join(os.path.expanduser("~"), ".agents_runtime"),
)


# ---------------------------------------------------------------------------
# Thread-local request context
# ---------------------------------------------------------------------------

_thread_local = threading.local()


def set_request_context(**kwargs) -> None:
    """Store key/value pairs on the current thread's request context."""
    for k, v in kwargs.items():
        setattr(_thread_local, k, v)


def get_request_context(key: str, default=None):
    """Retrieve a value from the current thread's request context."""
    return getattr(_thread_local, key, default)


def clear_request_context(keys: list[str]) -> None:
    """Delete the given *keys* from the current thread's request context."""
    for k in keys:
        try:
            delattr(_thread_local, k)
        except AttributeError:
            pass


def snapshot_request_context() -> dict:
    """Capture the current thread's request context as a plain dict.

    ``threading.local`` storage is per-thread, so a context set on the
    request/HTTP thread is invisible to any worker thread spawned by the
    runtime (e.g. ``ThreadPoolExecutor``).  Snapshot the attrs so they can be
    re-applied with :func:`restore_request_context` on the worker thread,
    keeping tools like ``exec_cli`` (persistent PTY) and ``write_file``
    (per-session file journal) pinned to the same session.
    """
    return dict(getattr(_thread_local, "__dict__", {}))


def restore_request_context(ctx: dict) -> None:
    """Replace the current thread's request context with *ctx*.

    Intended for worker threads: clears whatever context (if any) this thread
    already carries, then applies the snapshot captured on the caller thread.
    """
    attrs = getattr(_thread_local, "__dict__", {})
    attrs.clear()
    attrs.update(ctx)


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------


def get_workspace() -> str:
    """Return the current workspace directory.

    Resolution order:
    1. ``_thread_local.workspace`` (set per-request via ``set_request_context``)
    2. ``AGENTS_WORKSPACE`` environment variable
    3. ``os.getcwd()``

    The result is always ``os.path.realpath``'d.
    """
    raw = getattr(_thread_local, "workspace", "") or ""
    if not raw:
        raw = os.environ.get("AGENTS_WORKSPACE", "") or ""
    if not raw:
        raw = os.getcwd()
    return os.path.realpath(raw)


# ---------------------------------------------------------------------------
# Generic file search
# ---------------------------------------------------------------------------

import fnmatch
import shutil
import subprocess as _subprocess


def _split_glob_patterns(patterns: Optional[str]) -> list[str]:
    """Split ``|``-separated glob patterns into normalized, non-empty patterns."""
    if not patterns:
        return []
    return [p.strip().replace("\\", "/") for p in patterns.split("|") if p.strip()]


def _glob_matches_path(path: str, root: str, pattern: str) -> bool:
    """Return whether *path* matches a ripgrep-style path glob best-effort.

    Python's :mod:`fnmatch` is used as a portable fallback matcher.  We match
    both the path relative to *root* and the basename so common patterns such as
    ``*.py`` keep the same user-facing behavior across rg and grep.
    """
    normalized_pattern = pattern.strip().replace("\\", "/")
    if normalized_pattern in {"*", "**", "**/*"}:
        return True

    try:
        rel_path = os.path.relpath(os.path.realpath(path), root)
    except ValueError:
        return False
    rel_path = rel_path.replace(os.sep, "/")
    basename = os.path.basename(path)

    if fnmatch.fnmatch(rel_path, normalized_pattern):
        return True
    if fnmatch.fnmatch(basename, normalized_pattern):
        return True

    # Treat ``**/foo`` as "foo at any depth", including the root directory.
    if normalized_pattern.startswith("**/"):
        suffix = normalized_pattern[3:]
        return fnmatch.fnmatch(rel_path, suffix) or fnmatch.fnmatch(basename, suffix)

    return False


def _grep_include_args(globs: list[str]) -> list[str]:
    """Build safe GNU grep ``--include`` args from path-aware glob patterns.

    GNU grep applies ``--include`` to basenames only, so passing path globs like
    ``**/*`` or ``**/conversation.json`` directly makes grep match nothing.  We
    therefore only pass basename constraints to grep and do the authoritative
    path-glob filtering in Python after the command returns.
    """
    args: list[str] = []
    seen: set[str] = set()
    for glob in globs:
        normalized = glob.strip().replace("\\", "/")
        if normalized in {"", "*", "**", "**/*"}:
            continue
        basename_glob = normalized.rsplit("/", 1)[-1]
        if basename_glob in {"", "*", "**"} or basename_glob in seen:
            continue
        seen.add(basename_glob)
        args.append(f"--include={basename_glob}")
    return args


def _filter_search_paths(paths: set[str], root: str, include_globs: list[str], exclude_dirs: list[str]) -> set[str]:
    """Normalize and filter search result paths returned by rg/grep."""
    filtered: set[str] = set()
    root_real = os.path.realpath(root)

    for raw_path in paths:
        if not raw_path:
            continue
        abs_path = raw_path if os.path.isabs(raw_path) else os.path.join(root_real, raw_path)
        abs_path = os.path.realpath(abs_path)

        try:
            rel_path = os.path.relpath(abs_path, root_real)
            if rel_path == os.pardir or rel_path.startswith(os.pardir + os.sep):
                continue
        except ValueError:
            continue

        rel_parts = set(rel_path.replace(os.sep, "/").split("/"))
        if any(excluded in rel_parts for excluded in exclude_dirs):
            continue

        if include_globs and not any(_glob_matches_path(abs_path, root_real, glob) for glob in include_globs):
            continue

        filtered.add(abs_path)

    return filtered


def parse_search_query(query: str) -> tuple[str, list[str]]:
    """Parse a search query into mode and keyword list.

    Rules:
    - Contains ``|`` → OR mode: split on ``|``
    - Otherwise      → AND mode: split on whitespace

    Empty segments are discarded.  Each keyword is stripped of leading/trailing
    whitespace.

    Returns:
        ``(mode, keywords)`` where *mode* is ``"or"`` or ``"and"``.
    """
    stripped = query.strip()
    if "|" in stripped:
        keywords = [kw.strip() for kw in stripped.split("|") if kw.strip()]
        return ("or", keywords)
    else:
        keywords = [kw.strip() for kw in stripped.split() if kw.strip()]
        return ("and", keywords)


def search_files(
    root: str,
    query: str,
    *,
    include: str = "**/*",
    exclude_dirs: Optional[list[str]] = None,
    fixed_strings: bool = True,
    timeout: int = 30,
) -> set[str]:
    """Search *root* for files matching *query* keywords.

    Uses ripgrep (``rg``) when available, falls back to ``grep``.

    Args:
        root: Directory to search (must be an existing directory).
        query: User query string — parsed via :func:`parse_search_query`
            into AND/OR keywords.
        include: Glob pattern(s) for files to search.
            Multiple patterns separated by ``|`` (e.g. ``"*.py|*.js"``).
            Default ``"**/*"`` (all files).
        exclude_dirs: Directory names to skip.
            Defaults to ``[".git", "node_modules", "dist"]``.
        fixed_strings: If *True* (default), treat keywords as literal strings
            (``--fixed-strings`` / ``-F``).  If *False*, treat as regex.
        timeout: Per-subprocess timeout in seconds.

    Returns:
        Set of absolute file paths that match.
    """
    root = os.path.realpath(root)
    if not os.path.isdir(root):
        return set()

    mode, keywords = parse_search_query(query)
    if not keywords:
        return set()

    if exclude_dirs is None:
        exclude_dirs = [".git", "node_modules", "dist"]

    rg = shutil.which("rg") or shutil.which("ripgrep")
    grep = shutil.which("grep") if not rg else None

    if not rg and not grep:
        return set()

    tool = rg or grep

    # ---- helpers ----------------------------------------------------------
    def _run(cmd: list[str]) -> set[str]:
        proc = _subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if proc.returncode not in (0, 1):          # 1 = no matches
            return set()
        return {line.strip() for line in (proc.stdout or "").splitlines() if line.strip()}

    # ---- build common flags ----------------------------------------------
    globs = _split_glob_patterns(include)
    grep_include_args = _grep_include_args(globs)
    result: set[str] = set()

    try:
        if mode == "or":
            # OR: single command, regex alternation pattern
            if rg:
                escaped = [re.escape(kw) if fixed_strings else kw for kw in keywords]
                pattern = "|".join(escaped)
                cmd = [tool, "--files-with-matches"]
                for g in globs:
                    cmd += ["--glob", g]
                for d in exclude_dirs:
                    cmd += ["--glob", f"!{d}"]
                cmd += [pattern, root]
            else:
                pattern = "|".join(
                    re.escape(kw) if fixed_strings else kw for kw in keywords
                )
                # Use -r (not -R): grep -R follows symlinks, unlike rg, and
                # can walk huge external trees such as workspace symlinks.
                cmd = [tool, "-r", "-l", "-I", "-E"]
                cmd += grep_include_args
                for d in exclude_dirs:
                    cmd += [f"--exclude-dir={d}"]
                cmd += ["-e", pattern, root]
            result = _filter_search_paths(_run(cmd), root, globs, exclude_dirs)

        else:
            # AND: iterative narrowing — first keyword narrows scope,
            # subsequent keywords refine the already-matched set.
            for i, kw in enumerate(keywords):
                if i == 0:
                    if rg:
                        cmd = [tool, "--files-with-matches"]
                        for g in globs:
                            cmd += ["--glob", g]
                        for d in exclude_dirs:
                            cmd += ["--glob", f"!{d}"]
                        if fixed_strings:
                            cmd += ["--fixed-strings"]
                        cmd += ["-e", kw, root]
                    else:
                        # Use -r (not -R) to avoid following workspace symlinks.
                        cmd = [tool, "-r", "-l", "-I"]
                        if fixed_strings:
                            cmd += ["-F"]
                        cmd += grep_include_args
                        for d in exclude_dirs:
                            cmd += [f"--exclude-dir={d}"]
                        cmd += ["-e", kw, root]
                    result = _filter_search_paths(_run(cmd), root, globs, exclude_dirs)
                else:
                    if not result:
                        break
                    if rg:
                        cmd = [tool, "--files-with-matches"]
                        if fixed_strings:
                            cmd += ["--fixed-strings"]
                        cmd += ["-e", kw] + sorted(result)
                    else:
                        cmd = [tool, "-l", "-I"]
                        if fixed_strings:
                            cmd += ["-F"]
                        cmd += ["-e", kw] + sorted(result)
                    result = _run(cmd)

    except _subprocess.TimeoutExpired:
        return set()
    except Exception:
        return set()

    return _filter_search_paths(result, root, globs, exclude_dirs)


# ---------------------------------------------------------------------------
# Process management utilities
# ---------------------------------------------------------------------------


def kill_process_group(proc: subprocess.Popen) -> None:
    """Kill entire process group/tree.
    
    On Unix: sends SIGKILL to the process group.
    On Windows: uses taskkill /T /F to kill the process tree.
    Falls back to proc.kill() if group kill fails.
    
    Args:
        proc: The subprocess.Popen object to kill.
    """
    if sys.platform == "win32":
        # Windows: use taskkill to kill process tree
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return
        except (subprocess.SubprocessError, OSError):
            pass
    else:
        # Unix: send SIGKILL to process group
        if hasattr(signal, "SIGKILL"):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                return
            except (ProcessLookupError, OSError):
                pass
    proc.kill()


def get_system_encoding() -> str:
    """Get the preferred system encoding for subprocess output.

    Returns the system's preferred encoding for text I/O, with fallback to utf-8.
    On Windows this is typically the OEM codepage (e.g. cp936/GBK for Chinese locales),
    which matches what cmd.exe and PowerShell emit by default.
    """
    encoding = locale.getpreferredencoding(False)
    if encoding:
        return encoding
    return "utf-8"


# Resolve once at module load time so every subprocess call uses the same codec.
SYSTEM_ENCODING: str = get_system_encoding()
