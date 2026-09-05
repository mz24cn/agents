"""Tests for the /v1/setup?op=hello inference status breakdown.

The hello response reports whether inference is active and distinguishes
web-session inference (request carries a session_id) from stateless API
inference (no session_id), which is invisible to the web UI. The auth/setup
page uses this breakdown to explain why an update is currently blocked.
"""

import json
import threading
import time
import urllib.request
from unittest.mock import patch

import pytest

from runtime.models import Message
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def runtime():
    return Runtime(ModelRegistry(), ToolRegistry())


@pytest.fixture()
def server(runtime, tmp_path):
    models_path = str(tmp_path / "models.json")
    tools_path = str(tmp_path / "tools.json")
    prompt_templates_path = str(tmp_path / "prompt_templates.json")
    with patch("runtime.server._MODELS_PATH", models_path), \
         patch("runtime.server._TOOLS_PATH", tools_path), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", prompt_templates_path), \
         patch("runtime.server._DATA_DIR", str(tmp_path)):
        srv = RuntimeHTTPServer(runtime)
        srv.start_background(host="127.0.0.1", port=0)
        yield srv
        srv.stop()


def _hello(server: RuntimeHTTPServer) -> dict:
    url = f"http://127.0.0.1:{server.port}/v1/setup?op=hello"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def _post(server: RuntimeHTTPServer, path: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _blocking_stream_factory(release: threading.Event):
    """Build a fake infer_stream that blocks until *release* is set."""

    def fake_infer_stream(request, cancel_event=None):
        release.wait(timeout=15)
        yield Message(role="assistant", content="done")

    return fake_infer_stream


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHelloInferenceStatus:
    def test_idle_reports_all_false(self, server):
        body = _hello(server)
        assert body["inference_active"] is False
        assert body["api_inference_active"] is False
        assert body["session_inference_active"] is False

    def test_stateless_stream_counts_as_api_inference(self, runtime, server):
        """A /v1/infer/stream request without session_id is API inference."""
        release = threading.Event()
        with patch.object(
            runtime, "infer_stream",
            side_effect=_blocking_stream_factory(release),
        ):
            errors = []

            def run():
                try:
                    _post(server, "/v1/infer/stream", {
                        "model_id": "m",
                        "messages": [{"role": "user", "content": "hi"}],
                    })
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            try:
                assert _wait_until(
                    lambda: _hello(server)["api_inference_active"] is True
                ), "stateless stream was not reported as API inference"
                body = _hello(server)
                assert body["inference_active"] is True
                assert body["api_inference_active"] is True
                assert body["session_inference_active"] is False
            finally:
                release.set()
                worker.join(timeout=20)
            assert not errors
            assert _wait_until(
                lambda: _hello(server)["api_inference_active"] is False
            ), "API inference counter did not reset after the stream ended"

    def test_session_stream_counts_as_session_inference(self, runtime, server):
        """A /v1/infer/stream request with session_id is web-session inference."""
        release = threading.Event()
        with patch.object(
            runtime, "infer_stream",
            side_effect=_blocking_stream_factory(release),
        ):
            errors = []

            def run():
                try:
                    _post(server, "/v1/infer/stream", {
                        "model_id": "m",
                        "session_id": "new",
                        "messages": [{"role": "user", "content": "hi"}],
                    })
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            try:
                assert _wait_until(
                    lambda: _hello(server)["session_inference_active"] is True
                ), "session stream was not reported as session inference"
                body = _hello(server)
                assert body["inference_active"] is True
                assert body["api_inference_active"] is False
                assert body["session_inference_active"] is True
            finally:
                release.set()
                worker.join(timeout=20)
            assert not errors
            assert _wait_until(
                lambda: _hello(server)["session_inference_active"] is False
            ), "session stream was not reported as finished"

    def test_stateless_non_stream_infer_counts_as_api(self, runtime, server):
        """POST /v1/infer without session_id is also stateless API inference."""
        release = threading.Event()
        with patch.object(
            runtime, "infer_stream",
            side_effect=_blocking_stream_factory(release),
        ):
            errors = []

            def run():
                try:
                    _post(server, "/v1/infer", {
                        "model_id": "m",
                        "messages": [{"role": "user", "content": "hi"}],
                    })
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            try:
                assert _wait_until(
                    lambda: _hello(server)["api_inference_active"] is True
                ), "stateless non-streaming infer was not reported as API inference"
                body = _hello(server)
                assert body["inference_active"] is True
                assert body["api_inference_active"] is True
                assert body["session_inference_active"] is False
            finally:
                release.set()
                worker.join(timeout=20)
            assert not errors
            assert _wait_until(
                lambda: _hello(server)["api_inference_active"] is False
            ), "API inference counter did not reset after the request ended"

    def test_failed_prepare_does_not_count(self, server):
        """A rejected request (missing model_id) must not leave the counter up."""
        try:
            _post(server, "/v1/infer/stream", {"messages": []})
        except Exception:
            pass
        body = _hello(server)
        assert body["api_inference_active"] is False
        assert body["session_inference_active"] is False
