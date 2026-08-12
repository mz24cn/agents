"""Workspace + upload handler mixin.

Part of the ``_RuntimeRequestHandler`` decomposition in ``runtime.server``.
Provides the workspace browsing/search/content/download endpoints, the
file/directory mutation endpoints (rename/mkdir/duplicate/move/copy/delete)
and the chunked upload pipeline.

Zero third-party dependencies — only Python standard library.
"""

import logging
import os
import threading
import urllib.parse
from typing import Optional

logger = logging.getLogger("runtime.server")


class HandlerWorkspaceMixin:
    def _handle_workspace_list(self) -> None:
        """GET /v1/workspace/list — list files in workspace directory."""
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            
            path = params.get('path', [''])[0]
            page = int(params.get('page', ['1'])[0])
            page_size = int(params.get('page_size', ['50'])[0])
            restrict = params.get('restrict', ['1'])[0] != '0'
            sort = params.get('sort', ['name'])[0]
            name_filter = params.get('name_filter', [''])[0]
            
            if not path:
                self._send_json_error(400, "Missing 'path' parameter")
                return
            
            workspace_mgr = self._get_workspace_manager()
            result = workspace_mgr.list_files(
                path,
                page,
                page_size,
                restrict_workspace=restrict,
                sort=sort,
                name_filter=name_filter,
            )
            self._send_json_response(200, result)
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace list error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_children(self) -> None:
        """GET /v1/workspace/children — list child directories of any path (no workspace restriction)."""
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            path = params.get('path', [''])[0]
            
            workspace_mgr = self._get_workspace_manager()
            children = workspace_mgr.list_children(path)
            self._send_json_response(200, children)
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace children error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_search(self) -> None:
        """GET /v1/workspace/search — search files in workspace."""
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            
            path = params.get('path', [''])[0]
            query = params.get('query', [''])[0]
            name_filter = params.get('name_filter', [''])[0]
            
            if not path:
                self._send_json_error(400, "Missing 'path' parameter")
                return
            
            if not query and not name_filter:
                self._send_json_error(400, "Missing 'query' or 'name_filter' parameter")
                return
            
            workspace_mgr = self._get_workspace_manager()
            results = workspace_mgr.search_files(
                path,
                query,
                restrict_workspace=self._should_restrict_workspace(),
                name_filter=name_filter,
            )
            self._send_json_response(200, results)
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace search error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_content(self) -> None:
        """GET /v1/workspace/content — get file content for preview.

        Supports RFC 7233 byte ranges (206 Partial Content) so pdf.js can
        fetch large PDFs in chunks and <video>/<audio> can seek, plus
        zero-copy sendfile streaming for large files.
        """
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)

            path = params.get('path', [''])[0]
            restrict = params.get('restrict', ['1'])[0] != '0'

            if not path:
                self._send_json_error(400, "Missing 'path' parameter")
                return

            workspace_mgr = self._get_workspace_manager()
            file_info = workspace_mgr.get_file_info(path, restrict_workspace=restrict)
            content_type = file_info.get('mime_type') or 'application/octet-stream'
            file_path = file_info.get('path')
            if not file_path or not os.path.isfile(file_path):
                self._send_json_error(400, f"File does not exist: {path}")
                return
            try:
                stat_info = os.stat(file_path)
            except OSError:
                self._send_json_error(500, "Failed to stat file")
                return

            if self._serve_file_range(file_path, stat_info, content_type):
                return
            self._send_full_file(file_path, stat_info, content_type)
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace content error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_download(self) -> None:
        """GET /v1/workspace/download — download file.

        Supports RFC 7233 byte ranges (206 Partial Content) for resumable
        downloads and zero-copy sendfile streaming for large files.
        """
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)

            path = params.get('path', [''])[0]
            restrict = params.get('restrict', ['1'])[0] != '0'

            if not path:
                self._send_json_error(400, "Missing 'path' parameter")
                return

            workspace_mgr = self._get_workspace_manager()
            file_info = workspace_mgr.get_file_info(path, restrict_workspace=restrict)
            content_type = file_info.get('mime_type') or 'application/octet-stream'
            file_name = file_info.get('name', 'download')
            file_path = file_info.get('path')
            if not file_path or not os.path.isfile(file_path):
                self._send_json_error(400, f"File does not exist: {path}")
                return
            try:
                stat_info = os.stat(file_path)
            except OSError:
                self._send_json_error(500, "Failed to stat file")
                return

            disposition = self._content_disposition('attachment', file_name)
            if self._serve_file_range(
                file_path, stat_info, content_type, disposition=disposition
            ):
                return
            self._send_full_file(
                file_path, stat_info, content_type, disposition=disposition
            )
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace download error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_thumbnail(self) -> None:
        """GET /v1/workspace/thumbnail — get image thumbnail."""
        # For now, just serve the original image
        # TODO: Implement actual thumbnail generation
        self._handle_workspace_content()

    def _handle_workspace_paste_dir(self) -> None:
        """GET /v1/workspace/paste-dir — resolve the clipboard paste directory.

        Returns the directory the chat input should upload pasted files
        (images / PDF / DOCX ...) into.  Linux: ``/tmp``; Windows: the OS temp
        dir (or ``<workspace drive>:\\tmp`` fallback).
        """
        try:
            from runtime.workspace_manager import get_paste_directory
            workspace_mgr = self._get_workspace_manager()
            path = get_paste_directory(workspace_mgr.workspace_path)
            self._send_json_response(200, {"path": path})
        except Exception as e:
            logger.error(f"Workspace paste dir error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_rename(self) -> None:
        """POST /v1/workspace/rename — rename a file or directory."""
        try:
            body = self._read_json_body()
            if body is None:
                return
            
            path = body.get('path')
            new_name = body.get('new_name')
            
            if not path:
                self._send_json_error(400, "Missing 'path' field")
                return
            
            if not new_name:
                self._send_json_error(400, "Missing 'new_name' field")
                return
            
            workspace_mgr = self._get_workspace_manager()
            result = workspace_mgr.rename_file(path, new_name, restrict_workspace=self._should_restrict_workspace())
            self._send_json_response(200, result)
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace rename error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_mkdir(self) -> None:
        """POST /v1/workspace/mkdir — create a directory."""
        try:
            body = self._read_json_body()
            if body is None:
                return

            parent_path = body.get('parent_path')
            name = body.get('name')

            if not parent_path:
                self._send_json_error(400, "Missing 'parent_path' field")
                return

            if not name:
                self._send_json_error(400, "Missing 'name' field")
                return

            workspace_mgr = self._get_workspace_manager()
            result = workspace_mgr.create_directory(parent_path, name, restrict_workspace=self._should_restrict_workspace())
            self._send_json_response(200, result)
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace mkdir error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_duplicate(self) -> None:
        """POST /v1/workspace/duplicate — create a duplicate of a file."""
        try:
            body = self._read_json_body()
            if body is None:
                return
            
            path = body.get('path')
            
            if not path:
                self._send_json_error(400, "Missing 'path' field")
                return
            
            workspace_mgr = self._get_workspace_manager()
            result = workspace_mgr.duplicate_file(path, restrict_workspace=self._should_restrict_workspace())
            self._send_json_response(200, result)
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace duplicate error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_move(self) -> None:
        """POST /v1/workspace/move — move multiple files/directories to a destination."""
        try:
            body = self._read_json_body()
            if body is None:
                return
            
            paths = body.get('paths')
            dest_dir = body.get('dest_dir')
            overwrite = body.get('overwrite', False)
            
            if not paths or not isinstance(paths, list):
                self._send_json_error(400, "Missing or invalid 'paths' field (must be array)")
                return
            
            if not dest_dir:
                self._send_json_error(400, "Missing 'dest_dir' field")
                return
            
            workspace_mgr = self._get_workspace_manager()
            result = workspace_mgr.move_files(paths, dest_dir, restrict_workspace=self._should_restrict_workspace(), overwrite=overwrite)
            self._send_json_response(200, result)
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace move error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_copy(self) -> None:
        """POST /v1/workspace/copy — copy multiple files/directories to a destination."""
        try:
            body = self._read_json_body()
            if body is None:
                return
            
            paths = body.get('paths')
            dest_dir = body.get('dest_dir')
            overwrite = body.get('overwrite', False)
            
            if not paths or not isinstance(paths, list):
                self._send_json_error(400, "Missing or invalid 'paths' field (must be array)")
                return
            
            if not dest_dir:
                self._send_json_error(400, "Missing 'dest_dir' field")
                return
            
            workspace_mgr = self._get_workspace_manager()
            result = workspace_mgr.copy_files(paths, dest_dir, restrict_workspace=self._should_restrict_workspace(), overwrite=overwrite)
            self._send_json_response(200, result)
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace copy error: {e}")
            self._send_json_error(500, "Internal server error")

    def _handle_workspace_delete(self) -> None:
        """DELETE /v1/workspace/delete — delete a file or directory."""
        try:
            body = self._read_json_body()
            if body is None:
                return
            
            path = body.get('path')
            
            if not path:
                self._send_json_error(400, "Missing 'path' field")
                return
            
            workspace_mgr = self._get_workspace_manager()
            workspace_mgr.delete_file(path, restrict_workspace=self._should_restrict_workspace())
            self._send_json_response(200, {"status": "deleted", "path": path})
        except ValueError as e:
            self._send_json_error(400, str(e))
        except Exception as e:
            logger.error(f"Workspace delete error: {e}")
            self._send_json_error(500, "Internal server error")

    def _upload_error_status(self, message: str) -> int:
        if message.startswith("UPLOAD_NOT_FOUND") or message.startswith("CHUNK_NOT_FOUND"):
            return 404
        if message.startswith("UPLOAD_NOT_READY") or message.startswith("UPLOAD_CANCELLED"):
            return 409
        return 400

    def _get_workspace_upload_state(self):
        if not hasattr(self.server, 'workspace_uploads'):
            self.server.workspace_uploads = {}
        if not hasattr(self.server, 'workspace_uploads_lock'):
            self.server.workspace_uploads_lock = threading.Lock()
        return self.server.workspace_uploads, self.server.workspace_uploads_lock

    def _upload_header_int(self, name: str) -> Optional[int]:
        value = self.headers.get(name)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"CHUNK_SIZE_MISMATCH: invalid {name}")

    def _handle_workspace_upload_init(self) -> None:
        try:
            body = self._read_json_body()
            if body is None:
                return
            for field in ('workspace_id', 'file_name', 'file_size', 'target_path'):
                if field not in body:
                    self._send_json_error(400, f"INVALID_REQUEST: missing field {field}")
                    return

            from runtime.workspace_manager import parse_upload_size, parse_upload_max_threads

            parallel_size = parse_upload_size(os.environ.get('UPLOAD_PARALLEL_SIZE'))
            parallel_max_threads = parse_upload_max_threads(os.environ.get('UPLOAD_PARALLEL_MAX_THREADS'))
            workspace_mgr = self._get_workspace_manager()
            task = workspace_mgr.create_upload_task(
                body.get('file_name'),
                body.get('file_size'),
                body.get('target_path'),
                parallel_size,
                parallel_max_threads,
                body.get('target_dir_path'),
                restrict_workspace=self._should_restrict_workspace(),
            )
            task['workspace_id'] = body.get('workspace_id')

            uploads, lock = self._get_workspace_upload_state()
            with lock:
                uploads[task['upload_id']] = task

            self._send_json_response(200, {
                "upload_id": task['upload_id'],
                "parallel_size": task['parallel_size'],
                "parallel_max_threads": task['parallel_max_threads'],
                "chunk_count": task['chunk_count'],
                "chunks": [{
                    "parallel_id": chunk['parallel_id'],
                    "offset": chunk['offset'],
                    "size": chunk['size'],
                } for chunk in task['chunks']],
            })
        except ValueError as e:
            self._send_json_error(self._upload_error_status(str(e)), str(e))
        except Exception as e:
            logger.error(f"Workspace upload init error: {e}")
            self._send_json_error(500, "SERVER_ERROR: internal server error")

    def _handle_workspace_upload_chunk(self, upload_id: str, parallel_id: int) -> None:
        uploads, lock = self._get_workspace_upload_state()
        workspace_mgr = self._get_workspace_manager()
        try:
            content_length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self._send_json_error(400, "CHUNK_SIZE_MISMATCH: invalid Content-Length")
            return

        with lock:
            task = uploads.get(upload_id)
            if task is None:
                self._send_json_error(404, "UPLOAD_NOT_FOUND: upload_id not found")
                self._drain_request_body()
                return
            if task.get('status') == 'cancelled':
                self._send_json_error(409, "UPLOAD_CANCELLED: upload has been cancelled")
                self._drain_request_body()
                return
            if task.get('status') in {'completing', 'completed'}:
                self._send_json_error(409, "UPLOAD_NOT_READY: upload is not accepting chunks")
                self._drain_request_body()
                return
            chunks = {chunk['parallel_id']: chunk for chunk in task['chunks']}
            chunk = chunks.get(parallel_id)
            if chunk is None:
                self._send_json_error(404, "CHUNK_NOT_FOUND: parallel_id not found")
                self._drain_request_body()
                return
            if content_length != chunk['size']:
                self._send_json_error(400, "CHUNK_SIZE_MISMATCH: Content-Length does not match expected size")
                self._drain_request_body()
                return
            try:
                upload_offset = self._upload_header_int('X-Upload-Offset')
                upload_size = self._upload_header_int('X-Upload-Size')
                file_size = self._upload_header_int('X-File-Size')
            except ValueError as e:
                self._send_json_error(400, str(e))
                self._drain_request_body()
                return
            if upload_offset is not None and upload_offset != chunk['offset']:
                self._send_json_error(400, "CHUNK_SIZE_MISMATCH: X-Upload-Offset mismatch")
                self._drain_request_body()
                return
            if upload_size is not None and upload_size != chunk['size']:
                self._send_json_error(400, "CHUNK_SIZE_MISMATCH: X-Upload-Size mismatch")
                self._drain_request_body()
                return
            if file_size is not None and file_size != task['file_size']:
                self._send_json_error(400, "CHUNK_SIZE_MISMATCH: X-File-Size mismatch")
                self._drain_request_body()
                return
            chunk['status'] = 'uploading'
            task['status'] = 'uploading'

        try:
            received = workspace_mgr.write_upload_chunk(upload_id, parallel_id, self.rfile, content_length)
            with lock:
                task = uploads.get(upload_id)
                if task is None or task.get('status') == 'cancelled':
                    workspace_mgr.cleanup_upload_temp(upload_id, {"chunks": [{"parallel_id": parallel_id}], "target_path": ""})
                    self._send_json_error(409, "UPLOAD_CANCELLED: upload has been cancelled")
                    return
                for chunk in task['chunks']:
                    if chunk['parallel_id'] == parallel_id:
                        chunk['status'] = 'uploaded'
                        break
            self._send_json_response(200, {
                "upload_id": upload_id,
                "parallel_id": parallel_id,
                "received": received,
                "status": "uploaded",
            })
        except (ConnectionError, ValueError) as e:
            with lock:
                task = uploads.get(upload_id)
                if task:
                    for chunk in task['chunks']:
                        if chunk['parallel_id'] == parallel_id:
                            chunk['status'] = 'pending'
                            break
            self._send_json_error(self._upload_error_status(str(e)), str(e))
        except Exception as e:
            logger.error(f"Workspace upload chunk error: {e}")
            self._send_json_error(500, "SERVER_ERROR: internal server error")

    def _handle_workspace_upload_complete(self, upload_id: str) -> None:
        uploads, lock = self._get_workspace_upload_state()
        workspace_mgr = self._get_workspace_manager()
        # The client always sends a JSON body ("{}"). Drain it so the bytes do
        # not leak into the next request on this keep-alive connection (they
        # would be read as the start of the next request line, e.g.
        # "{}POST /v1/workspace/upload/init HTTP/1.1" -> intermittent 501).
        self._drain_request_body()
        with lock:
            task = uploads.get(upload_id)
            if task is None:
                self._send_json_error(404, "UPLOAD_NOT_FOUND: upload_id not found")
                return
            if task.get('status') == 'cancelled':
                self._send_json_error(409, "UPLOAD_CANCELLED: upload has been cancelled")
                return
            if any(chunk.get('status') == 'uploading' for chunk in task['chunks']):
                self._send_json_error(409, "UPLOAD_NOT_READY: some chunks are still uploading")
                return
            if any(chunk.get('status') != 'uploaded' for chunk in task['chunks']):
                self._send_json_error(409, "UPLOAD_NOT_READY: some chunks are missing")
                return
            task['status'] = 'completing'

        try:
            result = workspace_mgr.complete_upload_task(task)
            with lock:
                uploads.pop(upload_id, None)
            self._send_json_response(200, result)
        except ValueError as e:
            with lock:
                task = uploads.get(upload_id)
                if task:
                    task['status'] = 'failed'
            self._send_json_error(self._upload_error_status(str(e)), str(e))
        except Exception as e:
            with lock:
                task = uploads.get(upload_id)
                if task:
                    task['status'] = 'failed'
            logger.error(f"Workspace upload complete error: {e}")
            self._send_json_error(500, "SERVER_ERROR: internal server error")

    def _handle_workspace_upload_cancel(self, upload_id: str) -> None:
        uploads, lock = self._get_workspace_upload_state()
        workspace_mgr = self._get_workspace_manager()
        with lock:
            task = uploads.pop(upload_id, None)
            if task is not None:
                task['status'] = 'cancelled'
        if task is None:
            self._send_json_error(404, "UPLOAD_NOT_FOUND: upload_id not found")
            return
        workspace_mgr.cleanup_upload_temp(upload_id, task)
        self._send_json_response(200, {"upload_id": upload_id, "status": "cancelled"})

    def _get_workspace_manager(self):
        """Get or create workspace manager instance."""
        if not hasattr(self.server, '_workspace_manager'):
            from runtime.common import get_workspace
            workspace_path = get_workspace()
            from runtime.workspace_manager import WorkspaceManager
            self.server._workspace_manager = WorkspaceManager(workspace_path)
        return self.server._workspace_manager

    @staticmethod
    def _should_restrict_workspace() -> bool:
        """Read RESTRICT_WORKSPACE_IN_BACKEND env var dynamically (default False)."""
        return os.environ.get('RESTRICT_WORKSPACE_IN_BACKEND', '').strip().lower() in ('1', 'true', 'yes', 'on')
