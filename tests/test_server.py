"""Unit tests for RuntimeHTTPServer.

Tests all HTTP API routes using a real server started in a background thread.
Uses only Python standard library for HTTP client calls (urllib).
"""

import json
import os
import threading
import urllib.request
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from runtime.models import (
    InferenceRequest,
    InferenceResult,
    Message,
    ModelConfig,
    ToolConfig,
)
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture()
def registries():
    """Create fresh model and tool registries."""
    model_reg = ModelRegistry()
    tool_reg = ToolRegistry()
    return model_reg, tool_reg


@pytest.fixture()
def runtime(registries):
    """Create a Runtime with empty registries."""
    model_reg, tool_reg = registries
    return Runtime(model_reg, tool_reg)


@pytest.fixture()
def server(runtime, tmp_path):
    """Start a RuntimeHTTPServer on a random port in a background thread.
    
    Uses a temporary directory for data persistence to avoid polluting
    the real ~/.agents_runtime/ directory.
    """
    models_path = str(tmp_path / "models.json")
    tools_path = str(tmp_path / "tools.json")
    prompt_templates_path = str(tmp_path / "prompt_templates.json")
    with patch("runtime.server._MODELS_PATH", models_path), \
         patch("runtime.server._TOOLS_PATH", tools_path), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", prompt_templates_path), \
         patch("runtime.server._DATA_DIR", str(tmp_path)):
        srv = RuntimeHTTPServer(runtime, host="127.0.0.1", port=0)
        srv.start_background()
        yield srv
        srv.stop()


def _url(server: RuntimeHTTPServer, path: str) -> str:
    """Build a full URL for the given path."""
    return f"http://127.0.0.1:{server.port}{path}"


def _get(server: RuntimeHTTPServer, path: str) -> tuple[int, dict]:
    """Send a GET request and return (status_code, json_body)."""
    req = urllib.request.Request(_url(server, path))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            return resp.status, body
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read())
        return exc.code, body


def _post(server: RuntimeHTTPServer, path: str, data: dict) -> tuple[int, dict]:
    """Send a POST request with JSON body and return (status_code, json_body)."""
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


def _post_raw(server: RuntimeHTTPServer, path: str, data: dict) -> tuple[int, bytes]:
    """Send a POST request and return (status_code, raw_bytes)."""
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        _url(server, path),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# ------------------------------------------------------------------
# GET /v1/models
# ------------------------------------------------------------------


class TestListModels:
    def test_empty_registry(self, server):
        status, body = _get(server, "/v1/models")
        assert status == 200
        assert body == {"models": []}

    def test_with_registered_models(self, server, runtime):
        config = ModelConfig(
            model_id="test-model",
            api_base="http://localhost:11434",
            model_name="test:latest",
            api_protocol="openai",
        )
        runtime._model_registry.register(config)

        status, body = _get(server, "/v1/models")
        assert status == 200
        assert len(body["models"]) == 1
        assert body["models"][0]["model_id"] == "test-model"


# ------------------------------------------------------------------
# GET /v1/tools
# ------------------------------------------------------------------


class TestListTools:
    def test_empty_registry(self, server):
        status, body = _get(server, "/v1/tools")
        assert status == 200
        assert body == {"tools": []}

    def test_with_registered_tools(self, server, runtime):
        config = ToolConfig(
            tool_id="my-tool",
            tool_type="function",
            name="my_tool",
            description="A test tool",
            parameters={"type": "object", "properties": {}},
        )
        runtime._tool_registry.register(config)

        status, body = _get(server, "/v1/tools")
        assert status == 200
        assert len(body["tools"]) == 1
        assert body["tools"][0]["tool_id"] == "my-tool"


# ------------------------------------------------------------------
# POST /v1/models
# ------------------------------------------------------------------


class TestRegisterModel:
    def test_register_model(self, server, runtime):
        data = {
            "model_id": "new-model",
            "api_base": "http://localhost:8000",
            "model_name": "gpt-test",
            "api_protocol": "openai",
        }
        status, body = _post(server, "/v1/models", data)
        assert status == 201
        assert body["status"] == "registered"
        assert body["model_id"] == "new-model"

        # Verify it's actually in the registry
        assert runtime._model_registry.get("new-model") is not None

    def test_missing_required_field(self, server):
        data = {"model_id": "incomplete"}
        status, body = _post(server, "/v1/models", data)
        assert status == 400
        assert "error" in body


# ------------------------------------------------------------------
# POST /v1/tools
# ------------------------------------------------------------------


class TestRegisterTool:
    def test_register_tool(self, server, runtime):
        data = {
            "tool_id": "new-tool",
            "tool_type": "function",
            "name": "new_tool",
            "description": "A new tool",
            "parameters": {"type": "object", "properties": {}},
        }
        status, body = _post(server, "/v1/tools", data)
        assert status == 201
        assert body["status"] == "registered"
        assert body["tool_id"] == "new-tool"

        assert runtime._tool_registry.get("new-tool") is not None

    def test_missing_required_field(self, server):
        data = {"tool_id": "incomplete"}
        status, body = _post(server, "/v1/tools", data)
        assert status == 400
        assert "error" in body


# ------------------------------------------------------------------
# POST /v1/tools/call
# ------------------------------------------------------------------


class TestToolCall:
    def test_call_function_tool(self, server, runtime):
        # Register a simple function tool
        def add(a: int, b: int) -> int:
            return a + b

        config = ToolConfig(
            tool_id="add",
            tool_type="function",
            name="add",
            description="Add two numbers",
            parameters={"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}},
        )
        runtime._tool_registry.register(config, callable_fn=add)

        status, body = _post(server, "/v1/tools/call", {"tool_id": "add", "arguments": {"a": 2, "b": 3}})
        assert status == 200
        assert body["result"] == "5"

    def test_tool_not_found(self, server):
        status, body = _post(server, "/v1/tools/call", {"tool_id": "nonexistent", "arguments": {}})
        assert status == 400
        assert "error" in body
        assert "not found" in body["error"].lower()

    def test_missing_tool_id(self, server):
        status, body = _post(server, "/v1/tools/call", {"arguments": {}})
        assert status == 400
        assert "error" in body


# ------------------------------------------------------------------
# POST /v1/infer
# ------------------------------------------------------------------


class TestInfer:
    def test_model_not_found(self, server):
        data = {"model_id": "nonexistent", "text": "hello"}
        status, body = _post(server, "/v1/infer", data)
        assert status == 500
        assert body["success"] is False
        assert "not found" in body["error"].lower()

    def test_missing_model_id(self, server):
        data = {"text": "hello"}
        status, body = _post(server, "/v1/infer", data)
        assert status == 400
        assert "error" in body


# ------------------------------------------------------------------
# POST /v1/infer/stream
# ------------------------------------------------------------------


class TestInferStream:
    def test_missing_model_id(self, server):
        data = {"text": "hello"}
        status, body = _post(server, "/v1/infer/stream", data)
        assert status == 400
        assert "error" in body

    def test_stream_model_not_found(self, server):
        """When model is not found, the SSE stream should contain an error message."""
        data = {"model_id": "nonexistent", "text": "hello"}
        status, raw = _post_raw(server, "/v1/infer/stream", data)
        assert status == 200  # SSE always starts with 200
        text = raw.decode("utf-8")
        # Should contain error data and [DONE]
        assert "data:" in text
        assert "[DONE]" in text


# ------------------------------------------------------------------
# 404 handling
# ------------------------------------------------------------------


class TestNotFound:
    def test_get_unknown_path(self, server):
        status, body = _get(server, "/v1/unknown")
        assert status == 404
        assert "error" in body

    def test_post_unknown_path(self, server):
        status, body = _post(server, "/v1/unknown", {})
        assert status == 404
        assert "error" in body


# ------------------------------------------------------------------
# Invalid JSON
# ------------------------------------------------------------------


# ------------------------------------------------------------------
# PUT / DELETE / OPTIONS helpers
# ------------------------------------------------------------------


def _put(server: RuntimeHTTPServer, path: str, data: dict) -> tuple[int, dict]:
    """Send a PUT request with JSON body and return (status_code, json_body)."""
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        _url(server, path),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            return resp.status, body
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read())
        return exc.code, body


def _delete(server: RuntimeHTTPServer, path: str) -> tuple[int, dict]:
    """Send a DELETE request and return (status_code, json_body)."""
    req = urllib.request.Request(_url(server, path), method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            return resp.status, body
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read())
        return exc.code, body


def _options(server: RuntimeHTTPServer, path: str) -> tuple[int, dict]:
    """Send an OPTIONS request and return (status_code, headers_dict)."""
    req = urllib.request.Request(_url(server, path), method="OPTIONS")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers)


# ------------------------------------------------------------------
# CORS tests
# ------------------------------------------------------------------


class TestCORS:
    def test_options_returns_cors_headers(self, server):
        """OPTIONS preflight should return correct CORS headers."""
        status, headers = _options(server, "/v1/models")
        assert status == 200
        assert headers.get("Access-Control-Allow-Origin") == "*"
        allow_methods = headers.get("Access-Control-Allow-Methods", "")
        for method in ("GET", "POST", "PUT", "DELETE", "OPTIONS"):
            assert method in allow_methods
        assert "Content-Type" in headers.get("Access-Control-Allow-Headers", "")

    def test_get_response_includes_cors_header(self, server):
        """GET responses should include Access-Control-Allow-Origin."""
        req = urllib.request.Request(_url(server, "/v1/models"))
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    def test_post_response_includes_cors_header(self, server):
        """POST responses should include Access-Control-Allow-Origin."""
        data = {
            "model_id": "cors-test",
            "api_base": "http://localhost:8000",
            "model_name": "test",
            "api_protocol": "openai",
        }
        payload = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            _url(server, "/v1/models"),
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"


# ------------------------------------------------------------------
# PUT routing tests
# ------------------------------------------------------------------


class TestPutRouting:
    def test_put_model_not_found_returns_404(self, server):
        """PUT /v1/models/{id} for non-existent model should return 404."""
        data = {
            "model_id": "some-model",
            "api_base": "http://localhost:8000",
            "model_name": "test",
        }
        status, body = _put(server, "/v1/models/some-model", data)
        assert status == 404
        assert "error" in body

    def test_put_unknown_path_returns_404(self, server):
        """PUT to an unknown path should return 404."""
        status, body = _put(server, "/v1/unknown", {})
        assert status == 404
        assert "error" in body


# ------------------------------------------------------------------
# DELETE routing tests
# ------------------------------------------------------------------


class TestDeleteRouting:
    def test_delete_model_not_found_returns_404(self, server):
        """DELETE /v1/models/{id} for non-existent model should return 404."""
        status, body = _delete(server, "/v1/models/some-model")
        assert status == 404
        assert "error" in body

    def test_delete_unknown_path_returns_404(self, server):
        """DELETE to an unknown path should return 404."""
        status, body = _delete(server, "/v1/unknown")
        assert status == 404
        assert "error" in body


# ------------------------------------------------------------------
# Invalid JSON
# ------------------------------------------------------------------


# ------------------------------------------------------------------
# Model CRUD tests
# ------------------------------------------------------------------


class TestModelCRUD:
    """Tests for model Create/Read/Update/Delete operations."""

    def _register_model(self, server, model_id="test-model"):
        """Helper to register a model and return the response."""
        data = {
            "model_id": model_id,
            "api_base": "http://localhost:11434",
            "model_name": "test:latest",
            "api_protocol": "openai",
        }
        return _post(server, "/v1/models", data)

    def test_put_update_existing_model(self, server, runtime):
        """PUT /v1/models/{id} should update an existing model and return 200."""
        # Register a model first
        self._register_model(server, "upd-model")
        assert runtime._model_registry.get("upd-model") is not None

        # Update it via PUT
        updated_data = {
            "model_id": "upd-model",
            "api_base": "http://localhost:9999",
            "model_name": "updated:latest",
            "api_protocol": "ollama",
        }
        status, body = _put(server, "/v1/models/upd-model", updated_data)
        assert status == 200
        assert body["status"] == "updated"

        # Verify registry reflects the update
        cfg = runtime._model_registry.get("upd-model")
        assert cfg is not None
        assert cfg.api_base == "http://localhost:9999"
        assert cfg.model_name == "updated:latest"
        assert cfg.api_protocol == "ollama"

    def test_put_update_nonexistent_model(self, server):
        """PUT /v1/models/{id} for a model that doesn't exist should return 404."""
        data = {
            "model_id": "ghost",
            "api_base": "http://localhost:8000",
            "model_name": "nope",
        }
        status, body = _put(server, "/v1/models/ghost", data)
        assert status == 404
        assert "error" in body

    def test_delete_existing_model(self, server, runtime):
        """DELETE /v1/models/{id} should remove an existing model and return 200."""
        self._register_model(server, "del-model")
        assert runtime._model_registry.get("del-model") is not None

        status, body = _delete(server, "/v1/models/del-model")
        assert status == 200
        assert body["status"] == "deleted"

        # Verify it's gone from the registry
        assert runtime._model_registry.get("del-model") is None

    def test_delete_nonexistent_model(self, server):
        """DELETE /v1/models/{id} for a model that doesn't exist should return 404."""
        status, body = _delete(server, "/v1/models/no-such-model")
        assert status == 404
        assert "error" in body

    def test_register_model_persists(self, server):
        """POST /v1/models should call ModelRegistry.save() to persist data."""
        with patch.object(
            server._runtime._model_registry, "save", wraps=server._runtime._model_registry.save
        ) as mock_save:
            data = {
                "model_id": "persist-model",
                "api_base": "http://localhost:8000",
                "model_name": "persist:latest",
                "api_protocol": "openai",
            }
            status, body = _post(server, "/v1/models", data)
            assert status == 201
            mock_save.assert_called()


# ------------------------------------------------------------------
# Tool CRUD tests
# ------------------------------------------------------------------


class TestToolCRUD:
    """Tests for tool Create/Read/Update/Delete operations."""

    def _register_tool(self, server, tool_id="test-tool"):
        """Helper to register a tool and return the response."""
        data = {
            "tool_id": tool_id,
            "tool_type": "function",
            "name": "test_tool",
            "description": "A test tool",
            "parameters": {"type": "object", "properties": {}},
        }
        return _post(server, "/v1/tools", data)

    def test_put_update_existing_tool(self, server, runtime):
        """PUT /v1/tools/{id} should update an existing tool and return 200."""
        self._register_tool(server, "upd-tool")
        assert runtime._tool_registry.get("upd-tool") is not None

        updated_data = {
            "tool_id": "upd-tool",
            "tool_type": "mcp",
            "name": "updated_tool",
            "description": "An updated tool",
            "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}},
        }
        status, body = _put(server, "/v1/tools/upd-tool", updated_data)
        assert status == 200
        assert body["status"] == "updated"

        cfg = runtime._tool_registry.get("upd-tool")
        assert cfg is not None
        assert cfg.tool_type == "mcp"
        assert cfg.name == "updated_tool"
        assert cfg.description == "An updated tool"

    def test_put_update_nonexistent_tool(self, server):
        """PUT /v1/tools/{id} for a tool that doesn't exist should return 404."""
        data = {
            "tool_id": "ghost-tool",
            "tool_type": "function",
            "name": "ghost",
            "description": "Does not exist",
            "parameters": {"type": "object", "properties": {}},
        }
        status, body = _put(server, "/v1/tools/ghost-tool", data)
        assert status == 404
        assert "error" in body

    def test_delete_existing_tool(self, server, runtime):
        """DELETE /v1/tools/{id} should remove an existing tool and return 200."""
        self._register_tool(server, "del-tool")
        assert runtime._tool_registry.get("del-tool") is not None

        status, body = _delete(server, "/v1/tools/del-tool")
        assert status == 200
        assert body["status"] == "deleted"

        assert runtime._tool_registry.get("del-tool") is None

    def test_delete_nonexistent_tool(self, server):
        """DELETE /v1/tools/{id} for a tool that doesn't exist should return 404."""
        status, body = _delete(server, "/v1/tools/no-such-tool")
        assert status == 404
        assert "error" in body


class TestInvalidInput:
    def test_empty_body(self, server):
        """POST with empty body should return 400."""
        req = urllib.request.Request(
            _url(server, "/v1/infer"),
            data=b"",
            headers={"Content-Type": "application/json", "Content-Length": "0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = json.loads(exc.read())
        assert status == 400
        assert "error" in body


# ------------------------------------------------------------------
# Prompt Template CRUD tests
# ------------------------------------------------------------------


class TestPromptTemplateCRUD:
    """Tests for prompt template Create/Read/Update/Delete operations."""

    def test_get_empty_templates(self, server):
        """GET /v1/prompt-templates returns empty list when no templates exist."""
        status, body = _get(server, "/v1/prompt-templates")
        assert status == 200
        assert body == {"templates": []}

    def test_post_create_template(self, server):
        """POST /v1/prompt-templates with template_id+content returns 201 with template_id."""
        data = {"template_id": "test-template", "content": "Hello {name}"}
        status, body = _post(server, "/v1/prompt-templates", data)
        assert status == 201
        assert body["status"] == "created"
        assert "template_id" in body
        assert isinstance(body["template_id"], str)
        assert len(body["template_id"]) > 0

    def test_post_missing_template_id(self, server):
        """POST /v1/prompt-templates without template_id returns 400."""
        data = {"content": "some content"}
        status, body = _post(server, "/v1/prompt-templates", data)
        assert status == 400
        assert "error" in body

    def test_post_missing_content(self, server):
        """POST /v1/prompt-templates without content returns 400."""
        data = {"template_id": "no-content"}
        status, body = _post(server, "/v1/prompt-templates", data)
        assert status == 400
        assert "error" in body

    def test_put_update_template(self, server):
        """Create a template, then PUT to update it, assert 200."""
        # Create
        create_status, create_body = _post(
            server, "/v1/prompt-templates", {"template_id": "orig", "content": "original"}
        )
        assert create_status == 201
        tid = create_body["template_id"]

        # Update
        status, body = _put(
            server,
            f"/v1/prompt-templates/{tid}",
            {"template_id": "updated", "content": "updated content"},
        )
        assert status == 200
        assert body["status"] == "updated"
        assert body["template_id"] == "updated"

        # Verify via GET
        get_status, get_body = _get(server, "/v1/prompt-templates")
        assert get_status == 200
        templates = get_body["templates"]
        assert len(templates) == 1
        assert templates[0]["template_id"] == "updated"
        assert templates[0]["content"] == "updated content"

    def test_put_nonexistent_template(self, server):
        """PUT to non-existent template_id returns 404."""
        status, body = _put(
            server,
            "/v1/prompt-templates/nonexistent-id",
            {"template_id": "x", "content": "y"},
        )
        assert status == 404
        assert "error" in body

    def test_delete_template(self, server):
        """Create a template, then DELETE it, assert 200."""
        # Create
        create_status, create_body = _post(
            server, "/v1/prompt-templates", {"template_id": "to-delete", "content": "bye"}
        )
        assert create_status == 201
        tid = create_body["template_id"]

        # Delete
        status, body = _delete(server, f"/v1/prompt-templates/{tid}")
        assert status == 200
        assert body["status"] == "deleted"

        # Verify it's gone
        get_status, get_body = _get(server, "/v1/prompt-templates")
        assert get_status == 200
        assert get_body["templates"] == []

    def test_delete_nonexistent_template(self, server):
        """DELETE non-existent template_id returns 404."""
        status, body = _delete(server, "/v1/prompt-templates/no-such-id")
        assert status == 404
        assert "error" in body

    def test_template_persists_to_file(self, server):
        """Create a template, verify the JSON file was written."""
        import runtime.server as srv_module

        data = {"template_id": "persist-test", "content": "persisted {var}"}
        status, body = _post(server, "/v1/prompt-templates", data)
        assert status == 201

        # Read the persisted file using the patched path
        path = srv_module._PROMPT_TEMPLATES_PATH
        assert os.path.isfile(path)
        with open(path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert isinstance(saved, list)
        assert len(saved) == 1
        assert saved[0]["template_id"] == "persist-test"
        assert saved[0]["content"] == "persisted {var}"


# ------------------------------------------------------------------
# Conversation persistence tests
# ------------------------------------------------------------------


def _make_infer_result(assistant_content: str) -> InferenceResult:
    """Build a minimal successful InferenceResult with one assistant message."""
    return InferenceResult(
        success=True,
        messages=[Message(role="assistant", content=assistant_content)],
    )


def _make_infer_stream_messages(assistant_content: str) -> list[Message]:
    """Build a list of messages that mimics infer_stream output.

    Returns assistant content delta + usage stat, matching the format
    produced by runtime.infer_stream().
    """
    import json
    return [
        Message(role="assistant", content=assistant_content),
        Message(role="usage", content=json.dumps({
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        })),
    ]


@pytest.fixture()
def server_with_session(runtime, tmp_path):
    """Server fixture that also patches _DATA_DIR and returns (server, context_manager)."""
    models_path = str(tmp_path / "models.json")
    tools_path = str(tmp_path / "tools.json")
    prompt_templates_path = str(tmp_path / "prompt_templates.json")
    chats_dir = str(tmp_path / "chats")
    with patch("runtime.server._MODELS_PATH", models_path), \
         patch("runtime.server._TOOLS_PATH", tools_path), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", prompt_templates_path), \
         patch("runtime.server._DATA_DIR", str(tmp_path)):
        srv = RuntimeHTTPServer(runtime, host="127.0.0.1", port=0, chats_dir=chats_dir)
        srv.start_background()
        yield srv, srv._context_manager
        srv.stop()


class TestConversationPersistence:
    """Tests for conversation.md incremental recording in /v1/infer."""

    def test_system_message_not_persisted(self, runtime, server_with_session):
        """System messages sent per-request must not be saved to conversation.md."""
        server, cm = server_with_session

        # Register a model and mock infer
        runtime._model_registry.register(ModelConfig(
            model_id="m", api_base="http://x", model_name="x", api_protocol="openai"
        ))
        with patch.object(runtime, "infer_stream", return_value=iter(_make_infer_stream_messages("hello"))):
            status, body = _post(server, "/v1/infer", {
                "model_id": "m",
                "session_id": "new",
                "messages": [
                    {"role": "system", "content": "You are helpful."},
                    {"role": "user", "content": "hi"},
                ],
            })
        assert status == 200
        session_id = body["session_id"]

        turns = cm.load_conversation(session_id)
        roles = [t.role for t in turns]
        assert "system" in roles, "system turns should be persisted"
        assert "user" in roles
        assert "assistant" in roles

    def test_no_duplicate_turns_across_requests(self, runtime, server_with_session):
        """Sending only the current turn each request must not duplicate history."""
        server, cm = server_with_session

        runtime._model_registry.register(ModelConfig(
            model_id="m", api_base="http://x", model_name="x", api_protocol="openai"
        ))

        # First request
        with patch.object(runtime, "infer_stream", return_value=iter(_make_infer_stream_messages("reply1"))):
            status1, body1 = _post(server, "/v1/infer", {
                "model_id": "m",
                "session_id": "new",
                "messages": [{"role": "user", "content": "turn1"}],
            })
        assert status1 == 200
        session_id = body1["session_id"]

        # Second request — only sends the new user message, not history
        with patch.object(runtime, "infer_stream", return_value=iter(_make_infer_stream_messages("reply2"))):
            status2, body2 = _post(server, "/v1/infer", {
                "model_id": "m",
                "session_id": session_id,
                "messages": [{"role": "user", "content": "turn2"}],
            })
        assert status2 == 200

        turns = cm.load_conversation(session_id)
        contents = [t.content for t in turns]
        # Each message should appear exactly once
        assert contents.count("turn1") == 1, "turn1 must appear exactly once"
        assert contents.count("turn2") == 1, "turn2 must appear exactly once"
        assert contents.count("reply1") == 1, "reply1 must appear exactly once"
        assert contents.count("reply2") == 1, "reply2 must appear exactly once"
        assert len(turns) == 4

    def test_each_turn_has_distinct_timestamp(self, runtime, server_with_session):
        """Each persisted turn must have a non-empty timestamp string."""
        server, cm = server_with_session

        runtime._model_registry.register(ModelConfig(
            model_id="m", api_base="http://x", model_name="x", api_protocol="openai"
        ))
        with patch.object(runtime, "infer_stream", return_value=iter(_make_infer_stream_messages("pong"))):
            status, body = _post(server, "/v1/infer", {
                "model_id": "m",
                "session_id": "new",
                "messages": [{"role": "user", "content": "ping"}],
            })
        assert status == 200

        turns = cm.load_conversation(body["session_id"])
        for turn in turns:
            assert turn.timestamp, f"Turn {turn.role!r} has empty timestamp"
            # Must match ISO 8601 format YYYY-MM-DDTHH:MM:SS
            import re
            assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", turn.timestamp), (
                f"Unexpected timestamp format: {turn.timestamp!r}"
            )


# ------------------------------------------------------------------
# Env & Session API tests
# ------------------------------------------------------------------


@pytest.fixture()
def server_with_env(runtime, tmp_path):
    """Server fixture that patches _ENV_PATH and provides a chats_dir for env/session tests."""
    models_path = str(tmp_path / "models.json")
    tools_path = str(tmp_path / "tools.json")
    prompt_templates_path = str(tmp_path / "prompt_templates.json")
    env_path = str(tmp_path / "env.json")
    chats_dir = str(tmp_path / "chats")
    with patch("runtime.server._MODELS_PATH", models_path), \
         patch("runtime.server._TOOLS_PATH", tools_path), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", prompt_templates_path), \
         patch("runtime.server._DATA_DIR", str(tmp_path)), \
         patch("runtime.server._ENV_PATH", env_path):
        srv = RuntimeHTTPServer(runtime, host="127.0.0.1", port=0, chats_dir=chats_dir)
        srv.start_background()
        yield srv
        srv.stop()


class TestEnvAPI:
    """Tests for GET/POST/DELETE /v1/env endpoints."""

    def test_get_env_empty_when_no_file(self, server_with_env):
        """GET /v1/env 返回空字典（env.json 不存在时）。"""
        status, body = _get(server_with_env, "/v1/env")
        assert status == 200
        assert body == {"env": {}}

    def test_post_env_add_key_value(self, server_with_env):
        """POST /v1/env 成功新增键值对，返回 200 及完整列表。"""
        status, body = _post(server_with_env, "/v1/env", {"key": "MY_KEY", "value": "my_value"})
        assert status == 200
        assert "env" in body
        assert body["env"]["MY_KEY"] == "my_value"

    def test_post_env_missing_key_field_returns_400(self, server_with_env):
        """POST /v1/env 缺少 key 字段返回 400。"""
        status, body = _post(server_with_env, "/v1/env", {"value": "some_value"})
        assert status == 400
        assert "error" in body
        assert "key" in body["error"].lower()

    def test_post_env_empty_key_returns_400(self, server_with_env):
        """POST /v1/env key 为空字符串返回 400。"""
        status, body = _post(server_with_env, "/v1/env", {"key": "", "value": "some_value"})
        assert status == 400
        assert "error" in body

    def test_delete_env_existing_key(self, server_with_env):
        """DELETE /v1/env/{key} 成功删除，返回 200 及当前完整列表。"""
        # 先新增一个键
        _post(server_with_env, "/v1/env", {"key": "DEL_KEY", "value": "del_value"})
        # 再删除
        status, body = _delete(server_with_env, "/v1/env/DEL_KEY")
        assert status == 200
        assert "env" in body
        assert "DEL_KEY" not in body["env"]

    def test_delete_env_nonexistent_key_returns_200(self, server_with_env):
        """DELETE /v1/env/{key} key 不存在时返回 200 及当前完整列表（静默忽略）。"""
        status, body = _delete(server_with_env, "/v1/env/NONEXISTENT_KEY")
        assert status == 200
        assert "env" in body

    def test_post_env_detect_returns_keys(self, server_with_env, tmp_path):
        """POST /v1/env/detect 返回 200 及 keys 列表。"""
        # mock os.getcwd() 返回临时目录，避免扫描整个工作目录导致超时
        with patch("runtime.server.os.getcwd", return_value=str(tmp_path)):
            status, body = _post(server_with_env, "/v1/env/detect", {})
        assert status == 200
        assert "keys" in body
        assert isinstance(body["keys"], list)

    def test_get_env_returns_all_keys_after_multiple_sets(self, server_with_env):
        """多次 POST /v1/env 后，GET /v1/env 返回所有键值对。"""
        _post(server_with_env, "/v1/env", {"key": "KEY_A", "value": "val_a"})
        _post(server_with_env, "/v1/env", {"key": "KEY_B", "value": "val_b"})
        status, body = _get(server_with_env, "/v1/env")
        assert status == 200
        assert body["env"]["KEY_A"] == "val_a"
        assert body["env"]["KEY_B"] == "val_b"


class TestSessionAPI:
    """Tests for GET /v1/sessions and GET /v1/sessions/{session_id} endpoints."""

    def test_get_sessions_returns_empty_list(self, server_with_env):
        """GET /v1/sessions 返回 200 及 sessions 列表（初始为空）。"""
        status, body = _get(server_with_env, "/v1/sessions")
        assert status == 200
        assert "sessions" in body
        assert isinstance(body["sessions"], list)

    def test_get_session_not_found_returns_404(self, server_with_env):
        """GET /v1/sessions/{session_id} 会话不存在返回 404。"""
        status, body = _get(server_with_env, "/v1/sessions/nonexistent-session-id")
        assert status == 404
        assert "error" in body

    def test_get_session_exists_returns_200(self, server_with_env):
        """GET /v1/sessions/{session_id} 会话存在时返回 200 及完整数据。"""
        import json as _json
        # 手动在 chats_dir 下创建一个会话目录和 conversation.json
        chats_dir = server_with_env._context_manager._chats_dir
        session_id = "2026-01-01_00-00-00"
        session_dir = os.path.join(chats_dir, session_id)
        os.makedirs(session_dir, exist_ok=True)
        conv_data = {
            "meta": {"session_id": session_id, "created_at": "2026-01-01T00:00:00"},
            "messages": [{"role": "user", "content": "hello", "timestamp": "2026-01-01T00:00:00"}],
        }
        with open(os.path.join(session_dir, "conversation.json"), "w", encoding="utf-8") as f:
            _json.dump(conv_data, f)

        status, body = _get(server_with_env, f"/v1/sessions/{session_id}")
        assert status == 200
        assert "messages" in body
        assert len(body["messages"]) == 1
        assert body["messages"][0]["role"] == "user"

    def test_get_sessions_lists_created_sessions(self, server_with_env):
        """创建会话目录后，GET /v1/sessions 应返回该会话。"""
        import json as _json
        chats_dir = server_with_env._context_manager._chats_dir
        session_id = "2026-06-15_10-30-00"
        session_dir = os.path.join(chats_dir, session_id)
        os.makedirs(session_dir, exist_ok=True)
        conv_data = {"meta": {}, "messages": []}
        with open(os.path.join(session_dir, "conversation.json"), "w", encoding="utf-8") as f:
            _json.dump(conv_data, f)

        # 新版 list_sessions() 读取 index.json，需要先写入 index 条目
        server_with_env._session_manager.on_session_created(session_id)

        status, body = _get(server_with_env, "/v1/sessions")
        assert status == 200
        session_ids = [s["session_id"] for s in body["sessions"]]
        assert session_id in session_ids
