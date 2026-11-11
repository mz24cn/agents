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


def test_remove_trailing_assistant_message_requires_final_assistant() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        session_id = cm.create_session()
        cm.save_conversation(session_id, [
            _make_turn("user", "question"),
            _make_turn("assistant", "a complete answer", "2026-01-01T00:00:01"),
        ], last_total_tokens=123)

        assert cm.remove_trailing_assistant_message(session_id) is True
        loaded = cm.load_conversation(session_id)
        assert [(turn.role, turn.content) for turn in loaded] == [("user", "question")]

        with open(cm._conversation_path(session_id), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["meta"]["turn_count"] == 1
        assert data["meta"]["last_total_tokens"] == 123


def test_remove_trailing_assistant_message_does_not_classify_content() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        session_id = cm.create_session()
        cm.save_conversation(session_id, [
            _make_turn("user", "render"),
            ConversationTurn(
                role="assistant",
                content="ordinary content without an error marker",
                timestamp="2026-01-01T00:00:01",
                tool_calls=[{"id": "partial", "name": "read_file", "arguments": "{\"path\":"}],
            ),
        ])

        assert cm.remove_trailing_assistant_message(session_id) is True
        assert [turn.role for turn in cm.load_conversation(session_id)] == ["user"]


def test_remove_trailing_assistant_message_preserves_non_assistant_tail() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        session_id = cm.create_session()
        cm.save_conversation(session_id, [
            _make_turn("assistant", "answer"),
            _make_turn("tool", "Error: tool execution failed", "2026-01-01T00:00:01"),
        ])

        assert cm.remove_trailing_assistant_message(session_id) is False
        assert len(cm.load_conversation(session_id)) == 2


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


def _write_manifest(
    session_dir: str,
    workspace: str,
    session_id: str,
    turn_key: str,
    timestamp: str,
    files: dict,
    omit_workspace: bool = False,
) -> str:
    manifest_dir = os.path.join(session_dir, "file_journals", turn_key)
    os.makedirs(manifest_dir, exist_ok=True)
    manifest_path = os.path.join(manifest_dir, "manifest.json")
    manifest = {
        "version": 1,
        "session_id": session_id,
        "timestamp": timestamp,
        "turn_key": turn_key,
        "created_at": timestamp,
        "updated_at": timestamp,
        "files": files,
    }
    if not omit_workspace:
        # The workspace the turn was journaled in, exactly as the writer
        # records it (runtime/builtin_tools_coding._FileJournalManager).
        manifest["workspace"] = workspace
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
# Unit tests — the workspace a revoke restores into
#
# A session's workspace is per-session state; a revoke request does not carry
# it and nothing on that path binds it to the request thread, so the only
# trustworthy answer is the workspace each journal turn recorded.  Everything
# below pins that against the process default (AGENTS_WORKSPACE), which is the
# production failure: an HQD session's revoke restoring into an unrelated TDS
# checkout -- and "force" writing 24 files there while HQD stayed untouched.
# ---------------------------------------------------------------------------

def _journal_file(session_dir: str, turn_key: str, rel: str, baseline: bytes, after: bytes) -> dict:
    return {
        rel: {
            "path": rel,
            "tools": ["edit_file"],
            "baseline": _blob_ref(session_dir, turn_key, rel, "baseline", baseline),
            "after": _blob_ref(session_dir, turn_key, rel, "after", after),
        }
    }


def test_revoke_restores_the_workspace_the_journal_was_taken_in(tmp_path, monkeypatch):
    """The manifest's workspace wins over the caller's (process default)."""
    session_ws = str(tmp_path / "HQD")
    process_ws = str(tmp_path / "TDS")
    session_dir = str(tmp_path / "chat_data" / "session1")
    for d in (session_ws, process_ws, session_dir):
        os.makedirs(d)
    rel = os.path.join("frontend", "src", "router", "index.ts")
    session_target = os.path.join(session_ws, rel)
    process_target = os.path.join(process_ws, rel)
    for path in (session_target, process_target):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("after\n")
    files = _journal_file(
        session_dir, "260511_102030", "frontend/src/router/index.ts",
        b"before\n", b"after\n",
    )
    _write_manifest(
        session_dir, session_ws, "session1", "260511_102030",
        "2026-05-11T10:20:30", files,
    )
    # The server process runs in a different workspace than the session did.
    monkeypatch.setenv("AGENTS_WORKSPACE", process_ws)

    result = revoke_session_file_changes(
        process_ws, session_dir, "session1", "2026-05-11T10:20:30",
    )

    assert "error" not in result, result
    assert open(session_target, encoding="utf-8").read() == "before\n"
    # The unrelated checkout must not gain (or lose) a single file.
    assert open(process_target, encoding="utf-8").read() == "after\n"


def test_revoke_conflict_reports_the_workspaces_it_checked(tmp_path):
    """The 409 payload must name the root it probed, or the report is guesswork."""
    session_ws = str(tmp_path / "HQD")
    session_dir = str(tmp_path / "chat_data" / "session1")
    os.makedirs(session_ws)
    os.makedirs(session_dir)
    with open(os.path.join(session_ws, "foo.txt"), "w", encoding="utf-8") as fh:
        fh.write("drifted\n")
    files = _journal_file(session_dir, "260511_102030", "foo.txt", b"before\n", b"after\n")
    _write_manifest(session_dir, session_ws, "session1", "260511_102030", "2026-05-11T10:20:30", files)

    result = revoke_session_file_changes(
        str(tmp_path / "TDS"), session_dir, "session1", "2026-05-11T10:20:30",
    )

    assert result["error"] == "JournalConflict"
    assert os.path.realpath(session_ws) in result["checked_workspaces"]


def test_revoke_refuses_a_missing_journal_root_even_when_forced(tmp_path):
    """A root that is not here cannot be restored into -- and force would invent it."""
    gone_ws = str(tmp_path / "moved-or-unmounted")
    process_ws = str(tmp_path / "TDS")
    session_dir = str(tmp_path / "chat_data" / "session1")
    os.makedirs(process_ws)
    os.makedirs(session_dir)
    files = _journal_file(session_dir, "260511_102030", "foo.txt", b"before\n", b"after\n")
    _write_manifest(session_dir, gone_ws, "session1", "260511_102030", "2026-05-11T10:20:30", files)

    result = revoke_session_file_changes(
        process_ws, session_dir, "session1", "2026-05-11T10:20:30", force=True,
    )

    assert result["error"] == "JournalWorkspaceMismatch"
    assert result["can_force"] is False
    assert os.path.realpath(gone_ws) in result["workspaces"]
    # Nothing was written anywhere: the wrong tree must not be created.
    assert os.listdir(process_ws) == []
    # ...and the journal was left intact, so a later revoke can still succeed.
    manifest = json.load(open(
        os.path.join(session_dir, "file_journals", "260511_102030", "manifest.json"),
        encoding="utf-8",
    ))
    assert not manifest.get("revoked")
    assert "foo.txt" in manifest["files"]


def test_undo_latest_file_journal_turn_uses_the_manifest_workspace(tmp_path):
    session_ws = str(tmp_path / "HQD")
    process_ws = str(tmp_path / "TDS")
    session_dir = str(tmp_path / "chat_data" / "session1")
    for d in (session_ws, process_ws, session_dir):
        os.makedirs(d)
    with open(os.path.join(session_ws, "foo.txt"), "w", encoding="utf-8") as fh:
        fh.write("after\n")
    files = _journal_file(session_dir, "260511_102030", "foo.txt", b"before\n", b"after\n")
    _write_manifest(session_dir, session_ws, "session1", "260511_102030", "2026-05-11T10:20:30", files)

    result = undo_latest_file_journal_turn(process_ws, session_dir, "session1")

    assert "error" not in result, result
    assert open(os.path.join(session_ws, "foo.txt"), encoding="utf-8").read() == "before\n"
    assert not os.path.exists(os.path.join(process_ws, "foo.txt"))


def test_revoke_restores_each_turn_in_its_own_workspace(tmp_path):
    """A session may switch workspace mid-session; one revoke spans both roots.

    The same path label under two roots names two different files, so the
    restore plan must not merge them into one entry.
    """
    ws_a = str(tmp_path / "ws_a")
    ws_b = str(tmp_path / "ws_b")
    session_dir = str(tmp_path / "chat_data" / "session1")
    for d in (ws_a, ws_b, session_dir):
        os.makedirs(d)
    for root in (ws_a, ws_b):
        with open(os.path.join(root, "shared.txt"), "w", encoding="utf-8") as fh:
            fh.write("after\n")
    files_a = _journal_file(session_dir, "260511_102030", "shared.txt", b"a-before\n", b"after\n")
    files_b = _journal_file(session_dir, "260511_102100", "shared.txt", b"b-before\n", b"after\n")
    _write_manifest(session_dir, ws_a, "session1", "260511_102030", "2026-05-11T10:20:30", files_a)
    _write_manifest(session_dir, ws_b, "session1", "260511_102100", "2026-05-11T10:21:00", files_b)

    result = revoke_session_file_changes(
        str(tmp_path / "TDS"), session_dir, "session1", "2026-05-11T10:20:00",
    )

    assert "error" not in result, result
    assert open(os.path.join(ws_a, "shared.txt"), encoding="utf-8").read() == "a-before\n"
    assert open(os.path.join(ws_b, "shared.txt"), encoding="utf-8").read() == "b-before\n"


def test_revoke_falls_back_to_the_session_record_for_legacy_journals(tmp_path, monkeypatch):
    """Without a manifest workspace, use the session's own record, not AGENTS_WORKSPACE."""
    session_ws = str(tmp_path / "HQD")
    process_ws = str(tmp_path / "TDS")
    chats_dir = str(tmp_path / "chat_data")
    session_dir = os.path.join(chats_dir, "session1")
    for d in (session_ws, process_ws, session_dir):
        os.makedirs(d)
    with open(os.path.join(session_ws, "foo.txt"), "w", encoding="utf-8") as fh:
        fh.write("after\n")
    files = _journal_file(session_dir, "260511_102030", "foo.txt", b"before\n", b"after\n")
    _write_manifest(
        session_dir, session_ws, "session1", "260511_102030",
        "2026-05-11T10:20:30", files, omit_workspace=True,
    )

    cm = ContextManager(infer_fn=lambda req: None, chats_dir=chats_dir)
    cm.save_conversation(
        "session1",
        [_make_turn("user", "do it", "2026-05-11T10:20:30"),
         _make_turn("assistant", "done", "2026-05-11T10:20:40")],
        extra_meta={"workspace": session_ws},
    )
    monkeypatch.setenv("AGENTS_WORKSPACE", process_ws)

    result = cm.revoke_conversation("session1", "2026-05-11T10:20:30")

    assert result["journal"]["revoked"] is True
    assert open(os.path.join(session_ws, "foo.txt"), encoding="utf-8").read() == "before\n"
    assert not os.path.exists(os.path.join(process_ws, "foo.txt"))


def test_session_recorded_workspace_prefers_the_turn_snapshot(tmp_path):
    """A turn's own context snapshot beats the session-level (latest) value."""
    from runtime.context_manager import session_recorded_workspace

    session_dir = str(tmp_path / "chat_data" / "session1")
    os.makedirs(session_dir)
    with open(os.path.join(session_dir, "conversation.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "meta": {"workspace": "/ws/latest"},
            "messages": [
                {"role": "user", "content": "a", "timestamp": "2026-05-11T10:20:30",
                 "meta": {"workspace": "/ws/then"}},
                {"role": "user", "content": "b", "timestamp": "2026-05-11T10:30:00"},
            ],
        }, fh)

    assert session_recorded_workspace(session_dir, "2026-05-11T10:20:30") == "/ws/then"
    # A turn with no snapshot falls back to the session-level record.
    assert session_recorded_workspace(session_dir, "2026-05-11T10:30:00") == "/ws/latest"
    # Unknown session: nothing recorded, so the caller decides.
    assert session_recorded_workspace(str(tmp_path / "nope")) == ""


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
