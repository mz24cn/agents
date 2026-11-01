"""Non-streaming ``POST /v1/infer`` must keep the session visible while it runs.

A diagnostic client that posts one prompt to the non-streaming endpoint and
then lets the agent run for hundreds of tool rounds used to find its own
session invisible until the answer came back: that endpoint wrote
``conversation.json`` only once, after the inference was over, so during the
whole run the web frontend saw

    Session not found: 261101_200350

even though the session directory (and its index entry) already existed —
``GET /v1/sessions/{id}`` serves ``conversation.json`` from disk and used to
*prune* the index entry on that 404, so the session also vanished from
"""

import contextlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch

from runtime.models import Message, ModelConfig, ToolConfig
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _running_server(tmp_path):
    """Yield ``(server, chats_dir)`` on an ephemeral port with redirected state.

    ``server._runtime.infer_stream`` is patched by the caller; the stub model
    only has to exist to pass request validation.
    """
    chats_dir = tmp_path / "chats"
    with contextlib.ExitStack() as stack:
        for target, value in (
            ("runtime.server._MODELS_PATH", "models.json"),
            ("runtime.server._TOOLS_PATH", "tools.json"),
            ("runtime.server._PROMPT_TEMPLATES_PATH", "prompt_templates.json"),
            ("runtime.server._DATA_DIR", "."),
        ):
            stack.enter_context(patch(target, str(tmp_path / value)))

        model_reg = ModelRegistry()
        model_reg.register(ModelConfig(
            model_id="stub-model",
            api_base="http://localhost:1",
            model_name="stub",
            api_protocol="openai",
        ))
        tool_reg = ToolRegistry()
        tool_reg.register(
            ToolConfig(tool_id="echo", tool_type="function", name="echo",
                       description="echo tool", parameters={}),
            callable_fn=lambda **kw: "echo-out",
        )
        srv = RuntimeHTTPServer(Runtime(model_reg, tool_reg),
                                chats_dir=str(chats_dir))
        srv.start_background(host="127.0.0.1", port=0)
        try:
            yield srv, str(chats_dir)
        finally:
            srv.stop()


def _get(base: str, path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(base + path, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _post(base: str, path: str, payload: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _session_ids(base: str) -> list[str]:
    """Session ids currently listed by ``GET /v1/sessions`` (paginated object)."""
    status, body = _get(base, "/v1/sessions")
    if status != 200:
        return []
    try:
        data = json.loads(body)
    except Exception:
        return []
    entries = data.get("sessions", data) if isinstance(data, dict) else data
    return [e.get("session_id") or e.get("id") for e in entries if isinstance(e, dict)]


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    """Poll ``predicate`` until it is true (index writes are asynchronous)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# 2. GET must not prune the index while the session directory survives
# ---------------------------------------------------------------------------

def test_get_session_keeps_index_entry_when_dir_survives(tmp_path):
    with _running_server(tmp_path) as (srv, chats_dir):
        base = f"http://127.0.0.1:{srv.port}"
        session_id = "261101_200350"
        srv._session_manager.on_session_created(session_id, "推理中的会话")
        # 建档：会话目录已创建，conversation.json 要等第一个落盘点才出现。
        os.makedirs(os.path.join(chats_dir, session_id), exist_ok=True)
        assert _wait_until(lambda: session_id in _session_ids(base)), "index entry not created"

        status, body = _get(base, f"/v1/sessions/{session_id}")
        assert status == 404 and "Session not found" in body

        # 目录还在 —— 索引条目必须保留，否则推理期间侧边栏会凭空少一条会话。
        time.sleep(0.3)  # let any (wrong) asynchronous index rewrite land
        assert session_id in _session_ids(base), \
            "index entry was pruned while the session directory still exists"

        # 目录真没了（手工删目录 / 清理残留）才回收索引条目。
        os.rmdir(os.path.join(chats_dir, session_id))
        assert _get(base, f"/v1/sessions/{session_id}")[0] == 404
        time.sleep(0.3)
        assert session_id not in _session_ids(base), \
            "index entry should be pruned once the session directory is gone"
