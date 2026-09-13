"""Phase B: remote execution over the tunnel.

The chat execution path (tool list, tool call, session-context journals,
revoke, and setup push updates) runs through the child's reverse tunnel when
the bound environment is a ``tunnel:`` env, while direct environments keep
the existing HTTP transport.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import urllib.request
from unittest.mock import patch

import pytest

from runtime.remote_tool_proxy import RemoteToolProxy, RemoteToolCallError
from runtime.server import RuntimeHTTPServer
from tests.test_tunnel import (
    _real_server,
    _request,
    _set_child_source,
    _wait_until,
    _parent_tunnel_envs,
)

TURN_TS = "2026-01-02T10:00:00"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def pair(tmp_path):
    """Parent + child real servers with the child registered (tunnel online)."""
    with _real_server(tmp_path, "parent_data") as parent, \
         _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as child:
        parent_srv, parent_data = parent
        child_srv, child_data = child
        _set_child_source(child_srv, child_data, f"http://127.0.0.1:{parent_srv.port}/")
        status, body = _request(child_srv, "POST", "/v1/tunnel/parent/register")
        assert status == 200, body
        env_id = body["env_id"]

        def _online():
            envs = _parent_tunnel_envs(parent_srv)
            env = next((e for e in envs if e["id"] == env_id), None)
            return env if env and env.get("online") is True else None
        _wait_until(_online, message="tunnel env online")
        yield {
            "parent": parent_srv, "parent_data": parent_data,
            "child": child_srv, "child_data": child_data,
            "child_ws": tmp_path / "child_ws",
            "env_id": env_id,
        }


def _tunnel_proxy(pair) -> RemoteToolProxy:
    env = pair["parent"]._remote_env_manager.get(pair["env_id"])
    return RemoteToolProxy(env, tunnel_manager=pair["parent"]._tunnel_manager)


# ---------------------------------------------------------------------------
# Tool proxy over the tunnel
# ---------------------------------------------------------------------------

def test_proxy_configs_and_call_over_tunnel(pair):
    proxy = _tunnel_proxy(pair)
    assert proxy.is_tunnel is True
    configs = proxy.list_tool_configs()
    ids = {c.tool_id for c in configs}
    # child's original ids (remote tools never enter the parent registry)
    assert "write_file" in ids
    assert "exec_cli" in ids
    assert "delegate" in ids
    assert "talk_to" in ids
    cfg = next(c for c in configs if c.tool_id == "write_file")
    assert cfg.builtin is True and cfg.name == "write_file"
    assert cfg.callable_fn is not None

    from runtime.common import set_request_context, clear_request_context
    set_request_context(session_id="sess-tunnel-1", user_message_timestamp=TURN_TS)
    try:
        result = proxy.call(
            "write_file", {"path": "tunnel_proxy.txt", "content": "via-tunnel"}
        )
    finally:
        clear_request_context(["session_id", "user_message_timestamp"])
    assert not result.startswith("Error:"), result
    assert (pair["child_ws"] / "tunnel_proxy.txt").read_text(encoding="utf-8") == "via-tunnel"


def test_proxy_http_json_and_skill_over_tunnel(pair):
    proxy = _tunnel_proxy(pair)
    status, data = proxy.http_json("/v1/tools", method="GET")
    assert status == 200
    assert isinstance(data.get("tools"), list)

    # unknown tool -> child returns an error the proxy surfaces as text
    result = proxy.call("no_such_tool", {})
    assert result.startswith("Error:")


def test_proxy_offline_raises_clear_error(pair):
    proxy = _tunnel_proxy(pair)
    child_client = pair["child"]._tunnel_client
    child_client.stop()
    try:
        with pytest.raises(RemoteToolCallError) as exc_info:
            proxy.list_tools(force=True)
        assert "offline" in str(exc_info.value)
    finally:
        child_client.start()
        _wait_until(lambda: pair["parent"]._tunnel_manager.is_online(pair["env_id"]),
                    message="tunnel back online", timeout=20)


# ---------------------------------------------------------------------------
# Session-context journals + revoke over the tunnel
# ---------------------------------------------------------------------------

def test_journal_and_revoke_over_tunnel(pair):
    proxy = _tunnel_proxy(pair)
    session_id = "sess-tunnel-journal"
    from runtime.common import set_request_context, clear_request_context
    set_request_context(session_id=session_id, user_message_timestamp=TURN_TS)
    try:
        result = proxy.call(
            "write_file", {"path": "journal_over_tunnel.txt", "content": "v1"}
        )
        assert not result.startswith("Error:"), result
    finally:
        clear_request_context(["session_id", "user_message_timestamp"])
    assert (pair["child_ws"] / "journal_over_tunnel.txt").exists()

    # the child recorded the change under the parent session id
    status, journals = proxy.http_json(f"/v1/sessions/{session_id}/file-journals", "GET")
    assert status == 200
    entries = journals.get("turn_keys") or []
    assert entries, f"no journals on child: {journals}"

    # journal-only revoke restores the file on the child (new file -> removed)
    status, data = proxy.http_json(
        f"/v1/sessions/{session_id}/revoke?journal_only=true",
        method="POST",
        payload={"timestamp": TURN_TS, "forced": False, "keep_files": False},
    )
    assert status == 200, data
    assert not (pair["child_ws"] / "journal_over_tunnel.txt").exists()


def test_remote_bound_session_log_dir_returns_local_path_over_tunnel(pair):
    # 会话绑定远程环境（隧道）时 log-dir 仍返回本地（父端）会话目录：
    # 不转发子端、不创建子端目录（无 file journal 时子端无会话目录）。
    parent_srv, parent_data = pair["parent"], pair["parent_data"]
    child_data = pair["child_data"]
    env_id = pair["env_id"]
    session_id = "sess-tunnel-logdir"
    session_dir = parent_data / "chat_data" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "conversation.json").write_text(
        json.dumps({
            "meta": {"remote_env": env_id, "turn_count": 1},
            "messages": [{"role": "user", "content": "hi", "timestamp": TURN_TS}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    status, body = _request(parent_srv, "GET", f"/v1/sessions/{session_id}/log-dir")
    assert status == 200, body
    assert body["path"] == str(session_dir)
    assert not (child_data / "chat_data" / session_id).exists()


# ---------------------------------------------------------------------------
# Setup push update over the tunnel
# ---------------------------------------------------------------------------

def test_push_update_over_tunnel(pair, tmp_path):
    parent_srv, parent_data = pair["parent"], pair["parent_data"]
    child_data = pair["child_data"]

    # The child has no config files yet (last_config empty); a fresh config
    # on the parent makes the delta non-empty (config only, no restart).
    assert (child_data / "models.json").exists() is False
    (parent_data / "models.json").write_text("[]", encoding="utf-8")

    status, result = _request(
        parent_srv, "POST", f"/v1/remote-envs/{pair['env_id']}/push-update"
    )
    assert status == 200, result
    assert result["ok"] is True
    assert result["updated"] is True
    assert result["method"] == "push"
    assert "agents_runtime/models.json" in result["updated_files"]
    # the child really applied the pushed config through its tunnel
    assert (child_data / "models.json").read_text(encoding="utf-8") == "[]"

    # second push: nothing newer -> up-to-date (hello over the tunnel)
    status, result = _request(
        parent_srv, "POST", f"/v1/remote-envs/{pair['env_id']}/push-update"
    )
    assert status == 200
    assert result["updated"] is False
    assert result["reason"] == "up-to-date"


def test_push_update_tunnel_offline_reports_unreachable(pair):
    pair["child"]._tunnel_client.stop()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and pair["parent"]._tunnel_manager.is_online(pair["env_id"]):
        time.sleep(0.1)
    assert not pair["parent"]._tunnel_manager.is_online(pair["env_id"])
    status, result = _request(
        pair["parent"], "POST", f"/v1/remote-envs/{pair['env_id']}/push-update"
    )
    assert status == 502
    assert result.get("error") == "child_unreachable"
