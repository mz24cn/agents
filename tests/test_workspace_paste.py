"""Tests for the clipboard paste-directory feature.

Covers:
- get_paste_directory(): /tmp on Linux, OS temp dir / drive-\tmp on Windows.
- Uploads into /tmp are allowed even when RESTRICT_WORKSPACE_IN_BACKEND is on
  (i.e. the paste directory is always writable, outside-workspace or not).
- GET /v1/workspace/paste-dir endpoint returns the paste directory.
"""

import http.client
import io
import json
import os
import threading
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from runtime.workspace_manager import (
    WorkspaceManager,
    get_paste_directory,
    expand_workspace_file_refs,
)
from runtime.models import Message
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer


# ---------------------------------------------------------------------------
# get_paste_directory
# ---------------------------------------------------------------------------


class TestGetPasteDirectory:
    def test_linux_returns_tmp(self, monkeypatch):
        # Force a non-Windows platform
        monkeypatch.setattr(os, "name", "posix")
        assert get_paste_directory("/home/user/workspace") == "/tmp"

    def test_windows_returns_os_temp_dir(self):
        with patch("runtime.workspace_manager.os.name", "nt"), \
             patch("runtime.workspace_manager.tempfile.gettempdir", return_value="C:\\Users\\u\\AppData\\Local\\Temp"), \
             patch("runtime.workspace_manager.os.path.isdir", return_value=True):
            result = get_paste_directory("C:\\Users\\u\\workspace")
        assert result == "C:\\Users\\u\\AppData\\Local\\Temp"

    def test_windows_fallback_to_workspace_drive_tmp(self):
        with patch("runtime.workspace_manager.os.name", "nt"), \
             patch("runtime.workspace_manager.os.sep", "\\"), \
             patch("runtime.workspace_manager.tempfile.gettempdir", side_effect=RuntimeError("no temp")), \
             patch("runtime.workspace_manager.os.path.splitdrive",
                   side_effect=lambda p: ("D:", p[2:] if len(p) > 2 else "")), \
             patch("runtime.workspace_manager.os.makedirs") as makedirs:
            result = get_paste_directory("D:\\users\\me\\workspace")
        assert result == "D:\\tmp"
        makedirs.assert_called_once_with("D:\\tmp", exist_ok=True)


# ---------------------------------------------------------------------------
# WorkspaceManager uploads into the paste directory
# ---------------------------------------------------------------------------


@pytest.fixture()
def ws_manager(tmp_path):
    """WorkspaceManager rooted at a temp directory."""
    return WorkspaceManager(str(tmp_path))


class TestPasteDirectoryUploads:
    def test_is_paste_directory_true_for_tmp(self, ws_manager):
        assert ws_manager.is_paste_directory("/tmp")
        assert ws_manager.is_paste_directory("/tmp/pasted-file.pdf")

    def test_is_paste_directory_false_for_other_outside_path(self, ws_manager, tmp_path, monkeypatch):
        # pytest tmp dirs live under /tmp, so force a non-/tmp paste dir.
        monkeypatch.setattr(
            "runtime.workspace_manager.get_paste_directory",
            lambda ws: "/nonexistent-paste-dir-xyz",
        )
        other = tmp_path / ".." / "some-other-dir"
        other_abs = str(other.resolve())
        assert not ws_manager.is_paste_directory(other_abs)

    def test_create_directory_allowed_in_paste_dir_when_restricted(self, ws_manager, tmp_path, monkeypatch):
        paste_dir = tmp_path / "paste-area"
        paste_dir.mkdir()
        monkeypatch.setattr(
            "runtime.workspace_manager.get_paste_directory",
            lambda ws: str(paste_dir),
        )

        result = ws_manager.create_directory(str(paste_dir), "attachments", restrict_workspace=True)

        assert result["is_dir"] is True
        assert result["path"] == str(paste_dir / "attachments")
        assert (paste_dir / "attachments").is_dir()

    def test_create_directory_rejected_in_other_external_dir_when_restricted(self, tmp_path, monkeypatch):
        workspace_dir = tmp_path / "workspace"
        paste_dir = tmp_path / "paste-area"
        outside_dir = tmp_path / "outside-area"
        workspace_dir.mkdir()
        paste_dir.mkdir()
        outside_dir.mkdir()
        manager = WorkspaceManager(str(workspace_dir))
        monkeypatch.setattr(
            "runtime.workspace_manager.get_paste_directory",
            lambda ws: str(paste_dir),
        )

        with pytest.raises(ValueError, match="outside workspace or paste directory"):
            manager.create_directory(str(outside_dir), "not-allowed", restrict_workspace=True)

    def test_upload_init_allowed_into_tmp_even_when_restricted(self, ws_manager):
        task = ws_manager.create_upload_task(
            file_name="photo.png",
            file_size=1024,
            target_path="photo.png",
            parallel_size=100 * 1024 * 1024,
            parallel_max_threads=1,
            target_dir_path="/tmp",
            restrict_workspace=True,
        )
        assert task["target_path"] == os.path.realpath("/tmp/photo.png")

    def test_upload_init_rejected_outside_workspace_when_restricted(self, ws_manager, tmp_path, monkeypatch):
        # pytest tmp dirs live under /tmp, so force a non-/tmp paste dir.
        monkeypatch.setattr(
            "runtime.workspace_manager.get_paste_directory",
            lambda ws: "/nonexistent-paste-dir-xyz",
        )
        outside = str((tmp_path / ".." / "outside-upload-dir").resolve())
        os.makedirs(outside, exist_ok=True)
        with pytest.raises(ValueError, match="outside workspace"):
            ws_manager.create_upload_task(
                file_name="x.pdf",
                file_size=10,
                target_path="x.pdf",
                parallel_size=1024,
                parallel_max_threads=1,
                target_dir_path=outside,
                restrict_workspace=True,
            )

    def test_upload_init_allowed_outside_workspace_when_not_restricted(self, ws_manager, tmp_path):
        outside = str((tmp_path / ".." / "outside-upload-dir").resolve())
        os.makedirs(outside, exist_ok=True)
        task = ws_manager.create_upload_task(
            file_name="x.pdf",
            file_size=10,
            target_path="x.pdf",
            parallel_size=1024,
            parallel_max_threads=1,
            target_dir_path=outside,
            restrict_workspace=False,
        )
        assert task["target_path"] == os.path.realpath(os.path.join(outside, "x.pdf"))

    def test_full_upload_into_tmp_with_restriction_on(self, ws_manager):
        """End-to-end: init -> write chunks -> complete lands the file in /tmp."""
        content = b"%PDF-1.4 fake pdf bytes for paste test"
        task = ws_manager.create_upload_task(
            file_name="paste-test.pdf",
            file_size=len(content),
            target_path="paste-test.pdf",
            parallel_size=1024 * 1024,
            parallel_max_threads=1,
            target_dir_path="/tmp",
            restrict_workspace=True,
        )
        stream = io.BytesIO(content)
        received = ws_manager.write_upload_chunk(task["upload_id"], 0, stream, len(content))
        assert received == len(content)
        result = ws_manager.complete_upload_task(task)
        target = os.path.realpath("/tmp/paste-test.pdf")
        assert result["path"] == "paste-test.pdf"
        assert os.path.isfile(target)
        with open(target, "rb") as f:
            assert f.read() == content
        # Cleanup
        os.remove(target)


# ---------------------------------------------------------------------------
# <file> reference expansion for pasted files (image / PDF / DOCX)
# ---------------------------------------------------------------------------


class TestPastedFileRefExpansion:
    """Pasted files land in /tmp; the chat message references them via
    ``<file>/tmp/...`` tags.  These must expand even though /tmp lives outside
    the workspace (the paste directory is already handled specially)."""

    @pytest.fixture()
    def tmp_paste_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="paste_refs_") as d:
            yield d

    def test_pasted_image_ref_expands_to_image_attachment(self, tmp_paste_dir, tmp_path):
        img = os.path.join(tmp_paste_dir, "shot.png")
        with open(img, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        msg = Message(role="user", content=f"<file>{img}</file>")
        expanded = expand_workspace_file_refs([msg], str(tmp_path))[0]
        assert "Image file attached" in expanded.content
        assert expanded.images == [img]

    def test_pasted_pdf_ref_expands_to_path_reference(self, tmp_paste_dir, tmp_path):
        pdf = os.path.join(tmp_paste_dir, "doc.pdf")
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.4 fake pdf")
        msg = Message(role="user", content=f"<file>{pdf}</file>")
        expanded = expand_workspace_file_refs([msg], str(tmp_path))[0]
        assert f"[file attached: {pdf}]" in expanded.content

    def test_pasted_docx_ref_expands_to_path_reference(self, tmp_paste_dir, tmp_path):
        docx = os.path.join(tmp_paste_dir, "doc.docx")
        with open(docx, "wb") as f:
            f.write(b"PK\x03\x04 fake docx")
        msg = Message(role="user", content=f"<file>{docx}</file>")
        expanded = expand_workspace_file_refs([msg], str(tmp_path))[0]
        assert f"[file attached: {docx}]" in expanded.content


# ---------------------------------------------------------------------------
# HTTP endpoint: GET /v1/workspace/paste-dir
# ---------------------------------------------------------------------------


@pytest.fixture()
def runtime():
    model_reg = ModelRegistry()
    tool_reg = ToolRegistry()
    return Runtime(model_reg, tool_reg)


@pytest.fixture()
def server(runtime, tmp_path, monkeypatch):
    models_path = str(tmp_path / "models.json")
    tools_path = str(tmp_path / "tools.json")
    prompt_templates_path = str(tmp_path / "prompt_templates.json")
    workspace_dir = str(tmp_path / "workspace")
    os.makedirs(workspace_dir, exist_ok=True)
    monkeypatch.setenv("AGENTS_WORKSPACE", workspace_dir)
    with patch("runtime.server._MODELS_PATH", models_path), \
         patch("runtime.server._TOOLS_PATH", tools_path), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", prompt_templates_path), \
         patch("runtime.server._DATA_DIR", str(tmp_path)):
        srv = RuntimeHTTPServer(runtime)
        srv.start_background(host="127.0.0.1", port=0)
        yield srv
        srv.stop()


def _url(server, path):
    return f"http://127.0.0.1:{server.port}{path}"


def _get(server, path):
    req = urllib.request.Request(_url(server, path))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class TestPasteDirEndpoint:
    def test_returns_paste_directory(self, server, monkeypatch):
        monkeypatch.setattr(os, "name", "posix")
        status, body = _get(server, "/v1/workspace/paste-dir")
        assert status == 200
        assert body["path"] == "/tmp"

    def test_returns_windows_temp_dir(self, server):
        with patch("runtime.workspace_manager.os.name", "nt"), \
             patch("runtime.workspace_manager.tempfile.gettempdir", return_value="C:\\Temp\\AgentPaste"), \
             patch("runtime.workspace_manager.os.path.isdir", return_value=True):
            status, body = _get(server, "/v1/workspace/paste-dir")
        assert status == 200
        assert body["path"] == "C:\\Temp\\AgentPaste"


# ---------------------------------------------------------------------------
# Keep-alive body draining (regression: 501 "Unsupported method ('{}POST')")
# ---------------------------------------------------------------------------


class TestKeepAliveBodyDrain:
    """upload/complete must consume its JSON body so the bytes do not leak
    into the next request on the same keep-alive connection.

    Before the fix the client sent `POST .../complete` with body `{}`, the
    handler never read it, and the next `POST .../upload/init` on that
    connection arrived as `{}POST ...` -> intermittent
    501 "Unsupported method ('{}POST')".
    """

    def _upload_and_complete(self, conn, file_name):
        """Run one full upload (init -> chunks -> complete) over `conn`."""
        content = b"%PDF-1.4 keep-alive drain test"
        headers = {"Content-Type": "application/json"}
        conn.request(
            "POST",
            "/v1/workspace/upload/init",
            body=json.dumps({
                "workspace_id": "default",
                "file_name": file_name,
                "file_size": len(content),
                "target_path": file_name,
                "target_dir_path": "/tmp",
            }),
            headers=headers,
        )
        resp = conn.getresponse()
        assert resp.status == 200, resp.read()
        init = json.loads(resp.read())
        upload_id = init["upload_id"]
        for chunk in init["chunks"]:
            conn.request(
                "PUT",
                f"/v1/workspace/upload/{upload_id}/chunk/{chunk['parallel_id']}",
                body=content[chunk["offset"]:chunk["offset"] + chunk["size"]],
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Upload-Offset": str(chunk["offset"]),
                    "X-Upload-Size": str(chunk["size"]),
                    "X-File-Size": str(len(content)),
                },
            )
            resp = conn.getresponse()
            assert resp.status == 200, resp.read()
            resp.read()
        conn.request(
            "POST",
            f"/v1/workspace/upload/{upload_id}/complete",
            body="{}",
            headers=headers,
        )
        resp = conn.getresponse()
        assert resp.status == 200, resp.read()
        resp.read()
        return os.path.realpath(f"/tmp/{file_name}")

    def test_complete_body_does_not_corrupt_next_init_on_same_connection(self, server):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        try:
            first = self._upload_and_complete(conn, "drain-test-1.pdf")
            # Same connection: a second upload/init must NOT see the leftover
            # "{}" from the complete request (previously -> 501 {}POST).
            second = self._upload_and_complete(conn, "drain-test-2.pdf")
        finally:
            conn.close()
        for path in (first, second):
            if os.path.isfile(path):
                os.remove(path)

    def test_upload_complete_drains_body_even_on_error(self, server):
        """A complete for an unknown upload must also drain the body so the
        connection stays usable for the next request."""
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        try:
            conn.request(
                "POST",
                "/v1/workspace/upload/does-not-exist/complete",
                body="{}",
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            assert resp.status == 404
            resp.read()
            # The drained body must not corrupt this next request.
            status, _ = _get(server, "/v1/workspace/paste-dir")
            assert status == 200
        finally:
            conn.close()
