import shutil

from runtime.common import search_files


_ORIGINAL_WHICH = shutil.which


def _grep_only_which(name: str) -> str | None:
    if name in {"rg", "ripgrep"}:
        return None
    return _ORIGINAL_WHICH(name)


def test_search_files_grep_fallback_default_include_finds_files(tmp_path, monkeypatch):
    """Default include='**/*' must not become grep --include=**/* (matches nothing)."""
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "target.py"
    target.write_text("needle_default_include\n", encoding="utf-8")

    monkeypatch.setattr("runtime.common.shutil.which", _grep_only_which)

    assert search_files(str(tmp_path), "needle_default_include", timeout=5) == {str(target)}


def test_search_files_grep_fallback_path_include_finds_conversation_json(tmp_path, monkeypatch):
    """Session search uses **/conversation.json; grep must handle that path glob."""
    session_dir = tmp_path / "session_a"
    session_dir.mkdir()
    target = session_dir / "conversation.json"
    ignored = session_dir / "notes.txt"
    target.write_text('{"text":"needle_conversation_json"}\n', encoding="utf-8")
    ignored.write_text("needle_conversation_json\n", encoding="utf-8")

    monkeypatch.setattr("runtime.common.shutil.which", _grep_only_which)

    assert search_files(
        str(tmp_path),
        "needle_conversation_json",
        include="**/conversation.json",
        timeout=5,
    ) == {str(target)}


def test_search_files_grep_fallback_does_not_follow_symlink_dirs(tmp_path, monkeypatch):
    """grep fallback should behave like rg and avoid recursive symlink traversal."""
    external = tmp_path / "external"
    workspace = tmp_path / "workspace"
    external.mkdir()
    workspace.mkdir()
    external_target = external / "external.txt"
    workspace_target = workspace / "local.txt"
    external_target.write_text("needle_symlink_follow\n", encoding="utf-8")
    workspace_target.write_text("needle_symlink_follow\n", encoding="utf-8")
    (workspace / "linked_external").symlink_to(external, target_is_directory=True)

    monkeypatch.setattr("runtime.common.shutil.which", _grep_only_which)

    assert search_files(str(workspace), "needle_symlink_follow", timeout=5) == {str(workspace_target)}
