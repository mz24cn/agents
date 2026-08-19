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
from unittest.mock import MagicMock, patch

import pytest

from runtime.context_manager import ContextManager
from runtime.models import InferenceRequest, Message, ModelConfig, ToolConfig
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer
from runtime.server_state import merge_stream_messages, persist_conversation

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


# ---------------------------------------------------------------------------
# 2. Incremental chunk merge == full merge
# ---------------------------------------------------------------------------


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

    with patch("runtime.server._MODELS_PATH", str(tmp_path / "models.json")), \
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


def test_continue_inference_resumes_interrupted_session(continue_server, tmp_path):
    """A session whose last stored turn is a tool result (i.e. inference was
    interrupted after a completed tool round) can be resumed with
    continue:true; after the final answer, continue is rejected."""
    srv = continue_server
    cm = srv._context_manager
    session_id = cm.create_session()

    # Simulate the persisted state after an interrupted inference:
    # [user, assistant(tool_calls), tool] \u2014 the last completed round was
    # incrementally persisted, but the process died before the final answer.
    cm.save_conversation(session_id, [
        _turn("user", "do something", timestamp="2026-08-12T15:00:00"),
        _turn("assistant", "", timestamp="2026-08-12T15:00:01",
              tool_calls=[{"id": "call_1", "name": "echo", "arguments": "{}"}]),
        _turn("tool", "echo result", timestamp="2026-08-12T15:00:02",
              name="echo", tool_use_id="call_1"),
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

    # The final assistant reply is appended; no extra user message is added.
    status, sess = _get_session(srv, session_id)
    assert status == 200
    roles = [msg["role"] for msg in sess["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert sess["messages"][-1]["content"] == "All done."

    # --- continue on a completed session must be rejected ---
    status, err = _post_json(srv, "/v1/infer/stream", {
        "session_id": session_id,
        "model_id": "test-model",
        "tool_ids": ["echo"],
        "continue": True,
        "messages": [],
    })
    assert status == 400, f"expected 400, got {status}: {err}"
    assert "cannot continue" in err.get("error", "")

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
    assert "cannot continue" in err2.get("error", "")


def test_continue_injects_interrupted_tool_result(continue_server, tmp_path):
    """When the last stored turn is an assistant with dangling tool_calls
    (e.g. corrupted/old data where the tool result was never persisted),
    continue must inject an interrupted tool result so the model API does
    not reject the request."""
    srv = continue_server
    cm = srv._context_manager
    session_id = cm.create_session()

    # Simulate a conversation whose final stored turn is a dangling
    # assistant tool-call (no paired tool result).
    cm.save_conversation(session_id, [
        _turn("user", "start", timestamp="2026-08-12T15:00:00"),
        _turn("assistant", "", timestamp="2026-08-12T15:00:01",
              tool_calls=[{"id": "call_1", "name": "echo", "arguments": "{}"}]),
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
    assert status == 200, f"continue with dangling tool_calls failed: {body[:500]}"

    assert captured_bodies, "expected the model API to be called"
    sent = captured_bodies[-1]
    sent_roles = [m["role"] for m in sent["messages"]]
    # the injected interrupted-tool result must be the last message sent to the model
    assert sent_roles[-1] == "tool", (
        f"expected an injected tool result before the assistant request, roles={sent_roles}"
    )
    last_tool = sent["messages"][-1]
    assert last_tool["role"] == "tool"
    assert "interrupted" in last_tool["content"]
    # Internal Message uses tool_use_id; OpenAI wire format requires tool_call_id.
    assert last_tool.get("tool_call_id") == "call_1"


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
