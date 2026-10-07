"""Tests for ``runtime/model_probe.py`` and ``GET /v1/models/probe``.

The probe asks an upstream endpoint for its own model list so the Setup form
can read ``max_context`` (and modalities / tool support) off the server instead
of guessing.  Every shape below was captured from a real endpoint in this
deployment — that is the whole point of the module, so the fixtures are literal
copies of what those servers answer:

- local GGUF server: ``data[].meta.n_ctx``, plus ``/props`` for modalities,
  ``chat_template_caps.supports_tools``, quantization, slots
- native runner:     ``/v1/models`` has only ids; ``/api/tags`` carries
  ``details.context_length`` + ``capabilities``
- hosted chat API:   ``context_window`` / ``max_output_tokens`` /
  ``input_modalities`` / ``effort.supported_levels``
- hosted mixed API:  ``context_length`` / ``max_output_length`` /
  ``supported_features`` / ``quantization``
- gateway with a classifier: only ``type`` (``text_model`` / ``visual_model`` /
  ``speech_model`` / ``image_generation``) — and errors arrive as HTTP 200
  ``{"code":401,"msg":"token 无效"}``
- inference server:  ``data[].max_model_len``
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest

from runtime.model_probe import (
    ModelProbeError,
    extract_max_context,
    models_url_candidates,
    parse_models_payload,
    probe_models,
)
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


class _MockModelServer:
    """Minimal stand-in for an upstream inference server (any shape)."""

    def __init__(self):
        self.routes: dict[str, tuple[int, object]] = {}
        self.requests: list[tuple[str, dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep stderr quiet
                pass

            def do_GET(self) -> None:
                outer.requests.append((self.path, dict(self.headers)))
                route = self.path.split("?", 1)[0]
                if route not in outer.routes:
                    self.send_error(404)
                    return
                status, payload = outer.routes[route]
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_MockModelServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    @property
    def base(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"


@pytest.fixture()
def mock_models() -> _MockModelServer:
    with _MockModelServer() as srv:
        yield srv


@pytest.fixture()
def runtime():
    return Runtime(ModelRegistry(), ToolRegistry())


@pytest.fixture()
def server(runtime, tmp_path):
    """The service under test, on a random port with temp data files."""
    with patch("runtime.server._MODELS_PATH", str(tmp_path / "models.json")), \
         patch("runtime.server._TOOLS_PATH", str(tmp_path / "tools.json")), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", str(tmp_path / "prompt_templates.json")), \
         patch("runtime.server._DATA_DIR", str(tmp_path)):
        srv = RuntimeHTTPServer(runtime)
        srv.start_background(host="127.0.0.1", port=0)
        yield srv
        srv.stop()


def _get(server: RuntimeHTTPServer, path: str, headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# A self-hosted GGUF inference instance (meta.* on every entry)
_LLAMA_CPP = {
    "object": "list",
    "data": [
        {
            "id": "qwen3.8-flash-next-iq3_s",
            "object": "model",
            "status": {"value": "loaded"},
            "meta": {"n_ctx": 131072},
            "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
        }
    ],
}

_VLLM = {
    "object": "list",
    "data": [
        {"id": "qwen-large", "object": "model", "owned_by": "vllm", "max_model_len": 32768},
        {"id": "qwen-small", "object": "model", "owned_by": "vllm", "max_model_len": 8192},
    ],
}

# A native runner: the OpenAI-compatible list says nothing at all…
_OLLAMA_LIST = {
    "object": "list",
    "data": [{"id": "qwen3.8:27b", "object": "model", "created": 1, "owned_by": ""}],
}
# …while its native /api/tags has the window, the modalities and the size.
_OLLAMA_TAGS = {
    "models": [
        {
            "name": "qwen3.8:27b",
            "model": "qwen3.8:27b",
            "size": 17741872154,
            "details": {
                "parameter_size": "27.3B",
                "quantization_level": "Q4_K_M",
                "context_length": 262144,
            },
            "capabilities": ["completion", "vision", "tools", "thinking"],
        }
    ]
}

# A list without meta in it: /props is the only place to look.
_BARE_LIST = {"object": "list", "data": [{"id": "Qwen3.8-27B.gguf", "object": "model", "owned_by": "local"}]}
_LLAMA_PROPS = {
    "default_generation_settings": {"n_ctx": 131072, "params": {}},
    "modalities": {"vision": True, "audio": False},
    "chat_template_caps": {"supports_tools": True, "supports_tool_calls": True},
    "model_alias": "Qwen3.8-27B.gguf",
    "model_ftype": "Q4_K_M",
    "build_info": "b9999",
    "total_slots": 2,
    "is_sleeping": False,
}


# ------------------------------------------------------------------
# URL candidates
# ------------------------------------------------------------------


def test_api_base_already_ending_with_v1_is_used_as_is() -> None:
    assert models_url_candidates("http://127.0.0.1:18080/v1") == [
        "http://127.0.0.1:18080/v1/models"]


def test_api_base_without_v1_tries_v1_first_then_bare() -> None:
    assert models_url_candidates("http://localhost:11434/") == [
        "http://localhost:11434/v1/models", "http://localhost:11434/models"]


def test_blank_api_base_yields_no_candidates() -> None:
    assert models_url_candidates("   ") == []


# ------------------------------------------------------------------
# context window extraction
# ------------------------------------------------------------------


@pytest.mark.parametrize("entry,expected", [
    ({"meta": {"n_ctx": 131072}}, (131072, "meta.n_ctx")),
    ({"max_model_len": 32768}, (32768, "max_model_len")),
    ({"context_length": 131072}, (131072, "context_length")),
    ({"context_window": 1048576}, (1048576, "context_window")),  # hosted chat API
    ({"max_context": "128K"}, (131072, "max_context")),
    ({"n_ctx": 4096}, (4096, "n_ctx")),
    ({"details": {"context_length": 262144}}, (262144, "details.context_length")),
    ({"max_input_tokens": 4096}, (4096, "max_input_tokens")),
    # Trained window is a last resort, and says so — the runtime window may be
    # smaller (a --ctx-size style flag shrinks the runtime window).
    ({"meta": {"n_ctx": 65536, "n_ctx_train": 262144}}, (65536, "meta.n_ctx")),
    ({"meta": {"n_ctx_train": 262144}}, (262144, "meta.n_ctx_train(train)")),
    # Nothing published: keep whatever the user configured (0 = unknown).
    ({"id": "gpt-4o-mini"}, (0, "")),
    ({"meta": {"n_ctx": 0}}, (0, "")),
    ({"meta": {"n_ctx": -1}}, (0, "")),
    ({"max_context": "not-a-number"}, (0, "")),
])
def test_extract_max_context_covers_the_common_vendor_shapes(entry, expected) -> None:
    assert extract_max_context(entry) == expected


# ------------------------------------------------------------------
# per-row normalization: the fields the table shows
# ------------------------------------------------------------------


def test_parse_models_payload_normalizes_a_local_gguf_entry() -> None:
    rows = parse_models_payload(_LLAMA_CPP)
    assert rows == [{
        "model_name": "qwen3.8-flash-next-iq3_s",
        "max_context": 131072,
        "max_context_source": "meta.n_ctx",
        "max_output": 0,
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "features": [],
        "effort_levels": [],
        "details": "",
        "status": "loaded",
        "owned_by": "",
    }]


def test_parse_models_payload_reads_output_limit_and_effort() -> None:
    rows = parse_models_payload({"data": [{
        "id": "flash-chat",
        "context_window": 1048576,
        "max_output_tokens": 393216,
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"},
        "api_capabilities": {"anthropic_messages": {"system_prompt_update": "in-history"}},
    }]})
    row = rows[0]
    assert row["max_context"] == 1048576
    assert row["max_output"] == 393216
    assert row["input_modalities"] == ["text", "image"]
    assert row["effort_levels"] == ["low", "high", "max"]
    # api_capabilities keys are implementation details, not model features.
    assert row["features"] == []


def test_parse_models_payload_reads_features_and_quantization() -> None:
    rows = parse_models_payload({"data": [{
        "id": "glm-5.2",
        "context_length": 1048576,
        "max_output_length": 131072,
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "quantization": "fp8",
        "supported_features": ["tools", "json_mode", "reasoning"],
    }]})
    row = rows[0]
    assert (row["max_context"], row["max_output"]) == (1048576, 131072)
    assert row["features"] == ["tools", "json_mode", "reasoning"]
    assert row["details"] == "fp8"


@pytest.mark.parametrize("model_type,inputs,outputs", [
    ("text_model", ["text"], ["text"]),
    ("visual_model", ["text"], ["video"]),        # text-to-video bucket
    ("image_generation", ["text"], ["image"]),
    ("speech_model", ["text"], ["audio"]),        # text-to-speech bucket
    ("vision", ["text", "image"], ["text"]),
    ("something-else", [], []),
])
def test_type_field_is_the_only_modality_hint_for_some_gateways(model_type, inputs, outputs) -> None:
    rows = parse_models_payload({"data": [{"id": "m", "type": model_type}]})
    assert (rows[0]["input_modalities"], rows[0]["output_modalities"]) == (inputs, outputs)


def test_capabilities_become_modalities_but_not_duplicate_features() -> None:
    rows = parse_models_payload({"data": [
        {"id": "m", "capabilities": ["completion", "vision", "tools", "thinking"]},
    ]})
    row = rows[0]
    assert row["input_modalities"] == ["text", "image"]
    assert row["output_modalities"] == ["text"]
    # vision/completion already show up as modalities, so they are not features.
    assert row["features"] == ["tools", "thinking"]


def test_details_line_carries_size_and_quantization_for_local_models() -> None:
    rows = parse_models_payload({"data": [
        {"id": "m.gguf", "meta": {"n_ctx": 8192, "size": 16453443584}},
    ]})
    assert rows[0]["details"] == "15.3GB"


def test_parse_models_payload_accepts_native_style_and_bare_lists() -> None:
    assert [r["model_name"] for r in parse_models_payload({"models": [{"id": "a"}, {"name": "b"}]})] == ["a", "b"]
    assert [r["model_name"] for r in parse_models_payload([{"model": "c"}])] == ["c"]


def test_parse_models_payload_skips_entries_without_a_name() -> None:
    rows = parse_models_payload({"data": [{"id": ""}, {"id": "keep"}, "junk", None]})
    assert [row["model_name"] for row in rows] == ["keep"]


def test_parse_models_payload_rejects_a_non_list_data_field() -> None:
    with pytest.raises(ModelProbeError) as exc:
        parse_models_payload({"data": "junk"})
    assert exc.value.code == "invalid_response"


# ------------------------------------------------------------------
# probe_models against a live (mock) endpoint
# ------------------------------------------------------------------


def test_probe_models_reads_the_window_the_instance_publishes(mock_models) -> None:
    mock_models.routes["/v1/models"] = (200, _LLAMA_CPP)
    result = probe_models(f"{mock_models.base}/v1")
    assert result["models_url"] == f"{mock_models.base}/v1/models"
    assert result["count"] == 1
    assert result["models"][0]["max_context"] == 131072
    assert result["models"][0]["max_context_source"] == "meta.n_ctx"


def test_probe_models_lists_every_model_of_a_vllm_endpoint(mock_models) -> None:
    mock_models.routes["/v1/models"] = (200, _VLLM)
    result = probe_models(f"{mock_models.base}/v1")
    assert [row["max_context"] for row in result["models"]] == [32768, 8192]


def test_probe_models_sends_the_upstream_key_as_bearer(mock_models) -> None:
    mock_models.routes["/v1/models"] = (200, {"data": []})
    probe_models(f"{mock_models.base}/v1", api_key="sk-secret")
    _path, headers = mock_models.requests[0]
    assert headers.get("Authorization") == "Bearer sk-secret"


def test_probe_models_sets_a_user_agent_gateways_accept(mock_models) -> None:
    """实测：有网关对默认的 ``Python-urllib/3.x`` 直接回 403。"""
    mock_models.routes["/v1/models"] = (200, {"data": []})
    probe_models(f"{mock_models.base}/v1")
    _path, headers = mock_models.requests[0]
    assert headers.get("User-Agent", "").startswith("Mozilla/5.0")


def test_probe_models_without_v1_in_api_base_still_finds_the_list(mock_models) -> None:
    mock_models.routes["/v1/models"] = (200, _LLAMA_CPP)
    result = probe_models(mock_models.base)
    assert result["models_url"] == f"{mock_models.base}/v1/models"


def test_probe_models_reports_a_rejected_key_as_auth_failed(mock_models) -> None:
    mock_models.routes["/v1/models"] = (401, {"error": "unauthorized"})
    with pytest.raises(ModelProbeError) as exc:
        probe_models(f"{mock_models.base}/v1", api_key="nope")
    assert exc.value.code == "auth_failed"


def test_probe_models_reports_http_200_error_bodies_as_auth_failed(mock_models) -> None:
    """有些网关用 HTTP 200 + ``{"code":401,"msg":"token 无效"}`` 表达拒绝。"""
    mock_models.routes["/v1/models"] = (200, {"code": 401, "msg": "token 无效", "data": None})
    with pytest.raises(ModelProbeError) as exc:
        probe_models(f"{mock_models.base}/v1", api_key="nope")
    assert exc.value.code == "auth_failed"
    assert "token 无效" in str(exc.value)


def test_probe_models_reports_an_unreachable_endpoint() -> None:
    # Port 1 is reserved: connect fails fast on every platform.
    with pytest.raises(ModelProbeError) as exc:
        probe_models("http://127.0.0.1:1/v1", timeout=3)
    assert exc.value.code == "unreachable"


def test_probe_models_reports_html_answers_as_invalid(mock_models) -> None:
    mock_models.routes["/v1/models"] = (200, b"<html>proxy login</html>")
    with pytest.raises(ModelProbeError) as exc:
        probe_models(f"{mock_models.base}/v1")
    # Reached it, but the answer is not a model list — distinct from "unreachable".
    assert exc.value.code == "invalid_response"


def test_probe_models_reports_a_payload_without_a_list_field(mock_models) -> None:
    mock_models.routes["/v1/models"] = (200, {"success": True, "count": 0})
    with pytest.raises(ModelProbeError) as exc:
        probe_models(f"{mock_models.base}/v1")
    assert exc.value.code == "invalid_response"


def test_probe_models_accepts_an_empty_list_as_a_real_answer(mock_models) -> None:
    """A server that is up but has nothing loaded is not an error."""
    mock_models.routes["/v1/models"] = (200, {"object": "list", "data": []})
    assert probe_models(f"{mock_models.base}/v1")["count"] == 0


# ------------------------------------------------------------------
# native-endpoint fallbacks (where the real metadata lives)
# ------------------------------------------------------------------


def test_probe_models_fills_window_and_modalities_from_api_tags(mock_models) -> None:
    mock_models.routes["/v1/models"] = (200, _OLLAMA_LIST)
    mock_models.routes["/api/tags"] = (200, _OLLAMA_TAGS)
    row = probe_models(f"{mock_models.base}/v1")["models"][0]
    assert row["max_context"] == 262144
    assert row["max_context_source"] == "api.tags.details.context_length"
    assert row["input_modalities"] == ["text", "image"]
    assert row["features"] == ["tools", "thinking"]
    assert row["details"] == "27.3B · Q4_K_M · 16.5GB"


def test_probe_models_fills_modalities_tools_and_slots_from_props(mock_models) -> None:
    mock_models.routes["/v1/models"] = (200, _BARE_LIST)
    mock_models.routes["/props"] = (200, _LLAMA_PROPS)
    row = probe_models(f"{mock_models.base}/v1")["models"][0]
    assert row["max_context"] == 131072
    assert row["max_context_source"] == "props.default_generation_settings.n_ctx"
    assert row["input_modalities"] == ["text", "image"]      # modalities.vision
    assert "tools" in row["features"]                        # chat_template_caps
    assert row["details"] == "Q4_K_M · b9999 · 2 slot"


def test_probe_models_ignores_props_when_the_alias_matches_no_row(mock_models) -> None:
    """``/props`` 是实例级的：认不出是哪个模型就不能往多行列表上套。"""
    mock_models.routes["/v1/models"] = (200, {"data": [
        {"id": "a.gguf", "object": "model"}, {"id": "b.gguf", "object": "model"},
    ]})
    mock_models.routes["/props"] = (200, {**_LLAMA_PROPS, "model_alias": "other.gguf"})
    rows = probe_models(f"{mock_models.base}/v1")["models"]
    assert [row["max_context"] for row in rows] == [0, 0]


def test_probe_models_falls_back_to_health_for_the_window(mock_models) -> None:
    mock_models.routes["/v1/models"] = (200, _BARE_LIST)
    mock_models.routes["/health"] = (200, {"status": "ok", "service": "local-gguf", "max_context": 131072})
    row = probe_models(f"{mock_models.base}/v1")["models"][0]
    assert row["max_context"] == 131072
    assert row["max_context_source"] == "health.max_context"


def test_probe_models_native_fallbacks_are_optional(mock_models) -> None:
    """Cloud gateways 404 on /api/tags and /props — that must not fail the probe."""
    mock_models.routes["/v1/models"] = (200, _VLLM)
    result = probe_models(f"{mock_models.base}/v1")
    assert [row["max_context"] for row in result["models"]] == [32768, 8192]
    assert {path for path, _ in mock_models.requests} == {"/v1/models", "/api/tags", "/props"}


# ------------------------------------------------------------------
# GET /v1/models/probe
# ------------------------------------------------------------------


def test_probe_endpoint_returns_the_normalized_list(server, mock_models) -> None:
    mock_models.routes["/v1/models"] = (200, _LLAMA_CPP)
    status, body = _get(server, f"/v1/models/probe?api_base={mock_models.base}/v1")
    assert status == 200
    assert body["count"] == 1
    row = body["models"][0]
    assert row["model_name"] == "qwen3.8-flash-next-iq3_s"
    assert row["max_context"] == 131072
    assert row["input_modalities"] == ["text", "image"]
    assert row["output_modalities"] == ["text"]
    assert row["status"] == "loaded"


def test_probe_endpoint_never_echoes_the_upstream_key(server, mock_models) -> None:
    mock_models.routes["/v1/models"] = (200, _LLAMA_CPP)
    status, body = _get(
        server, f"/v1/models/probe?api_base={mock_models.base}/v1",
        headers={"X-Probe-Api-Key": "sk-secret"})
    assert status == 200
    assert "sk-secret" not in json.dumps(body)
    assert mock_models.requests[0][1].get("Authorization") == "Bearer sk-secret"


def test_probe_endpoint_resolves_env_placeholders(server, mock_models, monkeypatch) -> None:
    monkeypatch.setenv("PROBE_TEST_BASE", mock_models.base)
    mock_models.routes["/v1/models"] = (200, _LLAMA_CPP)
    status, body = _get(server, "/v1/models/probe?api_base={{PROBE_TEST_BASE}}/v1")
    assert status == 200
    assert body["models"][0]["max_context"] == 131072


def test_probe_endpoint_requires_api_base(server) -> None:
    status, body = _get(server, "/v1/models/probe")
    assert status == 400
    assert body["error"] == "missing_api_base"


def test_probe_endpoint_reports_unreachable_as_502(server) -> None:
    status, body = _get(server, "/v1/models/probe?api_base=http://127.0.0.1:1/v1")
    assert status == 502
    assert body["error"] == "unreachable"


def test_probe_endpoint_reports_a_rejected_key_as_400(server, mock_models) -> None:
    mock_models.routes["/v1/models"] = (401, {"error": "unauthorized"})
    status, body = _get(
        server, f"/v1/models/probe?api_base={mock_models.base}/v1",
        headers={"X-Probe-Api-Key": "nope"})
    assert status == 400
    assert body["error"] == "auth_failed"


def test_probe_endpoint_reports_upstream_error_message(server, mock_models) -> None:
    mock_models.routes["/v1/models"] = (200, {"success": False, "message": "额度用尽"})
    status, body = _get(server, f"/v1/models/probe?api_base={mock_models.base}/v1")
    assert status == 502
    assert body["error"] == "upstream_error"
    assert "额度用尽" in body["message"]


def test_probe_endpoint_does_not_touch_the_local_registry(server, mock_models) -> None:
    """Probing is read-only: the local model registry stays untouched."""
    mock_models.routes["/v1/models"] = (200, _LLAMA_CPP)
    status, _body = _get(server, f"/v1/models/probe?api_base={mock_models.base}/v1")
    assert status == 200
    assert _get(server, "/v1/models")[1]["models"] == []
