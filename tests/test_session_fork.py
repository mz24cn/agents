"""Tests for session forking (衍生新会话).

Covers the three layers of the feature:

  * ``ContextManager.fork_session``  — conversation truncation, meta rewrite,
    summary/memory reconciliation, anchor resolution rules;
  * ``SessionManager.on_session_forked`` / ``inherit_categories`` — index entry
    with inherited title + ``forked_from``, category inheritance;
  * ``POST /v1/sessions/{id}/fork`` — routing, error codes, and the fact that
    the fork is immediately loadable through the normal session GET.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from runtime.context_manager import (
    ContextManager,
    ConversationTurn,
    MemoryEntry,
    serialize_summary,
)
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer
from runtime.session_manager import SessionManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cm(tmp_dir: str) -> ContextManager:
    return ContextManager(infer_fn=lambda req: None, chats_dir=tmp_dir)


def _turn(
    role: str,
    content: str,
    ts: str,
    tool_calls: list | None = None,
    name: str | None = None,
    stat: dict | None = None,
) -> ConversationTurn:
    return ConversationTurn(
        role=role, content=content, timestamp=ts, tool_calls=tool_calls, name=name,
        stat=stat,
    )


def _seed_session(cm: ContextManager, session_id: str) -> None:
    """Write a two-turn conversation: loop call + loop-end assistant message.

    Layout (indices are what the client sees and forks on)::

        0 system      2026-01-01T00:00:00
        1 user        2026-01-01T00:00:01
        2 assistant   tool call          2026-01-01T00:00:02
        3 tool        result             2026-01-01T00:00:02
        4 assistant   loop end (forkable) 2026-01-01T00:00:03
        5 user        next question      2026-01-01T00:00:04
        6 assistant   loop end           2026-01-01T00:00:05
    """
    cm.save_conversation(session_id, [
        _turn("system", "sys prompt", "2026-01-01T00:00:00"),
        _turn("user", "first question", "2026-01-01T00:00:01"),
        _turn("assistant", "calling", "2026-01-01T00:00:02",
              tool_calls=[{"id": "c1", "name": "exec_shell", "arguments": {}}]),
        _turn("tool", "result", "2026-01-01T00:00:02", name="exec_shell"),
        _turn("assistant", "first answer", "2026-01-01T00:00:03"),
        _turn("user", "second question", "2026-01-01T00:00:04"),
        _turn("assistant", "second answer", "2026-01-01T00:00:05"),
    ])


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# ContextManager.fork_session
# ---------------------------------------------------------------------------

def test_fork_keeps_prefix_and_rewrites_meta() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        parent = cm.create_session()
        _seed_session(cm, parent)

        result = cm.fork_session(parent, 4)

        assert result["parent_session_id"] == parent
        assert result["cut_index"] == 4
        assert result["message_count"] == 5
        assert result["first_user_message"] == "first question"
        assert result["session_id"] != parent
        assert os.path.isdir(os.path.join(tmp_dir, result["session_id"]))

        data = _read_json(os.path.join(tmp_dir, result["session_id"], "conversation.json"))
        roles = [m["role"] for m in data["messages"]]
        assert roles == ["system", "user", "assistant", "tool", "assistant"]
        assert data["messages"][-1]["content"] == "first answer"

        meta = data["meta"]
        assert meta["session_id"] == result["session_id"]
        assert meta["turn_count"] == 5
        assert meta["parent_session_id"] == parent
        assert meta["forked_from_message_index"] == 4

        # The parent is untouched.
        parent_data = _read_json(os.path.join(tmp_dir, parent, "conversation.json"))
        assert len(parent_data["messages"]) == 7
        assert parent_data["meta"]["session_id"] == parent


def test_fork_inherits_session_bindings_from_parent_meta() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        parent = cm.create_session()
        _seed_session(cm, parent)
        cm.save_conversation(
            parent,
            cm.load_conversation(parent),
            last_total_tokens=1234,
            extra_meta={"model_id": "gpt-x", "tool_ids": ["t1"], "remote_env": "env-1"},
        )

        result = cm.fork_session(parent, 4)
        meta = _read_json(os.path.join(tmp_dir, result["session_id"], "conversation.json"))["meta"]

        assert meta["model_id"] == "gpt-x"
        assert meta["tool_ids"] == ["t1"]
        assert meta["remote_env"] == "env-1"
        assert meta["last_total_tokens"] == 1234


def test_fork_copies_summary_and_memory_within_the_cut() -> None:
    import tempfile

    from runtime.common import parse_front_matter

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        parent = cm.create_session()
        _seed_session(cm, parent)

        cm._atomic_write(
            os.path.join(tmp_dir, parent, "summary.md"),
            serialize_summary(
                {"session_id": parent, "summary_version": 2,
                 "summarized_up_to_turn": 2, "updated_at": "2026-01-01T00:00:02"},
                "summary body",
            ),
        )
        cm.save_memory(parent, [
            MemoryEntry(entry_type="fact", content="early fact", source_turn_index=1,
                        confidence=0.9, created_at="2026-01-01T00:00:01"),
            MemoryEntry(entry_type="fact", content="late fact", source_turn_index=5,
                        confidence=0.9, created_at="2026-01-01T00:00:04"),
        ])

        result = cm.fork_session(parent, 4)
        new_dir = os.path.join(tmp_dir, result["session_id"])

        summary_text, summary_fm = cm.get_summary(result["session_id"])
        assert summary_text == "summary body"
        assert summary_fm["session_id"] == result["session_id"]
        assert summary_fm["summarized_up_to_turn"] == 2

        entries = cm.load_memory(result["session_id"])
        assert [e.content for e in entries] == ["early fact"]
        memory_fm, _ = parse_front_matter(
            open(os.path.join(new_dir, "memory.md"), encoding="utf-8").read()
        )
        assert memory_fm["session_id"] == result["session_id"]


def test_fork_drops_summary_covering_dropped_turns() -> None:
    """A summary that describes turns the fork left out would be a lie.

    Memory is a different animal: entries recorded *inside* the copied prefix
    stay valid, so they come along even when the rolling summary is dropped.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        parent = cm.create_session()
        _seed_session(cm, parent)
        cm._atomic_write(
            os.path.join(tmp_dir, parent, "summary.md"),
            serialize_summary(
                {"session_id": parent, "summarized_up_to_turn": 5,
                 "updated_at": "2026-01-01T00:00:05"},
                "summary through the second question",
            ),
        )
        cm.save_memory(parent, [
            MemoryEntry(entry_type="fact", content="early fact", source_turn_index=1,
                        confidence=0.9, created_at="2026-01-01T00:00:01"),
            MemoryEntry(entry_type="fact", content="late fact", source_turn_index=5,
                        confidence=0.9, created_at="2026-01-01T00:00:04"),
        ])

        result = cm.fork_session(parent, 4)
        new_dir = os.path.join(tmp_dir, result["session_id"])
        assert not os.path.isfile(os.path.join(new_dir, "summary.md"))
        assert [e.content for e in cm.load_memory(result["session_id"])] == ["early fact"]


def test_fork_resolves_anchor_by_timestamp_when_index_is_stale() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        parent = cm.create_session()
        _seed_session(cm, parent)

        # Index points at a tool message → falls back to the timestamp scan.
        result = cm.fork_session(parent, 3, "2026-01-01T00:00:03")
        assert result["cut_index"] == 4
        assert result["message_count"] == 5

        result = cm.fork_session(parent, None, "2026-01-01T00:00:05")
        assert result["cut_index"] == 6
        assert result["message_count"] == 7


def test_resolve_fork_cut_rules() -> None:
    """Anchor resolution: verified index first, newest loop-end on fallback."""
    same_second = [
        {"role": "assistant", "timestamp": "T", "tool_calls": [{"id": "c"}]},
        {"role": "tool", "timestamp": "T"},
        {"role": "assistant", "timestamp": "T"},
        {"role": "assistant", "timestamp": "T"},
    ]
    # Only loop-ending assistant messages qualify, and the newest one sharing a
    # timestamp wins (that is the message the user clicked).
    assert ContextManager._resolve_fork_cut(same_second, None, "T") == 3
    assert ContextManager._resolve_fork_cut(same_second, 2, "") == 2

    # An index that does not describe a forkable message is not trusted; the
    # timestamp decides instead.  A verified index whose timestamp does not
    # match is rejected outright rather than forked at a guess.
    assert ContextManager._resolve_fork_cut(same_second, 1, "T") == 3

    with pytest.raises(ValueError):
        ContextManager._resolve_fork_cut(same_second, 2, "other")  # drifted
    with pytest.raises(ValueError):
        ContextManager._resolve_fork_cut(same_second, 0, "")   # tool calls
    with pytest.raises(ValueError):
        ContextManager._resolve_fork_cut(same_second, 1, "")   # tool result
    with pytest.raises(ValueError):
        ContextManager._resolve_fork_cut(same_second, None, "")


@pytest.mark.parametrize(
    "index,timestamp",
    [
        (3, ""),          # tool message
        (2, ""),          # assistant carrying tool calls
        (1, ""),          # user message
        (99, "1999-01-01T00:00:00"),  # out of range and unknown timestamp
        (None, ""),       # nothing to anchor on
    ],
)
def test_fork_rejects_non_forkable_anchors(index, timestamp) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        parent = cm.create_session()
        _seed_session(cm, parent)

        with pytest.raises(ValueError):
            cm.fork_session(parent, index, timestamp)

        # No half-written fork survives a rejected request.
        assert os.listdir(tmp_dir) == [parent]


def test_fork_missing_session_raises_file_not_found() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        with pytest.raises(FileNotFoundError):
            cm.fork_session("260101_000000", 0)


def test_fork_rejects_path_traversal_session_id() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        with pytest.raises(ValueError):
            cm.fork_session("../outside", 0)


def test_fork_allocates_unique_ids_within_the_same_second() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        parent = cm.create_session()
        _seed_session(cm, parent)

        first = cm.fork_session(parent, 4)["session_id"]
        second = cm.fork_session(parent, 4)["session_id"]
        assert first != second
        for session_id in (first, second):
            assert os.path.isfile(os.path.join(tmp_dir, session_id, "conversation.json"))


def test_fork_does_not_copy_journals_or_request_dumps() -> None:
    """A fork reuses history, not every artifact the parent produced."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        parent = cm.create_session()
        _seed_session(cm, parent)
        parent_dir = os.path.join(tmp_dir, parent)
        with open(os.path.join(parent_dir, "request_260101_000002_001.json"), "w") as fh:
            fh.write("{}")
        os.makedirs(os.path.join(parent_dir, "file_journals", "260101_000002"))
        os.makedirs(os.path.join(parent_dir, "sub_260101_000002"))

        result = cm.fork_session(parent, 4)
        new_dir = os.path.join(tmp_dir, result["session_id"])
        assert sorted(os.listdir(new_dir)) == ["conversation.json"]


# ---------------------------------------------------------------------------
# SessionManager index bookkeeping
# ---------------------------------------------------------------------------

def test_on_session_forked_inherits_title_and_marks_provenance() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        sm = SessionManager(chats_dir=tmp_dir)
        parent = "260101_000000"
        sm.on_session_created(parent, "first question")
        sm.set_title_manually(parent, "手动标题")

        title = sm.on_session_forked("260101_000010", parent)

        index = _read_json(os.path.join(tmp_dir, "index.json"))
        entry = index["260101_000010"]
        assert title == "手动标题"
        assert entry["title"] == "手动标题"
        assert entry["forked_from"] == parent
        assert entry["title_given"] is False

        # Parent metadata is untouched apart from its own manual title.
        assert "forked_from" not in index[parent]


def test_on_session_forked_falls_back_to_first_user_message() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        sm = SessionManager(chats_dir=tmp_dir)
        title = sm.on_session_forked(
            "260101_000010", "260101_000000", first_user_message="衍生分支的问题"
        )
        assert title == "衍生分支的问题"


def test_inherit_categories_attaches_fork_to_parent_categories() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        sm = SessionManager(chats_dir=tmp_dir)
        parent = "260101_000000"
        sm.on_session_created(parent, "question")
        with open(os.path.join(tmp_dir, "tree.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {"version": 1, "experimental": True,
                 "tree": [{"id": 1, "name": "A", "children": [
                     {"id": 2, "name": "B", "children": [parent]},
                     {"id": 3, "name": "C", "children": ["other"]},
                 ]}]},
                fh,
            )

        assert sm.session_categories(parent) == ["1/2"]
        sm.on_session_forked("260101_000010", parent)
        assert sm.inherit_categories("260101_000010", parent) == 1
        assert any(
            item["session_id"] == "260101_000010"
            for item in sm.list_sessions_by_category("1/2")
        )
        # A session without categories inherits nothing.
        assert sm.inherit_categories("260101_000020", "unknown-session") == 0


# ---------------------------------------------------------------------------
# Fork context guard (复制历史自带的 token 守卫)
# ---------------------------------------------------------------------------

def _make_cm_with_budget(
    tmp_dir: str,
    max_tokens: int,
    recent_turns_k: int = 10,
) -> ContextManager:
    """Return a ContextManager whose compression threshold is small enough to test.

    ``_fork_context_guard`` measures the fork's own history against 90 % of
    ``MAX_TOKENS_IN_CONTEXT``; overriding the constructor value keeps that
    threshold reachable with a handful of synthetic turns.
    """
    return ContextManager(
        infer_fn=lambda req: None,
        chats_dir=tmp_dir,
        max_tokens_in_context=max_tokens,
        recent_turns_k=recent_turns_k,
    )


def _seed_long_session(cm: ContextManager, session_id: str, heavy: int, light: int) -> int:
    """Seed user/assistant turns: *heavy* long answers first, then *light* short ones.

    The last message is a forkable assistant reply, so the returned index forks
    the whole conversation.
    """
    turns: list[ConversationTurn] = [
        _turn("system", "sys prompt", "2026-01-01T00:00:00")
    ]
    minute = 1
    filler = " ".join(f"w{i % 97}" for i in range(400))
    for i in range(heavy):
        ts = f"2026-01-01T00:{minute:02d}:00"
        turns.append(_turn("user", f"question {i}", ts))
        turns.append(_turn("assistant", filler, ts))
        minute += 1
    for i in range(light):
        ts = f"2026-01-01T00:{minute:02d}:00"
        turns.append(_turn("user", f"short {i}", ts))
        turns.append(_turn("assistant", "ok", ts))
        minute += 1
    cm.save_conversation(session_id, turns)
    return len(turns) - 1


def _summary_writer(summarized_up_to: int):
    """Return a ``compress_context_forced`` stand-in that writes ``summary.md``."""

    def _write(self: ContextManager, session_id: str) -> None:
        front_matter = {
            "session_id": session_id,
            "summarized_up_to_turn": summarized_up_to,
            "summary_version": 1,
            "updated_at": "2026-01-01T01:00:00",
        }
        self._atomic_write(
            self._summary_path(session_id),
            serialize_summary(front_matter, "compressed history of the forked turns"),
        )

    return _write


def test_fork_guard_leaves_small_history_alone() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)  # default 65536-token threshold
        parent = cm.create_session()
        _seed_session(cm, parent)

        with patch.object(
            ContextManager, "compress_context_forced", autospec=True
        ) as forced:
            result = cm.fork_session(parent, 4)

        forced.assert_not_called()
        guard = result["context_guard"]
        assert guard["compressed"] is False
        assert guard["warning"] == ""
        assert guard["estimated_tokens"] > 0
        assert guard["budget"] == int(65536 * ContextManager._FORK_GUARD_RATIO)  # noqa: SLF001


def test_fork_guard_compresses_history_that_cannot_fit() -> None:
    """A fork whose copied history overflows the budget gets its own summary.md.

    This is the case the guard exists for: the parent was compressed long ago,
    the fork cut before that point, so the copied turns are bigger than anything
    the parent ever sent \u2014 and the summary that made them fit stays behind.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        # budget = 200 * 0.9 = 180 tokens; heavy turns are ~400 tokens each.
        cm = _make_cm_with_budget(tmp_dir, max_tokens=200, recent_turns_k=2)
        parent = cm.create_session()
        anchor = _seed_long_session(cm, parent, heavy=4, light=2)

        with patch.object(
            ContextManager,
            "compress_context_forced",
            autospec=True,
            side_effect=_summary_writer(summarized_up_to=8),
        ) as forced:
            result = cm.fork_session(parent, anchor)

        new_id = result["session_id"]
        forced.assert_called_once()
        assert forced.call_args.args[1] == new_id

        guard = result["context_guard"]
        assert guard["compressed"] is True
        assert guard["warning"] == ""
        assert guard["estimated_tokens"] <= guard["budget"]

        summary_text, summary_fm = cm.get_summary(new_id)
        assert summary_text == "compressed history of the forked turns"
        assert summary_fm["session_id"] == new_id
        assert summary_fm["summarized_up_to_turn"] == 8
        # The parent was not touched by the fork's compression.
        assert cm.get_summary(parent)[0] == ""


def test_fork_guard_warns_when_compression_is_not_enough() -> None:
    """Compression ran but the recent turns kept verbatim still overflow."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        # recent_turns_k = 10 keeps the last 10 messages verbatim and the
        # summary covers only the first two, so the heavy turns stay and the
        # history remains over budget even after compressing.
        cm = _make_cm_with_budget(tmp_dir, max_tokens=200, recent_turns_k=10)
        parent = cm.create_session()
        anchor = _seed_long_session(cm, parent, heavy=4, light=2)

        with patch.object(
            ContextManager,
            "compress_context_forced",
            autospec=True,
            side_effect=_summary_writer(summarized_up_to=1),
        ) as forced:
            result = cm.fork_session(parent, anchor)

        forced.assert_called_once()
        guard = result["context_guard"]
        assert guard["compressed"] is True
        assert guard["estimated_tokens"] > guard["budget"]
        assert "still carries" in guard["warning"]
        # The fork itself is still usable.
        assert cm.load_conversation(result["session_id"])


def test_fork_guard_without_summary_model_reports_warning() -> None:
    """No summary model configured: the fork is created, and the risk is stated."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm_with_budget(tmp_dir, max_tokens=200)
        parent = cm.create_session()
        anchor = _seed_long_session(cm, parent, heavy=2, light=1)

        with patch.dict(os.environ, {"SUMMARY_MODEL_ID": ""}):
            result = cm.fork_session(parent, anchor)

        guard = result["context_guard"]
        assert guard["compressed"] is False
        assert "SUMMARY_MODEL_ID" in guard["warning"]
        assert cm.get_summary(result["session_id"])[0] == ""
        assert len(cm.load_conversation(result["session_id"])) == 7


def test_fork_guard_failure_keeps_the_fork() -> None:
    """An LLM failure while compressing must not delete the fork that was built."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm_with_budget(tmp_dir, max_tokens=200)
        parent = cm.create_session()
        anchor = _seed_long_session(cm, parent, heavy=2, light=1)

        with patch.object(
            ContextManager,
            "compress_context_forced",
            autospec=True,
            side_effect=RuntimeError("provider down"),
        ):
            result = cm.fork_session(parent, anchor)

        guard = result["context_guard"]
        assert guard["compressed"] is False
        assert "provider down" in guard["warning"]
        assert os.path.isdir(os.path.join(tmp_dir, result["session_id"]))
        assert len(cm.load_conversation(result["session_id"])) == 7


def test_fork_guard_reads_message_stats_not_session_meta() -> None:
    """The provider-reported size of the last copied message is the floor.

    ``meta.last_total_tokens`` describes the parent's newest turn, which a fork
    may not have copied, so it must not drive the decision either way.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm_with_budget(tmp_dir, max_tokens=1000)  # budget 900 tokens
        parent = cm.create_session()
        cm.save_conversation(parent, [
            _turn("system", "sys prompt", "2026-01-01T00:00:00"),
            _turn("user", "short question", "2026-01-01T00:00:01"),
            _turn("assistant", "short answer", "2026-01-01T00:00:02", stat={
                "prompt_tokens": 5000, "completion_tokens": 20, "total_tokens": 5020,
            }),
        ])

        with patch.object(
            ContextManager, "compress_context_forced", autospec=True
        ) as forced:
            result = cm.fork_session(parent, 2)

        from runtime.common import estimate_chat_prompt_tokens

        guard = result["context_guard"]
        local = estimate_chat_prompt_tokens(cm.assemble_context(parent, []))
        assert local <= guard["budget"]  # the text alone would never trigger

        forced.assert_called_once()
        # The stat, not the (tiny) text, is what put this fork over the budget.
        assert guard["compressed"] is False
        assert "returned nothing" in guard["warning"]

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm_with_budget(tmp_dir, max_tokens=1000)
        parent = cm.create_session()
        # Same short history, but the meta claims an enormous token count.
        cm.save_conversation(parent, [
            _turn("system", "sys prompt", "2026-01-01T00:00:00"),
            _turn("user", "short question", "2026-01-01T00:00:01"),
            _turn("assistant", "short answer", "2026-01-01T00:00:02"),
        ], last_total_tokens=999999)

        with patch.object(
            ContextManager, "compress_context_forced", autospec=True
        ) as forced:
            result = cm.fork_session(parent, 2)

        forced.assert_not_called()
        guard = result["context_guard"]
        assert guard["estimated_tokens"] <= guard["budget"]
        assert guard["warning"] == ""


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------

@pytest.fixture()
def server(tmp_path):
    """RuntimeHTTPServer on a random port, persisting under *tmp_path*."""
    with patch("runtime.server._MODELS_PATH", str(tmp_path / "models.json")), \
         patch("runtime.server._TOOLS_PATH", str(tmp_path / "tools.json")), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", str(tmp_path / "prompt_templates.json")), \
         patch("runtime.server._DATA_DIR", str(tmp_path)):
        srv = RuntimeHTTPServer(Runtime(ModelRegistry(), ToolRegistry()))
        srv.start_background(host="127.0.0.1", port=0)
        yield srv
        srv.stop()


def _post(srv: RuntimeHTTPServer, path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": raw.decode("utf-8", "replace")}


def _get(srv: RuntimeHTTPServer, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}{path}", timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_fork_endpoint_creates_loadable_session(server) -> None:
    cm = server._context_manager  # noqa: SLF001 - fixture-level white box
    sm = server._session_manager  # noqa: SLF001
    parent = cm.create_session()
    _seed_session(cm, parent)
    sm.on_session_created(parent, "first question")
    sm.set_title_manually(parent, "父会话标题")

    status, body = _post(server, f"/v1/sessions/{parent}/fork",
                         {"message_index": 4, "timestamp": "2026-01-01T00:00:03"})
    assert status == 200, body
    new_id = body["session_id"]
    assert body["parent_session_id"] == parent
    assert body["title"] == "父会话标题"
    assert body["message_count"] == 5

    # The fork is a first-class session: same load path the Sidebar uses.
    get_status, conversation = _get(server, f"/v1/sessions/{new_id}")
    assert get_status == 200
    assert len(conversation["messages"]) == 5
    assert conversation["meta"]["session_id"] == new_id

    list_status, listing = _get(server, "/v1/sessions?page=1&page_size=20")
    assert list_status == 200
    entries = {item["session_id"]: item for item in listing["sessions"]}
    assert new_id in entries
    assert entries[new_id]["forked_from"] == parent
    assert entries[new_id]["title"] == "父会话标题"


def test_fork_endpoint_unknown_session_returns_404(server) -> None:
    status, body = _post(server, "/v1/sessions/260101_000000/fork", {"message_index": 0})
    assert status == 404
    assert "error" in body


def test_fork_endpoint_rejects_non_forkable_anchor_with_400(server) -> None:
    cm = server._context_manager  # noqa: SLF001
    parent = cm.create_session()
    _seed_session(cm, parent)

    status, body = _post(server, f"/v1/sessions/{parent}/fork",
                         {"message_index": 3, "timestamp": "2026-01-01T00:00:02"})
    assert status == 400
    assert "error" in body
    # Nothing was forked.
    chats_dir = cm._chats_dir  # noqa: SLF001
    assert [d for d in os.listdir(chats_dir) if os.path.isdir(os.path.join(chats_dir, d))] == [parent]


def test_fork_endpoint_rejects_session_under_inference(server) -> None:
    from runtime.server_state import _session_state_lock, _session_statuses

    cm = server._context_manager  # noqa: SLF001
    parent = cm.create_session()
    _seed_session(cm, parent)

    with _session_state_lock:
        _session_statuses[parent] = "streaming"
    try:
        status, body = _post(server, f"/v1/sessions/{parent}/fork",
                             {"message_index": 4, "timestamp": "2026-01-01T00:00:03"})
    finally:
        with _session_state_lock:
            _session_statuses.pop(parent, None)

    assert status == 409
    assert "error" in body
    assert len([d for d in os.listdir(cm._chats_dir) if d != parent]) == 0  # noqa: SLF001


def test_fork_endpoint_reports_context_guard(server) -> None:
    """The fork response carries the token guard report for the copied history."""
    cm = server._context_manager  # noqa: SLF001 - fixture-level white box
    parent = cm.create_session()
    _seed_session(cm, parent)

    status, body = _post(server, f"/v1/sessions/{parent}/fork",
                         {"message_index": 4, "timestamp": "2026-01-01T00:00:03"})
    assert status == 200, body

    guard = body["context_guard"]
    assert set(guard) == {"estimated_tokens", "budget", "compressed", "warning"}
    assert guard["compressed"] is False
    assert guard["warning"] == ""
    assert guard["estimated_tokens"] > 0
    assert guard["estimated_tokens"] <= guard["budget"]
