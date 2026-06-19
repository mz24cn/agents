"""Workspace File Manager for the Agent Service.

Provides WorkspaceManager, a class for managing workspace files and directories.
Supports listing, searching, previewing, downloading, and managing files in the workspace.
Also provides utilities for expanding workspace file references (<file>...</file>) in user prompts.
"""

import os
import re
import shutil
import mimetypes
import math
import tempfile
import uuid
from pathlib import Path
from typing import Optional, List, Dict, Any, BinaryIO, Iterable
import logging

from runtime.models import Message

logger = logging.getLogger("runtime.workspace_manager")


DEFAULT_UPLOAD_PARALLEL_SIZE = 100 * 1024 * 1024
DEFAULT_UPLOAD_PARALLEL_MAX_THREADS = 5
UPLOAD_READ_BUFFER_SIZE = 1024 * 1024

# ---------------------------------------------------------------------------
# Shared file-type constants & helpers (used by listing, search, and refs)
# ---------------------------------------------------------------------------

_TEXT_EXTENSIONS: set[str] = {
    ".txt", ".md", ".markdown", ".html", ".htm", ".css", ".js", ".ts",
    ".jsx", ".tsx", ".py", ".rb", ".java", ".c", ".cpp", ".h", ".hpp",
    ".go", ".rs", ".php", ".swift", ".kt", ".scala", ".r", ".m", ".mm",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".log",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd", ".sql",
    ".xml", ".svg", ".csv", ".tsv", ".env", ".gitignore", ".dockerignore",
    ".editorconfig", ".prettierrc", ".eslintrc", ".babelrc",
}

_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_TYPES = {
    "application/json", "application/xml", "application/javascript",
    "application/x-yaml", "application/yaml",
}


def is_text_file(path: str) -> bool:
    """Return True if *path* looks like a text file (by MIME type or extension)."""
    mime_type, _ = mimetypes.guess_type(path)
    if mime_type and (mime_type.startswith(_TEXT_MIME_PREFIXES) or mime_type in _TEXT_MIME_TYPES):
        return True
    return os.path.splitext(path)[1].lower() in _TEXT_EXTENSIONS


def is_image_file(path: str) -> bool:
    mime_type, _ = mimetypes.guess_type(path)
    return bool(mime_type and mime_type.startswith("image/"))


def classify_file(path: str) -> dict:
    """Return ``{is_text, is_image, is_audio, is_video}`` flags for *path*."""
    mime_type, _ = mimetypes.guess_type(path)
    flags = {"is_text": False, "is_image": False, "is_audio": False, "is_video": False}
    if mime_type:
        if mime_type.startswith(_TEXT_MIME_PREFIXES) or mime_type in _TEXT_MIME_TYPES:
            flags["is_text"] = True
        elif mime_type.startswith("image/"):
            flags["is_image"] = True
        elif mime_type.startswith("audio/"):
            flags["is_audio"] = True
        elif mime_type.startswith("video/"):
            flags["is_video"] = True
    if not any(flags.values()) and os.path.splitext(path)[1].lower() in _TEXT_EXTENSIONS:
        flags["is_text"] = True
    return flags


# ---------------------------------------------------------------------------
# Upload size parsing
# ---------------------------------------------------------------------------


def parse_upload_size(value: Optional[str], default: int = DEFAULT_UPLOAD_PARALLEL_SIZE) -> int:
    if value is None:
        return default
    text = str(value).strip().upper()
    if not text:
        return default

    multipliers = {
        'K': 1024,
        'KB': 1024,
        'M': 1024 ** 2,
        'MB': 1024 ** 2,
        'G': 1024 ** 3,
        'GB': 1024 ** 3,
    }

    number = text
    multiplier = 1
    for suffix in sorted(multipliers, key=len, reverse=True):
        if text.endswith(suffix):
            number = text[:-len(suffix)].strip()
            multiplier = multipliers[suffix]
            break

    try:
        parsed = int(number) * multiplier
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def parse_upload_max_threads(value: Optional[str], default: int = DEFAULT_UPLOAD_PARALLEL_MAX_THREADS) -> int:
    try:
        parsed = int(str(value).strip()) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(1, min(32, parsed))


class WorkspaceManager:
    """Manages workspace files and directories."""
    
    def __init__(self, workspace_path: str):
        """Initialize with workspace root path."""
        self.workspace_path = os.path.realpath(workspace_path)
        if not os.path.isdir(self.workspace_path):
            raise ValueError(f"Workspace path does not exist: {workspace_path}")
    
    def list_files(
        self,
        path: str,
        page: int = 1,
        page_size: int = 50,
        restrict_workspace: bool = True,
        sort: str = "name",
        name_filter: str = "",
    ) -> Dict[str, Any]:
        """List files and directories in the given path.
        
        Args:
            path: Directory path relative to workspace or absolute path
            page: Page number (1-based)
            page_size: Number of items per page
            restrict_workspace: If True, restrict to workspace path
            sort: Sort mode. ``"name"`` sorts alphabetically (directories first),
                ``"recent"`` sorts by modification time descending.
            name_filter: Case-insensitive substring filter applied to item names in
                this directory before pagination.
            
        Returns:
            Dictionary with 'files' list and 'has_more' boolean
        """
        # Resolve path (use abspath to preserve symlinks)
        if os.path.isabs(path):
            dir_path = os.path.abspath(path)
        else:
            dir_path = os.path.abspath(os.path.join(self.workspace_path, path))
        
        # Security check: use realpath to verify actual location (if restricted)
        if restrict_workspace:
            real_dir = os.path.realpath(dir_path)
            if not real_dir.startswith(self.workspace_path):
                raise ValueError("Access denied: path is outside workspace")
        
        if not os.path.isdir(dir_path):
            raise ValueError(f"Directory does not exist: {dir_path}")
        
        try:
            # Get all items in directory
            items = []
            for item_name in os.listdir(dir_path):
                item_path = os.path.join(dir_path, item_name)
                try:
                    stat = os.stat(item_path)
                    is_dir = os.path.isdir(item_path)
                    
                    # Determine file type
                    flags = classify_file(item_path) if not is_dir else {
                        'is_text': False, 'is_image': False, 'is_audio': False, 'is_video': False
                    }
                    
                    entry = {
                        'name': item_name,
                        'path': item_path,
                        'is_dir': is_dir,
                        'size': stat.st_size if not is_dir else 0,
                        'modified': int(stat.st_mtime * 1000),
                        **flags,
                    }
                    if os.path.islink(item_path):
                        entry['symlink_target'] = os.readlink(item_path)
                    items.append(entry)
                except (OSError, PermissionError):
                    # Skip items we can't access
                    continue
            
            filter_text = (name_filter or '').strip().lower()
            if filter_text:
                items = [item for item in items if filter_text in item['name'].lower()]

            if sort == 'recent':
                # Newest first across files and directories; use name as a stable tie-breaker.
                items.sort(key=lambda x: (-(x.get('modified') or 0), x['name'].lower()))
            else:
                # Default: directories first, then alphabetical by name.
                items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
            
            # Apply pagination
            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            paginated_items = items[start_idx:end_idx]
            has_more = end_idx < len(items)
            
            return {
                'files': paginated_items,
                'has_more': has_more,
                'total': len(items)
            }
        except PermissionError:
            raise ValueError(f"Permission denied: {dir_path}")
    
    def list_children(self, path: str = '') -> List[Dict[str, Any]]:
        """List child directories of any path. No workspace restriction.
        
        If path is empty, returns root directories:
        - On Windows: drive letters (C:\\, D:\\, etc.)
        - On Unix: ['/']
        
        Args:
            path: Directory path to list children of (empty = roots)
            
        Returns:
            List of dicts with 'name' key, and optional 'symlink_target' for symlinks
        """
        if not path:
            # Return root directories
            if os.name == 'nt':
                # Windows: list drive letters
                import string
                roots = []
                for drive in string.ascii_uppercase:
                    drive_path = f"{drive}:\\"
                    if os.path.isdir(drive_path):
                        roots.append({
                            'name': drive_path,
                        })
                return roots
            else:
                return [{'name': '/'}]
        
        real_path = os.path.realpath(path)
        if not os.path.isdir(real_path):
            raise ValueError(f"Not a directory: {path}")
        
        children = []
        try:
            for item_name in sorted(os.listdir(real_path)):
                item_path = os.path.join(real_path, item_name)
                if os.path.isdir(item_path) and not item_name.startswith('.'):
                    entry = {
                        'name': item_name,
                    }
                    if os.path.islink(item_path):
                        entry['symlink_target'] = os.readlink(item_path)
                    children.append(entry)
        except PermissionError:
            pass
        
        return children
    
    def search_files(self, path: str, query: str, restrict_workspace: bool = True) -> List[Dict[str, Any]]:
        """Search for files matching the query."""
        if os.path.isabs(path):
            search_path = os.path.abspath(path)
        else:
            search_path = os.path.abspath(os.path.join(self.workspace_path, path))
        # Security check: use realpath to verify actual location
        if restrict_workspace:
            real_search = os.path.realpath(search_path)
            if not real_search.startswith(self.workspace_path):
                raise ValueError("Access denied: path is outside workspace")
        
        # Use common.py search function
        from runtime.common import search_files
        try:
            matched_paths = search_files(search_path, query)
            
            # Convert to file info objects
            results = []
            for file_path in matched_paths:
                try:
                    stat = os.stat(file_path)
                    is_dir = os.path.isdir(file_path)
                    
                    flags = classify_file(file_path) if not is_dir else {
                        'is_text': False, 'is_image': False, 'is_audio': False, 'is_video': False
                    }
                    
                    entry = {
                        'name': os.path.basename(file_path),
                        'path': file_path,
                        'is_dir': is_dir,
                        'size': stat.st_size if not is_dir else 0,
                        'modified': int(stat.st_mtime * 1000),
                        **flags,
                    }
                    if os.path.islink(file_path):
                        entry['symlink_target'] = os.readlink(file_path)
                    results.append(entry)
                except (OSError, PermissionError):
                    continue
            
            return results
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return []
    
    def get_file_content(self, path: str, restrict_workspace: bool = True) -> bytes:
        """Get file content for preview or download."""
        if os.path.isabs(path):
            file_path = os.path.abspath(path)
        else:
            file_path = os.path.abspath(os.path.join(self.workspace_path, path))
        # Security check: use realpath to verify actual location
        if restrict_workspace:
            real_path = os.path.realpath(file_path)
            if not real_path.startswith(self.workspace_path):
                raise ValueError("Access denied: path is outside workspace")
        if not os.path.isfile(file_path):
            raise ValueError(f"File does not exist: {file_path}")
        try:
            with open(file_path, 'rb') as f:
                return f.read()
        except PermissionError:
            raise ValueError(f"Permission denied: {file_path}")
    
    def get_file_info(self, path: str, restrict_workspace: bool = True) -> Dict[str, Any]:
        """Get file information."""
        if os.path.isabs(path):
            file_path = os.path.abspath(path)
        else:
            file_path = os.path.abspath(os.path.join(self.workspace_path, path))
        # Security check: use realpath to verify actual location
        if restrict_workspace:
            real_path = os.path.realpath(file_path)
            if not real_path.startswith(self.workspace_path):
                raise ValueError("Access denied: path is outside workspace")
        if not os.path.exists(file_path):
            raise ValueError(f"Path does not exist: {file_path}")
        try:
            stat = os.stat(file_path)
            is_dir = os.path.isdir(file_path)
            name = os.path.basename(file_path)
            mime_type = None
            if not is_dir:
                mime_type, _ = mimetypes.guess_type(file_path)
            entry = {
                'name': name,
                'path': file_path,
                'is_dir': is_dir,
                'size': stat.st_size if not is_dir else 0,
                'modified': int(stat.st_mtime * 1000),
                'mime_type': mime_type
            }
            if os.path.islink(file_path):
                entry['symlink_target'] = os.readlink(file_path)
            return entry
        except PermissionError:
            raise ValueError(f"Permission denied: {file_path}")
    
    def rename_file(self, path: str, new_name: str, restrict_workspace: bool = True) -> Dict[str, Any]:
        """Rename a file or directory."""
        if os.path.isabs(path):
            old_path = os.path.realpath(path)
        else:
            old_path = os.path.realpath(os.path.join(self.workspace_path, path))
        if restrict_workspace and not old_path.startswith(self.workspace_path):
            raise ValueError("Access denied: path is outside workspace")
        if not os.path.exists(old_path):
            raise ValueError(f"Path does not exist: {old_path}")
        if '/' in new_name or '\\' in new_name or ':' in new_name:
            raise ValueError("Invalid file name")
        new_path = os.path.join(os.path.dirname(old_path), new_name)
        if os.path.exists(new_path):
            raise ValueError(f"Target already exists: {new_name}")
        try:
            os.rename(old_path, new_path)
            return self.get_file_info(new_path, restrict_workspace=False)
        except PermissionError:
            raise ValueError(f"Permission denied: {old_path}")
    
    def duplicate_file(self, path: str, restrict_workspace: bool = True) -> Dict[str, Any]:
        """Create a duplicate of a file."""
        if os.path.isabs(path):
            src_path = os.path.realpath(path)
        else:
            src_path = os.path.realpath(os.path.join(self.workspace_path, path))
        if restrict_workspace and not src_path.startswith(self.workspace_path):
            raise ValueError("Access denied: path is outside workspace")
        if not os.path.exists(src_path):
            raise ValueError(f"Path does not exist: {src_path}")
        dir_name = os.path.dirname(src_path)
        base_name = os.path.basename(src_path)
        name, ext = os.path.splitext(base_name)
        counter = 1
        while True:
            new_name = f"{name} ({counter}){ext}"
            new_path = os.path.join(dir_name, new_name)
            if not os.path.exists(new_path):
                break
            counter += 1
        try:
            if os.path.isdir(src_path):
                shutil.copytree(src_path, new_path)
            else:
                shutil.copy2(src_path, new_path)
            return self.get_file_info(new_path, restrict_workspace=False)
        except PermissionError:
            raise ValueError(f"Permission denied: {src_path}")
    
    def delete_file(self, path: str, restrict_workspace: bool = True) -> bool:
        """Delete a file or directory."""
        if os.path.isabs(path):
            file_path = os.path.realpath(path)
        else:
            file_path = os.path.realpath(os.path.join(self.workspace_path, path))
        if restrict_workspace and not file_path.startswith(self.workspace_path):
            raise ValueError("Access denied: path is outside workspace")
        if not os.path.exists(file_path):
            raise ValueError(f"Path does not exist: {file_path}")
        if file_path == self.workspace_path:
            raise ValueError("Cannot delete workspace root")
        try:
            if os.path.isdir(file_path):
                shutil.rmtree(file_path)
            else:
                os.remove(file_path)
            return True
        except PermissionError:
            raise ValueError(f"Permission denied: {file_path}")

    def _resolve_path(self, path: str, restrict_workspace: bool = True) -> str:
        """Resolve a path to absolute, optionally checking workspace restriction."""
        if os.path.isabs(path):
            resolved = os.path.realpath(path)
        else:
            resolved = os.path.realpath(os.path.join(self.workspace_path, path))
        if restrict_workspace and not resolved.startswith(self.workspace_path):
            raise ValueError("Access denied: path is outside workspace")
        return resolved

    def move_files(self, paths: list, dest_dir: str, restrict_workspace: bool = True, overwrite: bool = False) -> Dict[str, Any]:
        """Move multiple files/directories to a destination directory.
        
        Returns dict with 'moved' list and 'errors' list.
        """
        dest_path = self._resolve_path(dest_dir, restrict_workspace)
        if not os.path.isdir(dest_path):
            raise ValueError(f"Destination is not a directory: {dest_dir}")

        moved = []
        errors = []
        
        for src in paths:
            try:
                src_path = self._resolve_path(src, restrict_workspace)
                if not os.path.exists(src_path):
                    errors.append({"path": src, "error": f"Path does not exist: {src}"})
                    continue
                
                # Prevent moving a directory into itself
                if os.path.isdir(src_path) and dest_path.startswith(src_path + os.sep):
                    errors.append({"path": src, "error": "Cannot move a directory into itself"})
                    continue
                
                base_name = os.path.basename(src_path)
                new_path = os.path.join(dest_path, base_name)
                
                if os.path.exists(new_path):
                    if not overwrite:
                        errors.append({"path": src, "error": f"Target already exists: {base_name}", "conflict": True})
                        continue
                # Remove existing before move
                    if os.path.isdir(new_path):
                        shutil.rmtree(new_path)
                    else:
                        os.remove(new_path)
                
                shutil.move(src_path, new_path)
                moved.append(self.get_file_info(new_path, restrict_workspace=False))
            except ValueError as e:
                errors.append({"path": src, "error": str(e)})
            except PermissionError:
                errors.append({"path": src, "error": f"Permission denied: {src}"})
            except Exception as e:
                errors.append({"path": src, "error": str(e)})
        
        return {"moved": moved, "errors": errors}

    def copy_files(self, paths: list, dest_dir: str, restrict_workspace: bool = True, overwrite: bool = False) -> Dict[str, Any]:
        """Copy multiple files/directories to a destination directory.
        
        Returns dict with 'copied' list and 'errors' list.
        """
        dest_path = self._resolve_path(dest_dir, restrict_workspace)
        if not os.path.isdir(dest_path):
            raise ValueError(f"Destination is not a directory: {dest_dir}")

        copied = []
        errors = []
        
        for src in paths:
            try:
                src_path = self._resolve_path(src, restrict_workspace)
                if not os.path.exists(src_path):
                    errors.append({"path": src, "error": f"Path does not exist: {src}"})
                    continue
                
                base_name = os.path.basename(src_path)
                new_path = os.path.join(dest_path, base_name)
                
                if os.path.exists(new_path):
                    if not overwrite:
                        errors.append({"path": src, "error": f"Target already exists: {base_name}", "conflict": True})
                        continue
                    # Remove existing before copy
                    if os.path.isdir(new_path):
                        shutil.rmtree(new_path)
                    else:
                        os.remove(new_path)
                
                if os.path.isdir(src_path):
                    shutil.copytree(src_path, new_path)
                else:
                    shutil.copy2(src_path, new_path)
                copied.append(self.get_file_info(new_path, restrict_workspace=False))
            except ValueError as e:
                errors.append({"path": src, "error": str(e)})
            except PermissionError:
                errors.append({"path": src, "error": f"Permission denied: {src}"})
            except Exception as e:
                errors.append({"path": src, "error": str(e)})
        
        return {"copied": copied, "errors": errors}

    def resolve_upload_target(self, target_path: str) -> str:
        if not isinstance(target_path, str) or not target_path.strip():
            raise ValueError("INVALID_TARGET_PATH: target_path is required")

        normalized = target_path.replace('\\', '/')
        if os.path.isabs(normalized):
            raise ValueError("INVALID_TARGET_PATH: absolute paths are not allowed")

        parts = [part for part in normalized.split('/') if part]
        if not parts or any(part in {'.', '..'} for part in parts):
            raise ValueError("INVALID_TARGET_PATH: path traversal is not allowed")

        target_abs = os.path.realpath(os.path.join(self.workspace_path, *parts))
        if os.path.commonpath([self.workspace_path, target_abs]) != self.workspace_path:
            raise ValueError("INVALID_TARGET_PATH: target is outside workspace")
        if target_abs == self.workspace_path:
            raise ValueError("INVALID_TARGET_PATH: target cannot be workspace root")
        return target_abs

    def create_upload_task(self, file_name: str, file_size: int, target_path: str, parallel_size: int, parallel_max_threads: int) -> Dict[str, Any]:
        if not isinstance(file_name, str) or not file_name.strip():
            raise ValueError("INVALID_REQUEST: file_name is required")
        try:
            file_size = int(file_size)
        except (TypeError, ValueError):
            raise ValueError("INVALID_REQUEST: file_size must be a number")
        if file_size < 0:
            raise ValueError("INVALID_REQUEST: file_size cannot be negative")

        target_abs = self.resolve_upload_target(target_path)
        if os.path.isdir(target_abs):
            raise ValueError("INVALID_TARGET_PATH: target is a directory")

        if file_size == 0:
            chunk_count = 0
            chunks: List[Dict[str, Any]] = []
        elif file_size <= parallel_size:
            chunk_count = 1
            chunks = [{"parallel_id": 0, "offset": 0, "size": file_size, "status": "pending"}]
        else:
            chunk_count = min(parallel_max_threads, math.ceil(file_size / parallel_size))
            chunk_size = math.ceil(file_size / chunk_count)
            chunks = []
            for parallel_id in range(chunk_count):
                offset = parallel_id * chunk_size
                size = min(chunk_size, file_size - offset)
                if size > 0:
                    chunks.append({"parallel_id": parallel_id, "offset": offset, "size": size, "status": "pending"})
            chunk_count = len(chunks)

        upload_id = uuid.uuid4().hex
        return {
            "upload_id": upload_id,
            "workspace_id": "default",
            "file_name": file_name,
            "file_size": file_size,
            "target_path": target_abs,
            "path": target_path,
            "parallel_size": parallel_size,
            "parallel_max_threads": parallel_max_threads,
            "chunk_count": chunk_count,
            "chunks": chunks,
            "status": "initialized",
        }

    def upload_chunk_path(self, upload_id: str, parallel_id: int, part: bool = False) -> str:
        suffix = '.part' if part else ''
        return os.path.join(tempfile.gettempdir(), f"{upload_id}-{parallel_id}{suffix}")

    def write_upload_chunk(self, upload_id: str, parallel_id: int, stream: BinaryIO, expected_size: int) -> int:
        part_path = self.upload_chunk_path(upload_id, parallel_id, part=True)
        final_path = self.upload_chunk_path(upload_id, parallel_id)
        remaining = expected_size
        received = 0

        with open(part_path, 'wb') as f:
            while remaining > 0:
                block = stream.read(min(UPLOAD_READ_BUFFER_SIZE, remaining))
                if not block:
                    raise ConnectionError("CHUNK_UPLOAD_INTERRUPTED: client disconnected")
                f.write(block)
                received += len(block)
                remaining -= len(block)
            f.flush()
            os.fsync(f.fileno())

        os.replace(part_path, final_path)
        return received

    def complete_upload_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        target_path = task['target_path']
        target_dir = os.path.dirname(target_path)
        os.makedirs(target_dir, exist_ok=True)
        merge_path = f"{target_path}.uploading-{task['upload_id']}"

        try:
            with open(merge_path, 'wb') as out:
                for chunk in sorted(task['chunks'], key=lambda item: item['parallel_id']):
                    chunk_path = self.upload_chunk_path(task['upload_id'], chunk['parallel_id'])
                    if not os.path.exists(chunk_path):
                        raise ValueError("UPLOAD_NOT_READY: missing chunk")
                    if os.path.getsize(chunk_path) != chunk['size']:
                        raise ValueError("CHUNK_SIZE_MISMATCH: chunk size mismatch")
                    with open(chunk_path, 'rb') as inp:
                        while True:
                            data = inp.read(UPLOAD_READ_BUFFER_SIZE)
                            if not data:
                                break
                            out.write(data)
                out.flush()
                os.fsync(out.fileno())

            if os.path.getsize(merge_path) != task['file_size']:
                raise ValueError("CHUNK_SIZE_MISMATCH: merged file size mismatch")
            os.replace(merge_path, target_path)
            self.cleanup_upload_temp(task['upload_id'], task)
            return {
                "upload_id": task['upload_id'],
                "file_name": task['file_name'],
                "file_size": task['file_size'],
                "path": task['path'],
                "status": "completed",
            }
        except Exception:
            try:
                if os.path.exists(merge_path):
                    os.remove(merge_path)
            except OSError:
                pass
            raise

    def cleanup_upload_temp(self, upload_id: str, task: Optional[Dict[str, Any]] = None) -> None:
        chunk_ids = []
        if task is not None:
            chunk_ids = [chunk['parallel_id'] for chunk in task.get('chunks', [])]
        else:
            chunk_ids = range(0, 1024)

        for parallel_id in chunk_ids:
            for part in (False, True):
                path = self.upload_chunk_path(upload_id, parallel_id, part=part)
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass

        if task is not None:
            merge_path = f"{task['target_path']}.uploading-{upload_id}"
            try:
                if os.path.exists(merge_path):
                    os.remove(merge_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Workspace file reference expansion (<file>...</file> tags in prompts)
# ---------------------------------------------------------------------------

_FILE_REF_RE = re.compile(r"<file>\s*([^<]+?)\s*</file>")


def _resolve_workspace_file(ref_path: str, workspace: str) -> str:
    """Resolve a <file> path and ensure it is inside the given workspace."""
    raw = ref_path.strip()
    if not raw:
        raise ValueError("Empty workspace file reference")

    if os.path.isabs(raw):
        file_path = os.path.realpath(raw)
    else:
        file_path = os.path.realpath(os.path.join(workspace, raw.lstrip("/\\")))

    workspace_real = os.path.realpath(workspace)
    if os.path.commonpath([workspace_real, file_path]) != workspace_real:
        raise ValueError(f"Access denied: file reference is outside workspace: {ref_path}")
    if not os.path.isfile(file_path):
        raise ValueError(f"Referenced file does not exist: {ref_path}")
    return file_path


def _read_text_file(path: str) -> str:
    raw = open(path, "rb").read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def expand_workspace_file_refs_in_message(message: Message, workspace: str) -> Message:
    """Expand <file> tags inside one user message in-place and return it.

    Deduplication logic:
    - Path descriptions (e.g. [Text file attached: ...]) appear at every occurrence
    - File content (code blocks for text, base64 for images) is only added once
    - All content attachments are appended at the end of the message
    """
    if message.role != "user" or not message.content or "<file>" not in message.content:
        return message

    images = list(message.images or [])
    # Track processed files to avoid duplicating content
    processed_files: set[str] = set()
    # Collect content attachments (text file code blocks) to append at the end
    attachments: list[str] = []

    def replace(match: re.Match) -> str:
        file_path = match.group(1).strip()

        if os.path.isabs(file_path):
            file_path = os.path.realpath(file_path)
        else:
            file_path = os.path.realpath(os.path.join(workspace, file_path.lstrip("/\\")))

        if not os.path.isfile(file_path):
            raise ValueError(f"Referenced file does not exist: {file_path}")

        if is_image_file(file_path):
            # Image content (base64) only added once
            if file_path not in processed_files:
                images.append(file_path)
                processed_files.add(file_path)
            return f"[Image file attached: {file_path}]"
        if is_text_file(file_path):
            # Text content (code block) only added once
            if file_path not in processed_files:
                content = _read_text_file(file_path)
                attachments.append(f"[Text file attached: {file_path}]\n```\n{content}\n```")
                processed_files.add(file_path)
            return f"[Text file attached: {file_path}]"
        # Unsupported file type: return path reference instead of raising error
        return f"[file attached: {file_path}]"

    message.content = _FILE_REF_RE.sub(replace, message.content)
    
    # Prepend text file content attachments at the beginning
    # so user's text appears at the end for better inference
    if attachments:
        message.content = "\n\n".join(attachments) + "\n\n" + message.content.lstrip()
    
    message.images = images or None
    return message


def expand_workspace_file_refs(messages: Iterable[Message] | None, workspace: str) -> list[Message] | None:
    """Expand workspace file references in user messages."""
    if messages is None:
        return None
    expanded = list(messages)
    for msg in expanded:
        expand_workspace_file_refs_in_message(msg, workspace)
    return expanded

