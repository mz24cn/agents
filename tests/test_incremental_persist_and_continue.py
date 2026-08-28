"""Tests for incremental conversation persistence and "continue inference".

Covers three things introduced for the persistence hardening:

1. ``persist_conversation(compress=...)`` \u2014 inference-time incremental
   persistence must be able to skip the LLM context compression (only the
   final persist should trigger it).
2. Incremental chunking \u2014 merging ``collected_messages`` in chunks (one
   completed tool round at a time) must produce the exact same conversation
   turns as merging the full list, so a process killed mid-inference loses
   at most the last (incomplete) round.
3. ``continue: true`` \u2014 an inference request with no new user message that
   resumes an interrupted session from its existing context.  The backend
   validates the last turn is "continuable" (user / assistant-with-tool_calls
   / tool) and injects interrupted-tool markers when the last turn is a
   dangling assistant tool-call.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from runtime.context_manager import ContextManager, ConversationTurn
from runtime.models import InferenceRequest, Message, ModelConfig, ToolConfig
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer
from runtime.server_state import (
    IncrementalConversationPersister,
    merge_stream_messages,
    persist_conversation,
)
from runtime.handler_infer import _stream_batch_is_protocol_complete

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _openai_sse_text(content: str, tool_calls=None) -> bytes:
    """Build an OpenAI-style SSE response: optional tool-call delta, final
    content delta, usage frame and [DONE]."""
    lines = []
    if tool_calls:
        delta = {
            "role": "assistant",
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": tc[0], "arguments": tc[1]},
                }
                for tc in tool_calls
            ],
        }
        lines.append("data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n")
    if content:
        lines.append(
            "data: " + json.dumps({"choices": [{"delta": {"role": "assistant", "content": content}}]}) + "\n\n"
        )
    lines.append(
        "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}) + "\n\n"
    )
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def _make_resp(data: bytes):
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__iter__ = lambda self: iter(data.splitlines(keepends=True))
    mock_resp.read = lambda *a, **k: data
    mock_resp.close = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# 1. persist_conversation compress parameter
# ---------------------------------------------------------------------------


def test_persist_conversation_compress_false_skips_compress():
    cm = MagicMock(spec=ContextManager)
    cm.load_conversation.return_value = []
    cm.compress_context = MagicMock()

    exc = persist_conversation(cm, "s1", [], [], compress=False)
    assert exc is None
    cm.compress_context.assert_not_called()

    exc = persist_conversation(cm, "s1", [], [], compress=True)
    assert exc is None
    cm.compress_context.assert_called_once()


def test_persist_conversation_default_compresses():
    cm = MagicMock(spec=ContextManager)
    cm.load_conversation.return_value = []
    cm.compress_context = MagicMock()

    exc = persist_conversation(cm, "s1", [], [])
    assert exc is None
    cm.compress_context.assert_called_once()


def test_incremental_persist_updates_index_without_generating_title():
    cm = MagicMock(spec=ContextManager)
    cm.load_conversation.return_value = []
    sm = MagicMock()

    exc = persist_conversation(
        cm, "s1", [], [], session_manager=sm,
        compress=False, update_title=False,
    )

    assert exc is None
    sm.update_index.assert_called_once_with(
        "s1",
        last_total_tokens=None,
        generate_title=False,
        compression_updated=False,
    )


def test_next_persist_preserves_existing_history_without_explicit_removal(tmp_path):
    cm = ContextManager(infer_fn=lambda req: None, chats_dir=str(tmp_path))
    session_id = cm.create_session()
    cm.save_conversation(session_id, [
        ConversationTurn(role="user", content="old user", timestamp="t1"),
        ConversationTurn(
            role="assistant", content="partial", timestamp="t2",
            tool_calls=[{
                "id": "broken", "name": "read_file", "arguments": "{\"path\":",
            }],
        ),
    ])

    assert persist_conversation(
        cm,
        session_id,
        [Message(role="user", content="new user", timestamp="t3")],
        [Message(role="assistant", content="new answer", timestamp="t4")],
        compress=False,
    ) is None

    loaded = cm.load_conversation(session_id)
    assert [(turn.role, turn.content) for turn in loaded] == [
        ("user", "old user"),
        ("assistant", "partial"),
        ("user", "new user"),
        ("assistant", "new answer"),
    ]


def test_final_empty_persist_preserves_incremental_usage_and_triggers_compression(tmp_path):
    """A final empty persistence must not erase usage saved incrementally.

    The stream handler persists the completed ``assistant + usage`` round with
    ``compress=False`` and advances ``persisted_until``.  Its final persistence
    therefore receives an empty slice.  Compression must still see the usage
    count from the incremental save.
    """
    cm = ContextManager(
        infer_fn=lambda req: None,
        chats_dir=str(tmp_path),
        max_tokens_in_context=100,
    )
    session_id = cm.create_session()
    usage_round = [
        Message(role="assistant", content="done"),
        Message(
            role="usage",
            content=json.dumps({
                "prompt_tokens": 120,
                "completion_tokens": 5,
                "total_tokens": 125,
            }),
        ),
    ]

    assert persist_conversation(
        cm,
        session_id,
        [Message(role="user", content="do work")],
        usage_round,
        compress=False,
    ) is None
    assert cm.get_last_total_tokens(session_id) == 125

    with patch.object(cm, "compress_context", wraps=cm.compress_context) as compress:
        assert persist_conversation(
            cm, session_id, [], [], compress=True,
        ) is None

    assert cm.get_last_total_tokens(session_id) == 125
    compress.assert_called_once()
    assert compress.call_args.kwargs["last_total_tokens"] is None
    assert (tmp_path / session_id / "summary.md").is_file()


def test_interrupted_round_estimates_usage_and_drives_summary_threshold(tmp_path):
    """Missing provider usage must not persist 0/0 or suppress compression."""
    from runtime.server_state import StreamUsageEstimator

    cm = ContextManager(
        infer_fn=lambda req: None,
        chats_dir=str(tmp_path),
        max_tokens_in_context=104,
    )
    session_id = cm.create_session()
    previous = [
        Message(role="assistant", content="ok"),
        Message(role="usage", content=json.dumps({
            "prompt_tokens": 100,
            "completion_tokens": 2,
            "total_tokens": 102,
            "usage_reported": True,
        })),
    ]
    assert persist_conversation(cm, session_id, [], previous, compress=False) is None

    estimator = StreamUsageEstimator(
        cm, session_id, [Message(role="user", content="abc")],
    )
    failed = Message(role="assistant", content="Error: timeout")
    estimator.observe(failed)
    usage = estimator.terminal_usage_messages("model-x")
    assert len(usage) == 1
    stat = json.loads(usage[0].content)
    assert stat["prompt_tokens"] == 103
    assert stat["completion_tokens"] == 2
    assert stat["total_tokens"] == 105
    assert stat["estimated"] is True
    assert stat["usage_reported"] is False
    assert stat["cached_input_tokens"] is None
    assert stat["new_token_cache"] is None

    collected = [failed, *usage]
    assert persist_conversation(cm, session_id, [], collected, compress=True) is None
    stored = cm.load_conversation(session_id)[-1]
    assert stored.stat["total_tokens"] == 105
    assert stored.stat["estimated"] is True
    assert cm.get_last_total_tokens(session_id) == 105
    assert (tmp_path / session_id / "summary.md").is_file()


def test_explicit_provider_zero_usage_is_not_estimated(tmp_path):
    from runtime.server_state import StreamUsageEstimator

    cm = ContextManager(infer_fn=lambda req: None, chats_dir=str(tmp_path))
    session_id = cm.create_session()
    estimator = StreamUsageEstimator(
        cm, session_id, [Message(role="user", content="abc")],
    )
    usage = Message(role="usage", content=json.dumps({
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "usage_reported": True,
    }))
    estimator.observe(usage)
    stat = json.loads(usage.content)
    assert stat["prompt_tokens"] == 0
    assert stat["completion_tokens"] == 0
    assert "estimated" not in stat


# ---------------------------------------------------------------------------
# 2. Incremental chunk merge == full merge
# ---------------------------------------------------------------------------


def test_merge_stream_messages_isolates_interleaved_agents():
    """Concurrent agent deltas must never share an assistant/tool buffer."""
    stream = [
        Message(role="assistant", content="A1", agent_id="agent-a", name="Agent A"),
        Message(role="assistant", content="B1", agent_id="agent-b", name="Agent B"),
        Message(role="assistant", content="A2", agent_id="agent-a", name="Agent A"),
        Message(
            role="usage", agent_id="agent-b",
            content=json.dumps({"prompt_tokens": 2, "completion_tokens": 1}),
        ),
        Message(role="assistant", content="B2", agent_id="agent-b", name="Agent B"),
        Message(
            role="usage", agent_id="agent-a",
            content=json.dumps({"prompt_tokens": 3, "completion_tokens": 2}),
        ),
        Message(
            role="usage", agent_id="agent-b",
            content=json.dumps({"prompt_tokens": 4, "completion_tokens": 2}),
        ),
    ]

    turns, last_stat = merge_stream_messages(stream)

    assert [(turn.agent_id, turn.name, turn.content) for turn in turns] == [
        ("agent-a", "Agent A", "A1A2"),
        ("agent-b", "Agent B", "B1"),
        ("agent-b", "Agent B", "B2"),
    ]
    assert last_stat == {"prompt_tokens": 4, "completion_tokens": 2}


def test_merge_stream_messages_routes_unscoped_tool_result_to_declaring_agent():
    stream = [
        Message(
            role="assistant", agent_id="agent-a", name="Agent A",
            tool_calls=[{"id": "call-a", "name": "exec_shell", "arguments": "{}"}],
        ),
        Message(role="assistant", content="other", agent_id="agent-b", name="Agent B"),
        Message(role="usage", agent_id="agent-a", content="{}"),
        Message(role="usage", agent_id="agent-b", content="{}"),
        Message(
            role="tool", name="exec_shell", tool_use_id="call-a",
            content="result without legacy agent id",
        ),
    ]

    turns, _ = merge_stream_messages(stream)

    assert [(turn.role, turn.agent_id, turn.content) for turn in turns] == [
        ("assistant", "agent-a", ""),
        ("tool", "agent-a", "result without legacy agent id"),
        ("assistant", "agent-b", "other"),
    ]


def test_multi_agent_incremental_batch_waits_for_every_started_agent_usage():
    partial = [
        Message(role="assistant", content="A complete", agent_id="agent-a"),
        Message(role="assistant", content="B still streaming", agent_id="agent-b"),
        Message(role="usage", content="{}", agent_id="agent-a"),
    ]
    complete = [*partial, Message(role="usage", content="{}", agent_id="agent-b")]

    assert _stream_batch_is_protocol_complete(partial) is False
    assert _stream_batch_is_protocol_complete(complete) is True


def _round_tool_call():
    """collected_messages for one tool-call round: assistant(tool_calls) +
    usage + tool result."""
    return [
        Message(
            role="assistant",
            content="",
            timestamp="2026-08-12T15:00:00",
            tool_calls=[{"id": "call_1", "name": "echo", "arguments": "{}"}],
        ),
        Message(
            role="usage",
            timestamp="2026-08-12T15:00:01",
            content=json.dumps({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
        ),
        Message(role="tool", timestamp="2026-08-12T15:00:02", name="echo", content="echo result"),
    ]


def _round_final():
    """collected_messages for the final text round: assistant(content) + usage."""
    return [
        Message(role="assistant", content="Done.", timestamp="2026-08-12T15:00:03"),
        Message(
            role="usage",
            timestamp="2026-08-12T15:00:04",
            content=json.dumps({"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}),
        ),
    ]


def test_incremental_persist_saves_plain_assistant(tmp_path):
    """Incremental persist must save a plain-text assistant message even when
    no tool-call / tool-result round follows — otherwise refreshing the page
    (killing the connection) loses the assistant's reply.

    Use merge_stream_messages + persist_conversation(compress=False) to
    simulate what the handler does after each completed round.
    """
    from runtime.server_state import persist_conversation, merge_stream_messages
    from runtime.context_manager import ContextManager

    cm = ContextManager(infer_fn=lambda req: None, chats_dir=str(tmp_path))
    session_id = cm.create_session()

    # Simulate collected_messages after a full assistant text round:
    # merge_stream_messages receives [assistant_token, ..., usage] and
    # produces [assistant_turn] where the turn carries the combined content.
    msgs = [
        Message(role="assistant", content="Hello ", timestamp="2026-08-12T15:00:01"),
        Message(role="assistant", content="world!", timestamp="2026-08-12T15:00:02"),
        Message(role="usage", timestamp="2026-08-12T15:00:03",
                content=json.dumps({"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})),
    ]
    turns, _ = merge_stream_messages(msgs)
    exc = persist_conversation(cm, session_id, [], turns,
                               agent_ids=[], agent_nickname="", model_id="m",
                               tool_ids=[], workspace="", compress=False)
    assert exc is None, f"persist failed: {exc}"
    loaded = cm.load_conversation(session_id)
    assert any(t.content == "Hello world!" for t in loaded), \
        f"plain-text assistant not persisted: {[(t.role, t.content) for t in loaded]}"


def test_merge_stream_messages_chunked_equals_full():
    """Merging collected_messages in completed-round chunks must equal merging
    the full list \u2014 the invariant that makes incremental persistence safe."""
    full = _round_tool_call() + _round_final()

    full_turns, full_stat = merge_stream_messages(full)

    chunk1_turns, _ = merge_stream_messages(_round_tool_call())
    chunk2_turns, chunk2_stat = merge_stream_messages(_round_final())
    chunked_turns = chunk1_turns + chunk2_turns

    assert len(full_turns) == 3  # assistant(tool_calls), tool, assistant(text)
    assert [t.role for t in full_turns] == ["assistant", "tool", "assistant"]
    assert full_turns[0].tool_calls == [{"id": "call_1", "name": "echo", "arguments": "{}"}]
    assert full_turns[1].content == "echo result"
    assert full_turns[2].content == "Done."

    # chunked merge produces identical turns (field by field)
    assert len(chunked_turns) == len(full_turns)
    for ct, ft in zip(chunked_turns, full_turns):
        assert ct.role == ft.role
        assert ct.content == ft.content
        assert ct.tool_calls == ft.tool_calls
        assert ct.timestamp == ft.timestamp
        assert ct.stat == ft.stat
    assert chunk2_stat == full_stat


def test_incremental_tool_round_waits_for_result_before_persisting(tmp_path):
    """A usage frame must not persist a dangling assistant(tool_calls) turn.

    The handler retains the slice until the matching result has arrived so the
    protocol segment is written atomically. Persistence itself must not repair
    or delete existing messages.
    """
    cm = ContextManager(infer_fn=lambda req: None, chats_dir=str(tmp_path))
    session_id = cm.create_session()
    cm.save_conversation(session_id, [
        ConversationTurn(role="user", content="run echo", timestamp="t0"),
    ])

    round_messages = _round_tool_call()
    declaration_and_usage = round_messages[:2]
    assert _stream_batch_is_protocol_complete(declaration_and_usage) is False

    # Even if callers split the writes, persistence now preserves both halves.
    assert persist_conversation(
        cm, session_id, [], declaration_and_usage, compress=False,
    ) is None
    assert persist_conversation(
        cm, session_id, [], round_messages[2:], compress=False,
    ) is None
    assert [(turn.role, turn.content) for turn in cm.load_conversation(session_id)] == [
        ("user", "run echo"),
        ("assistant", ""),
        ("tool", "echo result"),
    ]
    assert persist_conversation(cm, session_id, [], [], compress=False) is None
    assert [(turn.role, turn.content) for turn in cm.load_conversation(session_id)] == [
        ("user", "run echo"),
        ("assistant", ""),
        ("tool", "echo result"),
    ]

    # Reset and persist the complete protocol segment atomically, as the
    # handler now does.
    cm.save_conversation(session_id, [
        ConversationTurn(role="user", content="run echo", timestamp="t0"),
    ])
    assert _stream_batch_is_protocol_complete(round_messages) is True
    assert persist_conversation(
        cm, session_id, [], round_messages, compress=False,
    ) is None
    loaded = cm.load_conversation(session_id)
    assert [turn.role for turn in loaded] == ["user", "assistant", "tool"]
    assert loaded[1].tool_calls[0]["id"] == "call_1"
    assert loaded[2].content == "echo result"


def test_shared_incremental_persister_pre_incremental_and_final(tmp_path):
    """The reusable persister owns the cursor and never duplicates turns."""
    cm = ContextManager(infer_fn=lambda req: None, chats_dir=str(tmp_path))
    session_id = cm.create_session()
    persister = IncrementalConversationPersister(
        context_manager=cm,
        session_id=session_id,
        original_messages=[Message(role="user", content="run echo", timestamp="t0")],
        tool_ids=["echo"],
        agent_ids=["agent-a"],
        model_id="test-model",
    )

    assert persister.pre_persist() is None
    assert [turn.role for turn in cm.load_conversation(session_id)] == ["user"]

    tool_round = _round_tool_call()
    assert persister.persist_completed(tool_round[:2]) is None
    assert persister.persisted_until == 0
    assert [turn.role for turn in cm.load_conversation(session_id)] == ["user"]

    assert persister.persist_completed(tool_round) is None
    assert persister.persisted_until == len(tool_round)
    assert [turn.role for turn in cm.load_conversation(session_id)] == [
        "user", "assistant", "tool",
    ]

    all_messages = tool_round + _round_final()
    assert persister.finalize(all_messages, compress=False) is None
    loaded = cm.load_conversation(session_id)
    assert [turn.role for turn in loaded] == [
        "user", "assistant", "tool", "assistant",
    ]
    assert loaded[-1].content == "Done."


# ---------------------------------------------------------------------------
# 3. Continue-inference API
# ---------------------------------------------------------------------------


@pytest.fixture()
def continue_server(tmp_path):
    """RuntimeHTTPServer with an echo tool and a mocked OpenAI model that
    produces a scripted SSE stream (tool-call round then final text)."""
    model_reg = ModelRegistry()
    model_reg.register(ModelConfig(
        model_id="test-model",
        api_base="http://localhost:9999",
        model_name="test",
        api_protocol="openai",
    ))
    tool_reg = ToolRegistry()
    tool_reg.register(
        ToolConfig(tool_id="echo", tool_type="function", name="echo", description="echo tool", parameters={}),
        callable_fn=lambda **kw: "echo result",
    )
    runtime = Runtime(model_registry=model_reg, tool_registry=tool_reg)

    # AgentManager does not use runtime.server._DATA_DIR; it resolves its
    # persistence directory from AGENTS_RUNTIME_DIR.  Keep it inside tmp_path
    # as well, otherwise tests that create agents leak RetryA/RetryB into the
    # user's real ~/.agents_runtime/agents directory.
    with patch.dict(os.environ, {"AGENTS_RUNTIME_DIR": str(tmp_path)}), \
         patch("runtime.server._MODELS_PATH", str(tmp_path / "models.json")), \
         patch("runtime.server._TOOLS_PATH", str(tmp_path / "tools.json")), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", str(tmp_path / "prompt_templates.json")), \
         patch("runtime.server._DATA_DIR", str(tmp_path)):
        srv = RuntimeHTTPServer(runtime, host="127.0.0.1", port=0, chats_dir=str(tmp_path / "chat_data"))
        srv.start_background()
        yield srv
        srv.stop()


def _url(server, path):
    return f"http://127.0.0.1:{server.port}{path}"


def _patch_model_urlopen(server, model_side_effect):
    """Context manager that patches urllib.request.urlopen so that local
    server requests go through the real network while model-API requests
    (e.g. http://localhost:9999) are answered by model_side_effect."""
    import urllib.request as _ur

    orig = _ur.urlopen
    port = server.port

    def _side_effect(request, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if f"127.0.0.1:{port}" in url or f"localhost:{port}" in url:
            return orig(request, **kwargs)
        return model_side_effect(request, **kwargs)

    return patch("urllib.request.urlopen", side_effect=_side_effect)


def _post_stream(server, data, model_side_effect):
    """POST /v1/infer/stream; model-API calls answered by model_side_effect.
    Returns (status, sse_body)."""
    import urllib.request
    import urllib.error

    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        _url(server, "/v1/infer/stream"),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _patch_model_urlopen(server, model_side_effect):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")


def _post_json(server, path, data):
    import urllib.request
    import urllib.error

    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        _url(server, path),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body.decode("utf-8", errors="replace")}


def _get_session(server, session_id):
    import urllib.request
    import urllib.error

    req = urllib.request.Request(_url(server, f"/v1/sessions/{session_id}"))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_stream_transport_failure_after_tool_result_persists_error_without_title(
    continue_server, monkeypatch,
):
    """An exhausted retry after a tool round must leave a visible failed turn."""
    import io
    import urllib.error

    srv = continue_server
    monkeypatch.setenv("MODEL_API_MAX_RETRIES", "1")
    monkeypatch.setenv("MODEL_API_RETRY_DELAY", "0")
    # Keep the test focused on the main inference. If the regression re-enables
    # title generation on failure, this spy still proves that it was attempted.
    srv._session_manager._infer_fn = MagicMock()

    tool_round = _openai_sse_text("", tool_calls=[("echo", "{}")])
    calls = 0

    def model_urlopen(request, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _make_resp(tool_round)
        raise urllib.error.HTTPError(
            url=request.full_url,
            code=502,
            msg="Bad Gateway",
            hdrs={},
            fp=io.BytesIO(b"error code: 502"),
        )

    status, body = _post_stream(srv, {
        "session_id": "new",
        "model_id": "test-model",
        "tool_ids": ["echo"],
        "messages": [{"role": "user", "content": "use echo"}],
    }, model_urlopen)

    assert status == 200
    assert "Error: HTTP 502: Bad Gateway" in body
    assert calls == 3  # tool round + initial failed request + one retry

    session_id = json.loads(
        next(line.removeprefix("data: ") for line in body.splitlines()
             if line.startswith("data: {") and '"type": "init"' in line)
    )["session_id"]
    _, session = _get_session(srv, session_id)
    assert [message["role"] for message in session["messages"]] == [
        "user", "assistant", "tool", "assistant",
    ]
    assert session["messages"][-1]["content"].startswith("Error: HTTP 502")
    assert session["meta"]["turn_count"] == 4
    srv._session_manager._infer_fn.assert_not_called()


def _scripted_urlopen(calls):
    """Build a urlopen side_effect that returns a scripted SSE response per
    outgoing request. `calls` is a list of bytes responses; the same response
    repeats for the rest of the request count."""
    import itertools

    counter = itertools.count()

    def _urlopen(request, **kwargs):
        i = next(counter)
        return _make_resp(calls[min(i, len(calls) - 1)])

    return _urlopen


def _turn(role, content="", timestamp="2026-08-12T15:00:00", tool_calls=None, name=None, tool_use_id=None):
    from runtime.context_manager import ConversationTurn
    return ConversationTurn(
        role=role,
        content=content,
        timestamp=timestamp,
        name=name,
        tool_calls=tool_calls,
        tool_use_id=tool_use_id,
    )


def test_continue_explicitly_replaces_final_assistant_without_classification(continue_server, tmp_path):
    """Continue removes the final assistant regardless of its content or status."""
    srv = continue_server
    cm = srv._context_manager
    session_id = cm.create_session()

    # A complete tool round was followed by an ordinary completed answer. The
    # backend must not try to classify it before honoring explicit Continue.
    cm.save_conversation(session_id, [
        _turn("user", "do something", timestamp="2026-08-12T15:00:00"),
        _turn("assistant", "", timestamp="2026-08-12T15:00:01",
              tool_calls=[{"id": "call_1", "name": "echo", "arguments": "{}"}]),
        _turn("tool", "echo result", timestamp="2026-08-12T15:00:02",
              name="echo", tool_use_id="call_1"),
        _turn("assistant", "A complete answer that the user chose to replace.",
              timestamp="2026-08-12T15:00:03"),
    ])

    # --- continue resumes with the final answer ---
    final_round = _openai_sse_text("All done.")
    status, body = _post_stream(srv, {
        "session_id": session_id,
        "model_id": "test-model",
        "tool_ids": ["echo"],
        "continue": True,
        "messages": [],
    }, _scripted_urlopen([final_round]))
    assert status == 200, f"continue failed: {body[:500]}"
    assert "data: [DONE]" in body
    init = json.loads(next(
        line.removeprefix("data: ")
        for line in body.splitlines()
        if line.startswith("data: {") and '"type": "init"' in line
    ))
    assert init["user_message_timestamp"] == "2026-08-12T15:00:00"

    # The final assistant reply is appended; no extra user message is added.
    status, sess = _get_session(srv, session_id)
    assert status == 200
    roles = [msg["role"] for msg in sess["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert sess["messages"][-1]["content"] == "All done."

    # --- revoke the user message -> empty session is not continuable ---
    user_ts = sess["messages"][0]["timestamp"]
    status, rev = _post_json(srv, f"/v1/sessions/{session_id}/revoke", {"timestamp": user_ts})
    assert status == 200, f"revoke failed: {rev}"

    status, err2 = _post_json(srv, "/v1/infer/stream", {
        "session_id": session_id,
        "model_id": "test-model",
        "tool_ids": ["echo"],
        "continue": True,
        "messages": [],
    })
    assert status == 400
    assert "final conversation turn" in err2.get("error", "")


def test_continue_removes_malformed_tool_call_turn_before_retry(continue_server, tmp_path):
    """A partial streamed tool declaration is removed rather than sent onward."""
    srv = continue_server
    cm = srv._context_manager
    session_id = cm.create_session()

    # Simulate a truncated tool-call delta with invalid JSON arguments.
    cm.save_conversation(session_id, [
        _turn("user", "start", timestamp="2026-08-12T15:00:00"),
        _turn("assistant", "", timestamp="2026-08-12T15:00:01",
              tool_calls=[{"id": "call_1", "name": "echo", "arguments": "{\"x\":"}]),
    ])

    final_round = _openai_sse_text("Continuing after interruption.")
    captured_bodies = []

    def _capture_urlopen(request, **kwargs):
        captured_bodies.append(json.loads(request.data.decode("utf-8")))
        return _make_resp(final_round)

    status, body = _post_stream(srv, {
        "session_id": session_id,
        "model_id": "test-model",
        "tool_ids": ["echo"],
        "continue": True,
        "messages": [],
    }, _capture_urlopen)
    assert status == 200, f"retry with malformed tool_calls failed: {body[:500]}"

    assert captured_bodies, "expected the model API to be called"
    sent = captured_bodies[-1]
    sent_roles = [m["role"] for m in sent["messages"]]
    assert sent_roles[-1] == "user"
    assert all(
        not any(tc.get("id") == "call_1" for tc in (m.get("tool_calls") or []))
        for m in sent["messages"]
    )


def test_group_chat_continue_accepts_retry_agent_and_replacement_roster(continue_server):
    srv = continue_server
    cm = srv._context_manager
    session_id = cm.create_session()
    srv._agent_manager.create(
        agent_id="RetryA", model_id="test-model", nickname="Retry A",
        tool_ids=["echo"], system_prompt="You are Retry A.",
    )
    srv._agent_manager.create(
        agent_id="RetryB", model_id="test-model", nickname="Retry B",
        tool_ids=["echo"], system_prompt="You are Retry B.",
    )
    cm.save_conversation(session_id, [
        _turn("user", "do something", timestamp="2026-08-12T15:00:00"),
        _turn("assistant", "Error: stream parse: timed out",
              timestamp="2026-08-12T15:00:01"),
    ])

    status, body = _post_stream(srv, {
        "session_id": session_id,
        "model_id": "test-model",
        "tool_ids": ["echo"],
        "continue": True,
        "retry_agent_id": "RetryB",
        "agent_ids": ["RetryA", "RetryB"],
        "messages": [],
    }, _scripted_urlopen([_openai_sse_text("taken over")]))
    assert status == 200, body[:500]

    _, sess = _get_session(srv, session_id)
    assert sess["messages"][-1]["agent_id"] == "RetryB"
    assert sess["meta"]["agent_ids"] == ["RetryA", "RetryB"]


def test_continue_requires_existing_session(continue_server):
    status, err = _post_json(continue_server, "/v1/infer/stream", {
        "session_id": "no_such_session",
        "model_id": "test-model",
        "tool_ids": [],
        "continue": True,
        "messages": [],
    })
    assert status == 404


def test_continue_requires_session_id(continue_server):
    status, err = _post_json(continue_server, "/v1/infer/stream", {
        "model_id": "test-model",
        "tool_ids": [],
        "continue": True,
        "messages": [],
    })
    assert status == 400
    assert "session_id" in err.get("error", "")
