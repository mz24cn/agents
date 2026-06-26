"""Tests for ContextManager session management and file I/O (Task 2).

Covers:
  - Property test: record_tool_call file naming (Task 2.1, Property 7)
  - Unit tests: session management (Task 2.2)
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from runtime.context_manager import (
    ContextManager,
    ConversationTurn,
    revoke_session_file_changes,
    undo_latest_file_journal_turn,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cm(tmp_dir: str) -> ContextManager:
    """Return a ContextManager backed by *tmp_dir*."""
    return ContextManager(
        infer_fn=lambda req: None,
        chats_dir=tmp_dir,
    )


def _make_turn(role: str, content: str, ts: str = "2026-01-01T00:00:00") -> ConversationTurn:
    return ConversationTurn(role=role, content=content, timestamp=ts)


# ---------------------------------------------------------------------------
# Property 7: Tool-call file naming convention (Task 2.1)
# **Validates: Requirements 3.4**
# ---------------------------------------------------------------------------

@given(
    turn_index=st.integers(min_value=0),
    tool_name=st.text(
        min_size=1,
        alphabet=st.characters(
            whitelist_categories=("Lu", "Ll", "Nd"),
            whitelist_characters="_-",
        ),
    ),
)
@settings(max_examples=25)
def test_tool_call_file_naming(turn_index: int, tool_name: str) -> None:
    """record_tool_call is a no-op — returns empty string (tool results stored inline in conversation.json).

    **Validates: Requirements 3.4**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        session_id = cm.create_session()

        turns = [_make_turn("user", "hello")]
        cm.save_conversation(session_id, turns)

        file_path = cm.record_tool_call(
            session_id=session_id,
            turn_index=turn_index,
            tool_name=tool_name,
            arguments={"key": "value"},
            result="ok",
            timestamp="2026-01-01T00:00:00",
        )

        # record_tool_call is a no-op — always returns empty string
        assert file_path == "", (
            f"record_tool_call must return '' (no-op), got '{file_path}'"
        )


def _blob_ref(session_dir: str, turn_key: str, rel_path: str, role: str, raw: bytes) -> dict:
    digest = hashlib.sha256(raw).hexdigest()
    blob_rel = f"files/{rel_path.replace('/', '-')}.{role}.gz"
    blob_path = os.path.join(session_dir, "file_journals", turn_key, blob_rel)
    os.makedirs(os.path.dirname(blob_path), exist_ok=True)
    with open(blob_path, "wb") as fh:
        fh.write(gzip.compress(raw))
    return {
        "exists": True,
        "store": "sidecar",
        "file": blob_rel,
        "sha256": digest,
        "size": len(raw),
        "compression": "gzip",
        "mode": "100644",
        "is_symlink": False,
    }


def _write_manifest(session_dir: str, workspace: str, session_id: str, turn_key: str, timestamp: str, files: dict) -> str:
    manifest_dir = os.path.join(session_dir, "file_journals", turn_key)
    os.makedirs(manifest_dir, exist_ok=True)
    manifest_path = os.path.join(manifest_dir, "manifest.json")
    manifest = {
        "version": 1,
        "session_id": session_id,
        "timestamp": timestamp,
        "turn_key": turn_key,
        "workspace": workspace,
        "created_at": timestamp,
        "updated_at": timestamp,
        "files": files,
    }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    return manifest_path


def test_undo_latest_file_journal_turn_moves_files_to_undone_files(tmp_path):
    workspace = str(tmp_path / "workspace")
    session_dir = str(tmp_path / "chat_data" / "session1")
    os.makedirs(workspace)
    os.makedirs(session_dir)
    target = os.path.join(workspace, "foo.txt")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("after\n")
    files = {
        "foo.txt": {
            "path": "foo.txt",
            "tools": ["edit_file"],
            "baseline": _blob_ref(session_dir, "260511_102030", "foo.txt", "baseline", b"before\n"),
            "after": _blob_ref(session_dir, "260511_102030", "foo.txt", "after", b"after\n"),
        }
    }
    manifest_path = _write_manifest(session_dir, workspace, "session1", "260511_102030", "2026-05-11T10:20:30", files)

    result = undo_latest_file_journal_turn(workspace, session_dir, "session1")

    assert "error" not in result
    assert result["turn_key"] == "260511_102030"
    assert open(target, encoding="utf-8").read() == "before\n"
    journal_dir = os.path.dirname(manifest_path)
    assert not os.path.exists(os.path.join(journal_dir, "files"))
    assert os.path.exists(os.path.join(journal_dir, "undone_files", "foo.txt.baseline.gz"))
    assert os.path.exists(os.path.join(journal_dir, "undone_files", "foo.txt.after.gz"))
    manifest = json.load(open(manifest_path, encoding="utf-8"))
    assert "files" not in manifest
    assert "foo.txt" in manifest["undone_files"]
    assert manifest["undone_files"]["foo.txt"]["baseline"]["file"] == "undone_files/foo.txt.baseline.gz"
    assert manifest["undone_files"]["foo.txt"]["after"]["file"] == "undone_files/foo.txt.after.gz"


def test_revoke_session_file_changes_restores_sidecar_baseline(tmp_path):
    workspace = str(tmp_path / "workspace")
    session_dir = str(tmp_path / "chat_data" / "session1")
    os.makedirs(workspace)
    os.makedirs(session_dir)
    target = os.path.join(workspace, "foo.txt")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("after\n")
    files = {
        "foo.txt": {
            "path": "foo.txt",
            "tools": ["edit_file"],
            "baseline": _blob_ref(session_dir, "260511_102030", "foo.txt", "baseline", b"before\n"),
            "after": _blob_ref(session_dir, "260511_102030", "foo.txt", "after", b"after\n"),
        }
    }
    manifest_path = _write_manifest(session_dir, workspace, "session1", "260511_102030", "2026-05-11T10:20:30", files)

    result = revoke_session_file_changes(workspace, session_dir, "session1", "2026-05-11T10:20:30")

    assert "error" not in result
    assert open(target, encoding="utf-8").read() == "before\n"
    journal_dir = os.path.dirname(manifest_path)
    assert not os.path.exists(os.path.join(journal_dir, "files"))
    assert os.path.exists(os.path.join(journal_dir, "undone_files", "foo.txt.baseline.gz"))
    assert os.path.exists(os.path.join(journal_dir, "undone_files", "foo.txt.after.gz"))
    manifest = json.load(open(manifest_path, encoding="utf-8"))
    assert manifest["revoked"] is True
    assert "files" not in manifest
    assert "foo.txt" in manifest["undone_files"]
    assert manifest["undone_files"]["foo.txt"]["baseline"]["file"] == "undone_files/foo.txt.baseline.gz"
    assert manifest["undone_files"]["foo.txt"]["after"]["file"] == "undone_files/foo.txt.after.gz"


def test_revoke_session_file_changes_deletes_created_file(tmp_path):
    workspace = str(tmp_path / "workspace")
    session_dir = str(tmp_path / "chat_data" / "session1")
    os.makedirs(workspace)
    os.makedirs(session_dir)
    target = os.path.join(workspace, "created.txt")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("created\n")
    files = {
        "created.txt": {
            "path": "created.txt",
            "tools": ["write_file"],
            "baseline": {"exists": False},
            "after": _blob_ref(session_dir, "260511_102030", "created.txt", "after", b"created\n"),
        }
    }
    manifest_path = _write_manifest(session_dir, workspace, "session1", "260511_102030", "2026-05-11T10:20:30", files)

    result = revoke_session_file_changes(workspace, session_dir, "session1", "2026-05-11T10:20:30")

    assert "error" not in result
    assert not os.path.exists(target)
    journal_dir = os.path.dirname(manifest_path)
    assert not os.path.exists(os.path.join(journal_dir, "files"))
    assert os.path.exists(os.path.join(journal_dir, "undone_files", "created.txt.after.gz"))
    manifest = json.load(open(manifest_path, encoding="utf-8"))
    assert manifest["undone_files"]["created.txt"]["after"]["file"] == "undone_files/created.txt.after.gz"


def test_revoke_session_file_changes_keep_files_keeps_active_files_dir(tmp_path):
    workspace = str(tmp_path / "workspace")
    session_dir = str(tmp_path / "chat_data" / "session1")
    os.makedirs(workspace)
    os.makedirs(session_dir)
    target = os.path.join(workspace, "foo.txt")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("after\n")
    files = {
        "foo.txt": {
            "path": "foo.txt",
            "tools": ["edit_file"],
            "baseline": _blob_ref(session_dir, "260511_102030", "foo.txt", "baseline", b"before\n"),
            "after": _blob_ref(session_dir, "260511_102030", "foo.txt", "after", b"after\n"),
        }
    }
    manifest_path = _write_manifest(session_dir, workspace, "session1", "260511_102030", "2026-05-11T10:20:30", files)

    result = revoke_session_file_changes(workspace, session_dir, "session1", "2026-05-11T10:20:30", keep_files=True)

    assert "error" not in result
    assert open(target, encoding="utf-8").read() == "after\n"
    journal_dir = os.path.dirname(manifest_path)
    assert os.path.exists(os.path.join(journal_dir, "files", "foo.txt.baseline.gz"))
    assert not os.path.exists(os.path.join(journal_dir, "undone_files"))
    manifest = json.load(open(manifest_path, encoding="utf-8"))
    assert manifest["revoked"] is True
    assert manifest["kept_files"] is True
    assert "files" in manifest
    assert "undone_files" not in manifest


def test_revoke_session_file_changes_detects_conflict_before_restoring(tmp_path):
    workspace = str(tmp_path / "workspace")
    session_dir = str(tmp_path / "chat_data" / "session1")
    os.makedirs(workspace)
    os.makedirs(session_dir)
    target = os.path.join(workspace, "foo.txt")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("user change\n")
    files = {
        "foo.txt": {
            "path": "foo.txt",
            "tools": ["edit_file"],
            "baseline": _blob_ref(session_dir, "260511_102030", "foo.txt", "baseline", b"before\n"),
            "after": _blob_ref(session_dir, "260511_102030", "foo.txt", "after", b"after\n"),
        }
    }
    _write_manifest(session_dir, workspace, "session1", "260511_102030", "2026-05-11T10:20:30", files)

    result = revoke_session_file_changes(workspace, session_dir, "session1", "2026-05-11T10:20:30")

    assert result["error"] == "JournalConflict"
    assert open(target, encoding="utf-8").read() == "user change\n"


# ---------------------------------------------------------------------------
# Unit tests — session management (Task 2.2)
# ---------------------------------------------------------------------------

def test_create_session_creates_directory():
    """create_session must create the session directory under chats_dir."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        chats_dir = os.path.join(tmp_dir, "chats")
        cm = ContextManager(infer_fn=lambda req: None, chats_dir=chats_dir)

        session_id = cm.create_session()

        session_dir = os.path.join(chats_dir, session_id)
        assert os.path.isdir(session_dir), "Session directory must be created"


def test_create_session_timestamp_format():
    """create_session must return a session_id matching YYMMDD_HHMMSS."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        session_id = cm.create_session()

        pattern = re.compile(r"^\d{6}_\d{6}$")
        assert pattern.match(session_id), (
            f"session_id '{session_id}' does not match YYMMDD_HHMMSS"
        )


def test_create_session_auto_creates_chats_dir():
    """create_session must auto-create the /chats base directory if absent."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        chats_dir = os.path.join(tmp_dir, "nested", "chats")
        assert not os.path.exists(chats_dir), "chats_dir must not exist yet"

        cm = ContextManager(infer_fn=lambda req: None, chats_dir=chats_dir)
        cm.create_session()

        assert os.path.isdir(chats_dir), "/chats base directory must be created"


def test_session_exists_true():
    """session_exists must return True for an existing session."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        session_id = cm.create_session()

        assert cm.session_exists(session_id) is True


def test_session_exists_false():
    """session_exists must return False for a non-existent session."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)

        assert cm.session_exists("9999-01-01_00-00-00") is False


def test_save_load_conversation_roundtrip():
    """save_conversation then load_conversation must reproduce the original turns."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        session_id = cm.create_session()

        turns = [
            _make_turn("user", "What is 2+2?", "2026-01-01T10:00:00"),
            _make_turn("assistant", "4", "2026-01-01T10:00:01"),
            _make_turn("user", "Thanks!", "2026-01-01T10:00:02"),
        ]
        cm.save_conversation(session_id, turns)
        loaded = cm.load_conversation(session_id)

        assert len(loaded) == len(turns)
        for orig, got in zip(turns, loaded):
            assert got.role == orig.role
            assert got.content == orig.content
            assert got.timestamp == orig.timestamp


def test_save_conversation_atomic_write():
    """save_conversation must produce a readable conversation.json file."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        session_id = cm.create_session()

        turns = [_make_turn("user", "hello")]
        cm.save_conversation(session_id, turns)

        conv_path = os.path.join(tmp_dir, session_id, "conversation.json")
        assert os.path.isfile(conv_path), "conversation.json must exist after save"


def test_store_artifact_file_written_with_prefix():
    """store_artifact must write the file with an 'artifact-' prefix."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        session_id = cm.create_session()

        # Seed conversation so references can be updated
        cm.save_conversation(session_id, [_make_turn("user", "hi")])

        data = b"binary content"
        file_path = cm.store_artifact(session_id, "report.pdf", data)

        assert os.path.basename(file_path) == "artifact-report.pdf"
        assert os.path.isfile(file_path)
        with open(file_path, "rb") as fh:
            assert fh.read() == data


def test_store_artifact_updates_references():
    """store_artifact must write the artifact file to disk (references field is no longer used)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        session_id = cm.create_session()
        cm.save_conversation(session_id, [_make_turn("user", "hi")])

        file_path = cm.store_artifact(session_id, "image.png", b"\x89PNG")

        # Artifact file must exist on disk
        assert os.path.isfile(file_path), "Artifact file must be written to disk"
        with open(file_path, "rb") as fh:
            assert fh.read() == b"\x89PNG"


def test_record_tool_call_updates_references():
    """record_tool_call is a no-op (tool results stored inline in conversation.json)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        session_id = cm.create_session()
        cm.save_conversation(session_id, [_make_turn("user", "run exec_cli")])

        result = cm.record_tool_call(
            session_id=session_id,
            turn_index=0,
            tool_name="exec_cli",
            arguments={"command": "ls"},
            result="file.txt",
            timestamp="2026-01-01T00:00:00",
        )

        # record_tool_call is a no-op — returns empty string, no file written
        assert result == "", "record_tool_call must return empty string (no-op)"
