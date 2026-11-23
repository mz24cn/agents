"""Per-user-turn request-context snapshots in ``conversation.json``.

A session can switch model / tool set / execution environment / agent /
workspace between turns.  The top-level ``meta`` block only ever holds the
latest values, so every user turn also carries the settings it was actually
sent with (``messages[i].meta``).

Covers:
  - ``build_turn_meta`` snapshot shape
  - persistence attaches the snapshot to the initiating user turn only
  - earlier turns keep their own snapshot when later turns change settings
  - Continue (no new user message) refreshes the last user turn's snapshot
  - the session-level ``meta.terminal`` marker (terminal window restore after a refresh)
  - round-trip through ``load_conversation``
  - the snapshot never reaches the model-facing context
"""

from __future__ import annotations

import json
import tempfile

from runtime.context_manager import ContextManager, turn_to_message_dict
from runtime.models import Message
from runtime.server_state import (
    build_turn_meta,
    mark_session_terminal,
    persist_conversation,
    reset_session_terminal_marks,
    session_terminal_marked,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cm(tmp_dir: str) -> ContextManager:
    return ContextManager(infer_fn=lambda req: None, chats_dir=tmp_dir)


def _read_conv(cm: ContextManager, session_id: str) -> dict:
    with open(cm._conversation_path(session_id), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _send(
    cm: ContextManager,
    session_id: str,
    user_text: str,
    *,
    model_id: str,
    tool_ids: list[str],
    workspace: str,
    agent_ids: list[str] | None = None,
    remote_env: str | None = None,
    system_text: str | None = None,
):
    """Persist one full turn (system? + user + assistant reply)."""
    originals: list[Message] = []
    if system_text is not None:
        originals.append(Message(role="system", content=system_text,
                                 timestamp="2026-01-01T00:00:00"))
    originals.append(Message(role="user", content=user_text,
                             timestamp="2026-01-01T00:00:01"))
    return persist_conversation(
        context_manager=cm,
        session_id=session_id,
        original_messages=originals,
        collected_messages=[
            Message(role="assistant", content=f"reply to {user_text}",
                    timestamp="2026-01-01T00:00:02"),
        ],
        session_manager=None,
        model_id=model_id,
        tool_ids=tool_ids,
        agent_ids=agent_ids,
        workspace=workspace,
        extra_meta={"remote_env": remote_env} if remote_env else None,
        compress=False,
        update_title=False,
    )


# ---------------------------------------------------------------------------
# build_turn_meta
# ---------------------------------------------------------------------------

def test_build_turn_meta_returns_none_when_nothing_is_known() -> None:
    assert build_turn_meta() is None
    assert build_turn_meta(model_id="", tool_ids=None, agent_ids=[],
                           workspace="", extra_meta={"remote_env": ""}) is None


def test_build_turn_meta_keeps_an_empty_tool_set() -> None:
    """An explicitly empty tool list is a real setting, not "unknown"."""
    meta = build_turn_meta(model_id="m", tool_ids=[])
    assert meta == {"model_id": "m", "tool_ids": []}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_user_turn_records_the_context_it_was_sent_with() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        sid = cm.create_session()

        exc = _send(
            cm, sid, "hello",
            model_id="GPT5.5", tool_ids=["read_file", "exec_shell"],
            agent_ids=["agent-a"], workspace="/ws/a", remote_env="env-1",
            system_text="you are a helper",
        )
        assert exc is None

        data = _read_conv(cm, sid)
        by_role = {m["role"]: m for m in data["messages"]}
        assert by_role["user"]["meta"] == {
            "model_id": "GPT5.5",
            "tool_ids": ["read_file", "exec_shell"],
            "agent_ids": ["agent-a"],
            "workspace": "/ws/a",
            "remote_env": "env-1",
        }
        # Only the initiating user turn carries the snapshot.
        assert "meta" not in by_role["system"]
        assert "meta" not in by_role["assistant"]
        # The session-level block keeps holding the latest values so existing
        # clients (session restore, category tree, execution analysis) work.
        assert data["meta"]["model_id"] == "GPT5.5"
        assert data["meta"]["workspace"] == "/ws/a"
        assert data["meta"]["remote_env"] == "env-1"


def test_each_user_turn_keeps_its_own_settings() -> None:
    """The whole point: turn 2 changing model/tools must not rewrite turn 1."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        sid = cm.create_session()

        assert _send(cm, sid, "first", model_id="model-a",
                     tool_ids=["read_file"], workspace="/ws/a") is None
        assert _send(cm, sid, "second", model_id="model-b",
                     tool_ids=["exec_shell"], workspace="/ws/b",
                     remote_env="env-2") is None

        users = [m for m in _read_conv(cm, sid)["messages"] if m["role"] == "user"]
        assert [u["content"] for u in users] == ["first", "second"]
        assert users[0]["meta"]["model_id"] == "model-a"
        assert users[0]["meta"]["workspace"] == "/ws/a"
        assert "remote_env" not in users[0]["meta"]
        assert users[1]["meta"]["model_id"] == "model-b"
        assert users[1]["meta"]["tool_ids"] == ["exec_shell"]
        assert users[1]["meta"]["remote_env"] == "env-2"


def test_snapshot_survives_a_load_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        sid = cm.create_session()
        assert _send(cm, sid, "hello", model_id="model-a",
                     tool_ids=["read_file"], workspace="/ws/a") is None

        turns = cm.load_conversation(sid)
        user = next(t for t in turns if t.role == "user")
        assert user.meta == {
            "model_id": "model-a",
            "tool_ids": ["read_file"],
            "workspace": "/ws/a",
        }
        assistant = next(t for t in turns if t.role == "assistant")
        assert assistant.meta is None

        # Re-saving must not drop it (compression / revoke rewrite paths).
        cm.save_conversation(sid, turns)
        assert _read_conv(cm, sid)["messages"][0]["meta"]["model_id"] == "model-a"


def test_continue_refreshes_the_last_user_turn_snapshot() -> None:
    """Continue sends no new user message but may run with other settings."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        sid = cm.create_session()
        assert _send(cm, sid, "first", model_id="model-a",
                     tool_ids=["read_file"], workspace="/ws/a") is None
        assert _send(cm, sid, "second", model_id="model-b",
                     tool_ids=["read_file"], workspace="/ws/a") is None

        exc = persist_conversation(
            context_manager=cm,
            session_id=sid,
            original_messages=[],
            collected_messages=[
                Message(role="assistant", content="retry",
                        timestamp="2026-01-01T00:00:03"),
            ],
            session_manager=None,
            model_id="model-c",
            tool_ids=["exec_shell"],
            workspace="/ws/c",
            compress=False,
            update_title=False,
        )
        assert exc is None

        users = [m for m in _read_conv(cm, sid)["messages"] if m["role"] == "user"]
        assert users[0]["meta"]["model_id"] == "model-a"
        assert users[-1]["meta"]["model_id"] == "model-c"
        assert users[-1]["meta"]["tool_ids"] == ["exec_shell"]
        assert users[-1]["meta"]["workspace"] == "/ws/c"


# ---------------------------------------------------------------------------
# meta.terminal = "enabled": the session-level "this session has a terminal"
# ---------------------------------------------------------------------------
#
# Written when a terminal is *opened* for the session (runtime/server_state.py:
# mark_session_terminal), never because exec_cli happens to sit in the tool set.
# The frontend reads it back after a page refresh to reopen the window, hidden
# if need be, instead of losing the entry (web/src/lib/session-terminal.js).

def test_opening_a_terminal_marks_the_session() -> None:
    """终端打开过 → 会话 meta 里落一笔 terminal=enabled。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        sid = cm.create_session()
        reset_session_terminal_marks()

        assert _send(cm, sid, "hi", model_id="model-a",
                     tool_ids=["read_file"], workspace="/ws/a") is None
        assert "terminal" not in _read_conv(cm, sid)["meta"]

        mark_session_terminal(sid)
        assert _send(cm, sid, "again", model_id="model-a",
                     tool_ids=["read_file"], workspace="/ws/a") is None

        meta = _read_conv(cm, sid)["meta"]
        assert meta["terminal"] == "enabled"
        # /v1/sessions、/v1/sessions/{id}(/meta) 都原样返回 meta，接口不必加字段。
        assert cm.get_session_meta(sid)["terminal"] == "enabled"


def test_the_tool_set_alone_never_marks_the_session() -> None:
    """exec_cli 在 tool_ids 里不等于终端打开过——模型可能一次都没调。

    标记记的是"终端被打开过"这个状态本身，所以工具集里有什么都不能让它出现。
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        sid = cm.create_session()
        reset_session_terminal_marks()

        assert _send(cm, sid, "hi", model_id="model-a",
                     tool_ids=["read_file", "exec_cli"], workspace="/ws/a") is None

        meta = _read_conv(cm, sid)["meta"]
        assert "terminal" not in meta
        assert meta["tool_ids"] == ["read_file", "exec_cli"]


def test_opening_a_terminal_patches_the_file_without_a_turn() -> None:
    """开着终端一句话没说就刷新，标记也已经在磁盘上了。

    落盘走 update_conversation_meta：messages 原样搬过去，updated_at 也不动
    （会话内容没变，侧栏不该因此重排）。
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        sid = cm.create_session()
        reset_session_terminal_marks()

        assert _send(cm, sid, "hi", model_id="model-a",
                     tool_ids=["read_file"], workspace="/ws/a") is None
        before = _read_conv(cm, sid)

        mark_session_terminal(sid, context_manager=cm)

        after = _read_conv(cm, sid)
        assert after["meta"]["terminal"] == "enabled"
        assert after["messages"] == before["messages"]
        assert after["meta"]["updated_at"] == before["meta"]["updated_at"]
        assert after["meta"]["created_at"] == before["meta"]["created_at"]
        # 没有会话文件可补时不报错：事实先待在内存里，第一次落盘带上它。
        assert cm.update_conversation_meta("no-such-session", {"terminal": "enabled"}) is False


def test_persistence_keeps_a_marker_written_outside_the_inference_path() -> None:
    """重启后没人再 mark 过一次，但文件里那份不能被下一次落盘抹掉。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        sid = cm.create_session()
        reset_session_terminal_marks()

        assert _send(cm, sid, "hi", model_id="model-a",
                     tool_ids=["read_file"], workspace="/ws/a") is None
        mark_session_terminal(sid, context_manager=cm)
        reset_session_terminal_marks()          # 进程重启：内存里的登记没了

        assert _send(cm, sid, "again", model_id="model-a",
                     tool_ids=["read_file"], workspace="/ws/a") is None
        assert _read_conv(cm, sid)["meta"]["terminal"] == "enabled"


def test_destroying_the_terminal_clears_it_from_both_halves() -> None:
    """双击销毁是"我不要这个终端"：内存与文件都得忘掉它。

    否则刷新后这条会话会自己再起一个 shell。
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        sid = cm.create_session()
        reset_session_terminal_marks()

        assert _send(cm, sid, "hi", model_id="model-a",
                     tool_ids=["read_file"], workspace="/ws/a") is None
        mark_session_terminal(sid, context_manager=cm)
        assert _read_conv(cm, sid)["meta"]["terminal"] == "enabled"

        mark_session_terminal(sid, enabled=False, context_manager=cm)

        assert "terminal" not in _read_conv(cm, sid)["meta"]
        assert session_terminal_marked(sid) is False
        assert _send(cm, sid, "again", model_id="model-a",
                     tool_ids=["read_file"], workspace="/ws/a") is None
        assert "terminal" not in _read_conv(cm, sid)["meta"]


def test_marker_is_a_session_setting_not_a_per_turn_snapshot() -> None:
    """它描述的是会话现在的状态，不是某一轮的历史，所以不进 turn meta。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        sid = cm.create_session()
        reset_session_terminal_marks()

        mark_session_terminal(sid, context_manager=cm)
        assert _send(cm, sid, "hi", model_id="model-a",
                     tool_ids=["read_file", "exec_cli"], workspace="/ws/a") is None

        users = [m for m in _read_conv(cm, sid)["messages"] if m["role"] == "user"]
        assert "terminal" not in users[0]["meta"]
        assert users[0]["meta"]["tool_ids"] == ["read_file", "exec_cli"]


# ---------------------------------------------------------------------------
# The snapshot must stay out of the model's context
# ---------------------------------------------------------------------------

def test_turn_to_message_dict_strips_the_snapshot() -> None:
    from runtime.context_manager import ConversationTurn

    turn = ConversationTurn(
        role="user", content="hi", timestamp="2026-01-01T00:00:00",
        meta={"model_id": "model-a"},
    )
    assert turn_to_message_dict(turn) == {
        "role": "user", "content": "hi", "timestamp": "2026-01-01T00:00:00",
    }


def test_assembled_context_never_leaks_the_snapshot() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        sid = cm.create_session()
        assert _send(cm, sid, "hello", model_id="model-a",
                     tool_ids=["read_file"], workspace="/ws/a") is None

        assembled = cm.assemble_context(sid, [])
        assert assembled, "context should contain the persisted turns"
        assert all("meta" not in msg for msg in assembled)
        assert any(msg["role"] == "user" for msg in assembled)


# ---------------------------------------------------------------------------
# HTTP integration: the non-stream endpoint persists the same snapshot
# ---------------------------------------------------------------------------

def test_non_stream_infer_records_the_snapshot() -> None:
    """``POST /v1/infer`` runs the shared pipeline, whose persist point builds
    the snapshot from the very values it hands to ``persist_conversation``."""
    import urllib.error
    import urllib.request
    from pathlib import Path
    from unittest.mock import patch

    from runtime.models import InferenceResult, Message, ModelConfig, ToolConfig
    from runtime.registry import ModelRegistry, ToolRegistry
    from runtime.runtime import Runtime
    from runtime.server import RuntimeHTTPServer

    model_reg = ModelRegistry()
    model_reg.register(ModelConfig(
        model_id="any-model",
        api_base="http://localhost:11434",
        model_name="mock-model",
        api_protocol="ollama",
    ))
    tool_reg = ToolRegistry()
    tool_reg.register(
        ToolConfig(tool_id="echo", tool_type="function", name="echo",
                   description="echo tool", parameters={}),
        callable_fn=lambda **kw: "echo result",
    )
    runtime = Runtime(model_reg, tool_reg)

    def _infer(request):
        msgs = list(request.messages or [])
        msgs.append(Message(role="assistant", content="ok"))
        return InferenceResult(success=True, messages=msgs)

    # POST /v1/infer now runs the streaming pipeline, which calls the generator
    # with cancel/gate kwargs; a stub has to accept them.
    def _infer_stream(request, **_pipeline_kwargs):
        for msg in (request.messages or []):
            yield msg
        yield Message(role="assistant", content="ok")

    runtime.infer = _infer
    runtime.infer_stream = _infer_stream

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with patch("runtime.server._MODELS_PATH", str(tmp_path / "models.json")), \
             patch("runtime.server._TOOLS_PATH", str(tmp_path / "tools.json")), \
             patch("runtime.server._PROMPT_TEMPLATES_PATH",
                   str(tmp_path / "prompt_templates.json")), \
             patch("runtime.server._DATA_DIR", str(tmp_path)):
            srv = RuntimeHTTPServer(runtime, chats_dir=str(tmp_path / "chats"))
            srv.start_background(host="127.0.0.1", port=0)
            try:
                payload = json.dumps({
                    "model_id": "any-model",
                    "tool_ids": ["echo"],
                    "workspace": "/ws/api",
                    "session_id": "new",
                    "messages": [{"role": "user", "content": "hello"}],
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{srv.port}/v1/infer",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        body = json.loads(resp.read())
                except urllib.error.HTTPError as exc:
                    raise AssertionError(
                        f"infer failed: {exc.code} {exc.read().decode()}"
                    ) from exc

                session_id = body["session_id"]
                assert session_id, body
                conv_path = tmp_path / "chats" / session_id / "conversation.json"
                with open(conv_path, "r", encoding="utf-8") as fh:
                    conv = json.load(fh)

                user = next(m for m in conv["messages"] if m["role"] == "user")
                assert user["meta"]["model_id"] == "any-model"
                assert user["meta"]["tool_ids"] == ["echo"]
                assert user["meta"]["workspace"] == "/ws/api"
                assert all(
                    "meta" not in m
                    for m in conv["messages"] if m["role"] != "user"
                )
            finally:
                srv.stop()
