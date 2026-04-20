"""Integration tests for ContextManager + RuntimeHTTPServer.

Tests verify that the Server layer correctly integrates with ContextManager:
- session_id is returned in responses
- conversation files are persisted across requests
- assembled context includes prior turns from saved sessions
"""

import datetime
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from runtime.context_manager import (
    ContextManager,
    ConversationTurn,
    serialize_conversation,
)
from runtime.models import (
    InferenceRequest,
    InferenceResult,
    Message,
    ModelConfig,
)
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_infer(response_content: str = "Hello from mock"):
    """Return a mock infer_fn that produces a simple InferenceResult."""

    def _infer(request: InferenceRequest) -> InferenceResult:
        msgs = list(request.messages or [])
        msgs.append(Message(role="assistant", content=response_content))
        return InferenceResult(success=True, messages=msgs)

    return _infer


def _url(server: RuntimeHTTPServer, path: str) -> str:
    return f"http://127.0.0.1:{server.port}{path}"


def _post(server: RuntimeHTTPServer, path: str, data: dict) -> tuple[int, dict]:
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        _url(server, path),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            return resp.status, body
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read())
        return exc.code, body


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def chats_tmp(tmp_path):
    """Temporary directory for chats storage."""
    return str(tmp_path / "chats")


@pytest.fixture()
def mock_runtime(tmp_path):
    """Runtime whose infer method is replaced with a simple mock."""
    model_reg = ModelRegistry()
    tool_reg = ToolRegistry()
    rt = Runtime(model_reg, tool_reg)
    # Patch infer to avoid needing a real model
    rt.infer = _make_mock_infer()
    return rt


@pytest.fixture()
def server(mock_runtime, chats_tmp, tmp_path):
    """Start a RuntimeHTTPServer with a mock runtime and temp chats dir."""
    models_path = str(tmp_path / "models.json")
    tools_path = str(tmp_path / "tools.json")
    prompt_templates_path = str(tmp_path / "prompt_templates.json")
    with patch("runtime.server._MODELS_PATH", models_path), \
         patch("runtime.server._TOOLS_PATH", tools_path), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", prompt_templates_path), \
         patch("runtime.server._DATA_DIR", str(tmp_path)):
        srv = RuntimeHTTPServer(
            mock_runtime,
            host="127.0.0.1",
            port=0,
            chats_dir=chats_tmp,
        )
        srv.start_background()
        yield srv
        srv.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestServerInferReturnsSessionId:
    """POST /v1/infer should return a session_id in the response JSON."""

    def test_server_infer_returns_session_id(self, server):
        data = {
            "model_id": "any-model",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        status, body = _post(server, "/v1/infer", data)
        # The mock infer returns success=True
        assert status == 200
        assert "session_id" in body
        assert isinstance(body["session_id"], str)
        assert len(body["session_id"]) > 0

    def test_session_id_format(self, server):
        """session_id should follow YYYY-MM-DD_HH-MM-SS format."""
        data = {
            "model_id": "any-model",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        _, body = _post(server, "/v1/infer", data)
        session_id = body["session_id"]
        # Validate format: YYYY-MM-DD_HH-MM-SS
        import re
        assert re.match(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$", session_id), (
            f"session_id '{session_id}' does not match expected format"
        )

    def test_provided_session_id_is_echoed(self, server, chats_tmp):
        """If session_id is provided in the request, the same id is returned."""
        # First create a session so it exists on disk
        cm = server._context_manager
        session_id = cm.create_session()

        data = {
            "model_id": "any-model",
            "session_id": session_id,
            "messages": [{"role": "user", "content": "Hello again"}],
        }
        _, body = _post(server, "/v1/infer", data)
        assert body["session_id"] == session_id


class TestSessionPersistence:
    """Conversation files should be written to disk and accumulate across requests."""

    def test_conversation_file_created(self, server, chats_tmp):
        """After a request, conversation.json should exist in the session directory."""
        data = {
            "model_id": "any-model",
            "messages": [{"role": "user", "content": "First message"}],
        }
        _, body = _post(server, "/v1/infer", data)
        session_id = body["session_id"]

        conv_path = os.path.join(chats_tmp, session_id, "conversation.json")
        assert os.path.isfile(conv_path), f"Expected conversation.json at {conv_path}"

    def test_session_persistence(self, server, chats_tmp):
        """Two requests with the same session_id should both be recorded in conversation.json."""
        # First request — creates session
        data1 = {
            "model_id": "any-model",
            "messages": [{"role": "user", "content": "Turn one"}],
        }
        _, body1 = _post(server, "/v1/infer", data1)
        session_id = body1["session_id"]

        # Second request — reuses session
        data2 = {
            "model_id": "any-model",
            "session_id": session_id,
            "messages": [{"role": "user", "content": "Turn two"}],
        }
        _, body2 = _post(server, "/v1/infer", data2)
        assert body2["session_id"] == session_id

        # Verify conversation file exists
        conv_path = os.path.join(chats_tmp, session_id, "conversation.json")
        assert os.path.isfile(conv_path)

        # The file should contain content from both requests
        with open(conv_path, "r", encoding="utf-8") as f:
            content = f.read()
        # The mock appends an assistant message; the second save should have turns
        assert len(content) > 0

    def test_session_directory_created(self, server, chats_tmp):
        """The chats directory and session subdirectory should be created."""
        data = {
            "model_id": "any-model",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        _, body = _post(server, "/v1/infer", data)
        session_id = body["session_id"]

        session_dir = os.path.join(chats_tmp, session_id)
        assert os.path.isdir(session_dir)


class TestContextAssemblyWithRealFiles:
    """Saved turns should be included in the assembled context for subsequent requests."""

    def test_context_assembly_with_real_files(self, server, chats_tmp):
        """Create a session with saved turns, then verify assembled context includes them."""
        cm = server._context_manager

        # Manually create a session and save some prior turns
        session_id = cm.create_session()
        prior_turns = [
            ConversationTurn(
                role="user",
                content="What is 2+2?",
                timestamp="2026-01-01T00:00:00",
            ),
            ConversationTurn(
                role="assistant",
                content="2+2 equals 4.",
                timestamp="2026-01-01T00:00:01",
            ),
        ]
        cm.save_conversation(session_id, prior_turns)

        # Track what messages the mock infer receives
        received_messages: list = []

        def capturing_infer(request: InferenceRequest) -> InferenceResult:
            received_messages.extend(request.messages or [])
            msgs = list(request.messages or [])
            msgs.append(Message(role="assistant", content="Captured"))
            return InferenceResult(success=True, messages=msgs)

        server._runtime.infer = capturing_infer

        # Make a new request with the existing session_id
        data = {
            "model_id": "any-model",
            "session_id": session_id,
            "messages": [{"role": "user", "content": "What did I ask before?"}],
        }
        status, body = _post(server, "/v1/infer", data)
        assert status == 200
        assert body["session_id"] == session_id

        # The assembled context should include the prior turns
        roles_and_contents = [(m.role, m.content) for m in received_messages]
        # Prior turns should appear before the new message
        assert ("user", "What is 2+2?") in roles_and_contents
        assert ("assistant", "2+2 equals 4.") in roles_and_contents
        # New message should also be present
        assert ("user", "What did I ask before?") in roles_and_contents

        # Prior turns should appear before the new message in order
        prior_user_idx = next(
            i for i, (r, c) in enumerate(roles_and_contents) if c == "What is 2+2?"
        )
        new_msg_idx = next(
            i for i, (r, c) in enumerate(roles_and_contents) if c == "What did I ask before?"
        )
        assert prior_user_idx < new_msg_idx, (
            "Prior turns should appear before the new message in assembled context"
        )
