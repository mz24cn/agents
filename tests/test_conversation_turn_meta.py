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
  - round-trip through ``load_conversation``
  - the snapshot never reaches the model-facing context
"""

from __future__ import annotations

import json
import tempfile

from runtime.context_manager import ContextManager, turn_to_message_dict
from runtime.models import Message
from runtime.server_state import build_turn_meta, persist_conversation


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
    """``POST /v1/infer`` goes through ``_persist_conversation``, which builds
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

    def _infer_stream(request):
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
