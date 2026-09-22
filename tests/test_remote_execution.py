"""Tests for remote execution (远程执行).

A chat session bound to a remote environment runs ALL of its tools in the
child environment while inference (parent model + conversation history) stays
in the parent:

* ``POST /v1/infer`` body field ``remote_env`` (env id from remote_envs.json)
  binds the session to the child.  Simplified design: the tool list is taken
  ONLY from the parent registry (the model sees exactly the same names /
  parameters / descriptions as local mode; the web UI never talks to the
  child for tools).  Every selected tool is wrapped with a forwarding
  callable so each call executes in the child under the parent's tool_id
  (a tool missing on the child yields a child error result, handled by the
  model).  Wrapped configs travel as ToolConfigs on the InferenceRequest and
  never register into the parent ToolRegistry (the shared registry objects
  are copied, not mutated); the binding is persisted into conversation.json
  ``meta.remote_env``.  A dead child no longer blocks request preparation
  (no 502) — only individual tool calls fail.
* Tool calls are forwarded to the child's ``POST /v1/tools/call`` carrying
  the portable subset of the parent request context (workspace / session_id
  / user-message timestamp / depth / agent_id / agent_ids / all_agent_ids /
  model_id / available_tool_ids) as one base64url JSON value in the
  ``X-Agents-Request-Context`` header; the JSON payload stays a pure
  tool-call contract (tool_id + arguments).  The child rebuilds the session
  context on its own host, so its file journals / terminals / delegate
  sub-sessions share the parent session id.
* Skill progressive disclosure stays in the parent loop; the SKILL.md body
  is fetched from the child via ``GET /v1/tools/skill/{id}``.
* ``revoke`` first restores the child's files (``revoke?journal_only=true``),
  only deleting local messages on success; deleting a parent session
  fire-and-forget cleans the orphan child session dir.
* Child-side auth: GET /v1/* additionally accepts ``?token=`` (resources and
  WebSocket handshakes cannot set Authorization), Bearer accepts the setup
  token as well as the API key.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from runtime.remote_tool_proxy import RemoteToolProxy
from runtime.server import RuntimeHTTPServer

TURN_TS = "2026-01-02T10:00:00"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _request(server, method, path, payload=None, headers=None):
    url = f"http://127.0.0.1:{server.port}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except (ValueError, json.JSONDecodeError):
            return exc.code, {"raw": exc.read().decode("utf-8", errors="replace")}


def _request_text(server, method, path, payload=None, headers=None):
    """Like _request but returns the raw response text (tool results are
    text/plain on success)."""
    url = f"http://127.0.0.1:{server.port}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _wait_hello(server):
    for _ in range(50):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.port}/v1/setup?op=hello", timeout=1
            ) as resp:
                return json.loads(resp.read())
        except OSError:
            time.sleep(0.1)
    raise AssertionError("server never came up")


def _forwarded_headers(**ctx):
    """Build the ``X-Agents-Request-Context`` header the parent proxy sends.

    Mirrors ``RemoteToolProxy.call`` so child-side tests exercise the real
    transport without spinning up a parent inference request.
    """
    from runtime.common import FORWARDED_CONTEXT_HEADER, encode_forwarded_context
    return {FORWARDED_CONTEXT_HEADER: encode_forwarded_context(ctx)}


def _register_child_probe(child):
    """Register a child function tool that echoes its request context."""
    from runtime.models import ToolConfig
    from runtime.common import get_request_context

    def _probe() -> str:
        scope = get_request_context("tool_scope") or []
        return json.dumps({
            "workspace": get_request_context("workspace"),
            "session_id": get_request_context("session_id"),
            "session_dir": get_request_context("session_dir"),
            "user_message_timestamp": get_request_context("user_message_timestamp"),
            "depth": get_request_context("depth"),
            "agent_id": get_request_context("agent_id"),
            "agent_ids": get_request_context("agent_ids"),
            "all_agent_ids": get_request_context("all_agent_ids"),
            "tool_scope": [getattr(tc, "tool_id", None) for tc in scope],
            "has_agent_manager": get_request_context("agent_manager") is not None,
        })

    child[0]._server.runtime._tool_registry.register(  # type: ignore[attr-defined]
        ToolConfig(
            tool_id="ctx_probe",
            tool_type="function",
            name="ctx_probe",
            description="echo request context",
            parameters={"type": "object", "properties": {}},
        ),
        callable_fn=_probe,
    )
    return _probe


@contextlib.contextmanager
def _real_server(tmp_path, name="data", workspace=None):
    """Start a real RuntimeHTTPServer (builtins registered, isolated paths)."""
    data_dir = tmp_path / name
    data_dir.mkdir()
    if workspace:
        ws = workspace if isinstance(workspace, str) else str(workspace)
        os.makedirs(ws, exist_ok=True)
        (data_dir / "env.json").write_text(
            json.dumps({"AGENTS_WORKSPACE": ws}), encoding="utf-8"
        )
    with patch("runtime.server._MODELS_PATH", str(data_dir / "models.json")), \
         patch("runtime.server._TOOLS_PATH", str(data_dir / "tools.json")), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", str(data_dir / "prompt_templates.json")), \
         patch("runtime.server._DATA_DIR", str(data_dir)), \
         patch("runtime.server._ENV_PATH", str(data_dir / "env.json")), \
         patch("runtime.server._REMOTE_ENVS_PATH", str(data_dir / "remote_envs.json")), \
         patch("runtime.server._AUTH_PATH", str(data_dir / "auth_token.json")), \
         patch("runtime.server._AGENTS_DIR", str(data_dir / "agents")):
        srv = RuntimeHTTPServer()
        srv.start_background(host="127.0.0.1", port=0)
    try:
        _wait_hello(srv)
        yield srv, data_dir
    finally:
        srv.stop()


@pytest.fixture()
def parent(tmp_path):
    with _real_server(tmp_path, "parent_data") as result:
        yield result


@pytest.fixture()
def child(tmp_path):
    with _real_server(tmp_path, "child_data", workspace=tmp_path / "child_ws") as result:
        yield result


def _add_env(parent, url):
    status, body = _request(parent[0], "POST", "/v1/remote-envs", {"url": url})
    assert status == 200
    return body["envs"][0]["id"]


def _child_write_file(child, session_id, path, content, timestamp=TURN_TS, token=None):
    """Call the child's write_file via /v1/tools/call with session context.

    Session context travels in the ``X-Agents-Request-Context`` header; the
    JSON body carries only ``tool_id`` + ``arguments`` (no session fields).
    """
    payload = {
        "tool_id": "write_file",
        "arguments": {"path": path, "content": content},
    }
    headers = _forwarded_headers(session_id=session_id, user_message_timestamp=timestamp)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, text = _request_text(child[0], "POST", "/v1/tools/call", payload, headers)
    assert status == 200, text
    return text


# ---------------------------------------------------------------------------
# Request-context codec (unified X-Agents-Request-Context header)
# ---------------------------------------------------------------------------

class TestForwardedContextCodec:
    def test_round_trip_preserves_unicode_workspace(self):
        from runtime.common import (
            encode_forwarded_context, decode_forwarded_context,
        )
        ctx = {"workspace": r"D:\项目\子目录", "session_id": "s-1",
               "depth": 0, "agent_ids": ["a", "b"]}
        raw = encode_forwarded_context(ctx)
        assert raw.isascii()
        assert decode_forwarded_context(raw) == ctx

    def test_unknown_keys_are_dropped_on_decode(self):
        from runtime.common import (
            encode_forwarded_context, decode_forwarded_context,
        )
        raw = encode_forwarded_context({"session_id": "s", "sse_callback": "x"})
        assert decode_forwarded_context(raw) == {"session_id": "s"}

    def test_base64_auto_flag_round_trips(self):
        # The web tool-call test page sends {"base64":"auto"} in the header
        # to opt into inference-loop base64 marshalling; the key must
        # survive the allow-list filter.
        from runtime.common import (
            encode_forwarded_context, decode_forwarded_context,
        )
        raw = encode_forwarded_context({"base64": "auto"})
        assert decode_forwarded_context(raw) == {"base64": "auto"}

    def test_malformed_header_is_tolerated(self):
        from runtime.common import decode_forwarded_context
        assert decode_forwarded_context(None) == {}
        assert decode_forwarded_context("") == {}
        assert decode_forwarded_context("not-base64!!") == {}
        assert decode_forwarded_context("aGVsbG8=") == {}  # "hello", not a dict

    def test_build_forwarded_context_uses_the_allow_list(self):
        from runtime.common import (
            build_forwarded_context, clear_request_context,
            FORWARDED_CONTEXT_KEYS, set_request_context,
        )
        set_request_context(session_id="sess-1", workspace="/w")
        try:
            fwd = build_forwarded_context()
        finally:
            clear_request_context(["session_id", "workspace"])
        assert fwd["session_id"] == "sess-1"
        assert fwd["workspace"] == "/w"
        assert set(fwd) <= set(FORWARDED_CONTEXT_KEYS)


# ---------------------------------------------------------------------------
# Proxy URL / namespacing
# ---------------------------------------------------------------------------

class TestProxyUrlParsing:
    def test_bare_host(self):
        p = RemoteToolProxy({"id": "http://172.28.70.13:7988", "url": "http://172.28.70.13:7988/v1/setup"})
        assert p.base == "http://172.28.70.13:7988"
        assert p.token == ""

    def test_deploy_prefix_and_token(self):
        p = RemoteToolProxy({
            "id": "https://a.b.com:8443",
            "url": "https://a.b.com:8443/sub/v1/setup?token=as_123",
        })
        assert p.base == "https://a.b.com:8443/sub"
        assert p.token == "as_123"

    def test_build_url_appends_token_once(self):
        p = RemoteToolProxy({
            "id": "http://h:1", "url": "http://h:1/v1/setup?token=t",
        })
        assert p._build_url("/v1/tools") == "http://h:1/v1/tools?token=t"


# ---------------------------------------------------------------------------
# Proxy registration / forwarding against a real child
# ---------------------------------------------------------------------------

class TestProxyAgainstRealChild:
    def test_list_tool_configs_child_tools(self, child):
        p = RemoteToolProxy({"id": f"http://127.0.0.1:{child[0].port}",
                             "url": f"http://127.0.0.1:{child[0].port}/v1/setup"})
        configs = p.list_tool_configs()
        assert configs, "no tool configs"
        ids = {c.tool_id for c in configs}
        # built-ins are proxied with the child's original ids
        assert "write_file" in ids
        assert "exec_cli" in ids
        # delegate / talk_to are NOT filtered out (max-capability policy)
        assert "delegate" in ids
        assert "talk_to" in ids
        # proxy configs are marked builtin (registry.save() would skip them)
        cfg = next(c for c in configs if c.tool_id == "write_file")
        assert cfg.builtin is True
        assert cfg.tool_type == "function"
        assert getattr(cfg, "is_remote_proxy", False) is True
        # id/name keep the child's original values (model / conversation
        # display are identical to local execution)
        assert cfg.name == "write_file"
        # callable attached on the config for function tools
        assert cfg.callable_fn is not None
        # long-execution labels are preserved (delegate timeout behavior)
        delegate_cfg = next(c for c in configs if c.tool_id == "delegate")
        assert "long-execution" in (delegate_cfg.labels or [])

    def test_call_forwarded_with_session_context(self, child, tmp_path):
        child_ws = tmp_path / "child_ws"
        p = RemoteToolProxy({"id": f"http://127.0.0.1:{child[0].port}",
                             "url": f"http://127.0.0.1:{child[0].port}/v1/setup"})
        # Emulate the inference request context set by _prepare_infer_request
        from runtime.common import set_request_context
        set_request_context(session_id="sess-proxy-1", user_message_timestamp=TURN_TS)
        try:
            result = p.call("write_file", {"path": "proxy_ok.txt", "content": "via-proxy"})
            # the callable attached by list_tool_configs() is what the
            # inference loop invokes (Runtime._resolve_tool_callable prefers it)
            cfg = next(c for c in p.list_tool_configs() if c.tool_id == "write_file")
            result2 = cfg.callable_fn(path="via_callable.txt", content="via-callable")
        finally:
            from runtime.common import clear_request_context
            clear_request_context(["session_id", "user_message_timestamp"])
        assert not result.startswith("Error:"), result
        assert (child_ws / "proxy_ok.txt").read_text(encoding="utf-8") == "via-proxy"
        assert not result2.startswith("Error:"), result2
        assert (child_ws / "via_callable.txt").read_text(encoding="utf-8") == "via-callable"

    def test_call_forwards_workspace_from_context(self, child, tmp_path):
        """The parent request-context workspace (the session workspace held
        in the parent's _thread_local in remote mode) travels to the child
        inside the X-Agents-Request-Context header (not in the
        /v1/tools/call JSON payload) and pins the child's execution to it."""
        child_ws = tmp_path / "child_ws"
        sub = child_ws / "sub2"
        sub.mkdir()
        p = RemoteToolProxy({"id": f"http://127.0.0.1:{child[0].port}",
                             "url": f"http://127.0.0.1:{child[0].port}/v1/setup"})
        from runtime.common import set_request_context, clear_request_context
        set_request_context(session_id="sess-ws-3", user_message_timestamp=TURN_TS,
                            workspace=str(sub))
        try:
            result = p.call("write_file", {"path": "fwd.txt", "content": "fwd"})
        finally:
            clear_request_context(["session_id", "user_message_timestamp", "workspace"])
        assert not result.startswith("Error:"), result
        assert (sub / "fwd.txt").read_text(encoding="utf-8") == "fwd"
        assert not (child_ws / "fwd.txt").exists()

    def test_child_mcp_tool_is_base64_marshalled_by_parent(self, parent, child):
        """Remote MCP tools: the child executes verbatim and the parent owns
        base64 marshalling.

        ``list_tool_configs`` exposes a child MCP tool as a parent ``function``
        entry, records the child's original type, and the parent inference
        pipeline then reads base64 input files from / saves long base64
        results to the *parent* filesystem around the forwarded call.
        """
        import base64
        import re

        from runtime.models import ToolConfig

        payload = base64.b64encode(b"\x89PNG" + b"m" * 2000).decode()
        child_runtime = child[0]._server.runtime  # type: ignore[attr-defined]
        child_runtime._tool_registry.register(ToolConfig(  # type: ignore[attr-defined]
            tool_id="mcp_shot",
            tool_type="mcp",
            name="mcp_shot",
            description="fake child mcp tool",
            parameters={"type": "object", "properties": {}},
            mcp_server_name="fake-srv",
            tool_name="shot",
        ))

        class _FakeMcpManager:
            def call_tool(self, server_name, tool_name, arguments, timeout=None):
                return f'{{"screenshot": "{payload}"}}'

        child_runtime._mcp_manager = _FakeMcpManager()  # type: ignore[attr-defined]

        proxy = RemoteToolProxy({
            "id": f"http://127.0.0.1:{child[0].port}",
            "url": f"http://127.0.0.1:{child[0].port}/v1/setup",
        })
        cfg = next(c for c in proxy.list_tool_configs() if c.tool_id == "mcp_shot")
        assert cfg.tool_type == "function"
        assert cfg.remote_child_tool_type == "mcp"

        # 1. Child endpoint (what the parent forwards to) is verbatim.
        raw = proxy.call("mcp_shot", {})
        assert payload in raw and "filePath" not in raw

        # 2. The parent interception saves the base64 locally instead.
        parent_runtime = parent[0]._server.runtime  # type: ignore[attr-defined]
        os.environ["BASE64_CHECK_THRESHOLD"] = "1024"
        try:
            out, _config = parent_runtime._execute_tool_call(  # type: ignore[attr-defined]
                "mcp_shot", {}, tool_scope=[cfg])
        finally:
            del os.environ["BASE64_CHECK_THRESHOLD"]
        assert payload not in out
        match = re.search(r'"filePath":\s*"([^"]+)"', out)
        assert match, out
        assert open(match.group(1), "rb").read() == b"\x89PNG" + b"m" * 2000

    def test_child_resolves_base64_path_when_parent_forwards_intent(self, child, tmp_path):
        """A path-like base64 argument that only exists on the *child* is read
        there when the parent forwards ``{"base64":"auto"}``.

        The parent's own pre-processing only rewrites paths it can read
        locally, so a child-workspace path would otherwise reach the MCP tool
        verbatim and fail (the OCR tool's "Incorrect padding").  The runtime
        sets ``base64`` in the request context for remote MCP tools
        (``Runtime._forwards_base64_intent``); this asserts the proxy carries
        it and the child converts the path against its own filesystem.
        """
        import base64

        from runtime.common import (
            clear_request_context, set_request_context,
        )
        from runtime.models import ToolConfig

        child_ws = tmp_path / "child_ws"
        img = child_ws / "shot.png"
        img.write_bytes(b"\x89PNGchild-only")

        seen: dict = {}

        class _FakeMcpManager:
            def call_tool(self, server_name, tool_name, arguments, timeout=None):
                seen.update(arguments)
                return json.dumps({"success": True, "full_text": "ok"})

        child_runtime = child[0]._server.runtime  # type: ignore[attr-defined]
        child_runtime._tool_registry.register(ToolConfig(  # type: ignore[attr-defined]
            tool_id="mcp_ocr",
            tool_type="mcp",
            name="mcp_ocr",
            description="fake child mcp ocr",
            parameters={"type": "object", "properties": {
                "base64_content": {"type": "string"}}},
            mcp_server_name="fake-ocr",
            tool_name="ocr",
        ))
        child_runtime._mcp_manager = _FakeMcpManager()  # type: ignore[attr-defined]

        proxy = RemoteToolProxy({
            "id": f"http://127.0.0.1:{child[0].port}",
            "url": f"http://127.0.0.1:{child[0].port}/v1/setup",
        })

        # Parent request context as the runtime sets it around a remote MCP
        # tool call (Runtime._forwards_base64_intent).
        set_request_context(
            session_id="sess-b64-1", user_message_timestamp=TURN_TS,
            base64="auto",
        )
        try:
            result = proxy.call("mcp_ocr", {"base64_content": str(img)})
        finally:
            clear_request_context(
                ["session_id", "user_message_timestamp", "base64"])

        assert not result.startswith("Error:"), result
        # The child read its OWN file and handed the MCP tool real base64.
        assert base64.b64decode(seen["base64_content"]) == img.read_bytes()

        # Without the forwarded intent the child stays a raw executor.
        seen.clear()
        status, text = _request_text(
            child[0], "POST", "/v1/tools/call",
            {"tool_id": "mcp_ocr", "arguments": {"base64_content": str(img)}},
        )
        assert status == 200, text
        assert seen["base64_content"] == str(img)

    def test_call_failure_returns_error_text(self, child):
        p = RemoteToolProxy({"id": f"http://127.0.0.1:{child[0].port}",
                             "url": f"http://127.0.0.1:{child[0].port}/v1/setup"})
        result = p.call("no_such_tool", {})
        assert result.startswith("Error:")

    def test_unreachable_child_raises(self, parent):
        p = RemoteToolProxy({"id": "http://127.0.0.1:1", "url": "http://127.0.0.1:1/v1/setup"})
        with pytest.raises(Exception) as exc_info:
            p.list_tools()
        assert "unreachable" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Child-side /v1/tools/call session context (B2)
# ---------------------------------------------------------------------------

class TestChildToolCallSessionContext:
    def test_write_file_creates_journal_in_child_session_dir(self, child, tmp_path):
        _child_write_file(child, "sess-j-1", "journaled.txt", "hello")
        child_ws = tmp_path / "child_ws"
        assert (child_ws / "journaled.txt").read_text(encoding="utf-8") == "hello"
        session_dir = child[1] / "chat_data" / "sess-j-1"
        manifests = list(session_dir.glob("file_journals/*/manifest.json"))
        assert manifests, "no file journal manifest created"
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        assert manifest.get("session_id") == "sess-j-1"
        assert "journaled.txt" in json.dumps(manifest)

    def test_write_file_without_session_still_works(self, child, tmp_path):
        status, text = _request_text(
            child[0], "POST", "/v1/tools/call",
            {"tool_id": "write_file",
             "arguments": {"path": "plain.txt", "content": "no-session"}},
        )
        assert status == 200
        assert (tmp_path / "child_ws" / "plain.txt").read_text(encoding="utf-8") == "no-session"

    def test_tool_call_honors_forwarded_workspace(self, child, tmp_path):
        """The X-Agents-Request-Context workspace forwarded by the parent's
        remote tool proxy pins the tool call (relative paths and exec_shell
        cwd resolve against it), replicating the parent's context on the
        child."""
        child_ws = tmp_path / "child_ws"
        sub = child_ws / "sub"
        sub.mkdir()
        payload = {
            "tool_id": "write_file",
            "arguments": {"path": "in_sub.txt", "content": "ws"},
        }
        status, text = _request_text(
            child[0], "POST", "/v1/tools/call", payload,
            headers=_forwarded_headers(
                workspace=str(sub), session_id="sess-ws-1",
                user_message_timestamp=TURN_TS,
            ),
        )
        assert status == 200, text
        assert (sub / "in_sub.txt").read_text(encoding="utf-8") == "ws"
        assert not (child_ws / "in_sub.txt").exists()

    def test_tool_call_ignores_body_session_context(self, child, tmp_path):
        """Session context is read *only* from the X-Agents-Request-Context
        header; the same keys in the JSON body are ignored. A direct caller
        that stuffs them into the body therefore runs against the child's
        default workspace instead of the (untrusted) body path."""
        child_ws = tmp_path / "child_ws"
        sub = child_ws / "sub3"
        sub.mkdir()
        payload = {
            "tool_id": "write_file",
            "arguments": {"path": "direct.txt", "content": "direct"},
            "workspace": str(sub),
            "session_id": "should-be-ignored",
            "user_message_timestamp": TURN_TS,
        }
        status, text = _request_text(child[0], "POST", "/v1/tools/call", payload)
        assert status == 200, text
        # body workspace / session_id are ignored -> child default workspace
        assert (child_ws / "direct.txt").read_text(encoding="utf-8") == "direct"
        assert not (sub / "direct.txt").exists()

    def test_tool_call_ignores_missing_workspace(self, child, tmp_path):
        """A forwarded workspace that does not exist on the child (e.g. a
        stale parent-side path) falls back to the child default instead of
        failing the call."""
        child_ws = tmp_path / "child_ws"
        payload = {
            "tool_id": "write_file",
            "arguments": {"path": "fallback.txt", "content": "fb"},
        }
        status, text = _request_text(
            child[0], "POST", "/v1/tools/call", payload,
            headers=_forwarded_headers(
                workspace=str(tmp_path / "does_not_exist"),
                session_id="sess-ws-2", user_message_timestamp=TURN_TS,
            ),
        )
        assert status == 200, text
        assert (child_ws / "fallback.txt").read_text(encoding="utf-8") == "fb"

    def test_forwarded_context_rebuilds_child_request_context(self, child, tmp_path):
        """The X-Agents-Request-Context header carries the portable subset
        (session_id / timestamp / depth / agent_id / agent_ids /
        available_tool_ids); the child applies them and rebuilds the
        host-specific parts (session_dir, tool_scope, server singletons)
        from its own host."""
        _register_child_probe(child)
        status, text = _request_text(
            child[0], "POST", "/v1/tools/call",
            {"tool_id": "ctx_probe", "arguments": {}},
            headers=_forwarded_headers(
                session_id="sess-ctx-1",
                user_message_timestamp=TURN_TS,
                depth=2,
                agent_id="agent-x",
                agent_ids=["agent-x", "agent-y"],
                all_agent_ids=["agent-x", "agent-y"],
                available_tool_ids=["write_file"],
            ),
        )
        assert status == 200, text
        seen = json.loads(text)
        assert seen["session_id"] == "sess-ctx-1"
        assert seen["user_message_timestamp"] == TURN_TS
        assert seen["depth"] == 2
        assert seen["agent_id"] == "agent-x"
        assert seen["agent_ids"] == ["agent-x", "agent-y"]
        assert seen["all_agent_ids"] == ["agent-x", "agent-y"]
        # tool_scope is rebuilt from THIS host's registry
        assert seen["tool_scope"] == ["write_file"]
        # host-specific values are child-derived, not forwarded
        assert seen["session_dir"]
        assert seen["has_agent_manager"] is True

    def test_undo_restores_child_file(self, child, tmp_path):
        child_ws = tmp_path / "child_ws"
        _child_write_file(child, "sess-j-2", "undo_me.txt", "v1")
        # second turn modifies the file
        _child_write_file(child, "sess-j-2", "undo_me.txt", "v2",
                          timestamp="2026-01-02T11:00:00")
        assert (child_ws / "undo_me.txt").read_text(encoding="utf-8") == "v2"
        status, text = _request_text(
            child[0], "POST", "/v1/tools/call",
            {"tool_id": "undo", "arguments": {}},
            headers=_forwarded_headers(session_id="sess-j-2"),
        )
        assert status == 200, text
        assert (child_ws / "undo_me.txt").read_text(encoding="utf-8") == "v1"


# ---------------------------------------------------------------------------
# Child-side authorization extensions (B1)
# ---------------------------------------------------------------------------

class TestChildAuth:
    def _enable_auth(self, child):
        status, config = _request(child[0], "POST", "/v1/auth/config", {"password": "test-pass"})
        assert status == 200
        token = config.get("setup_token", "")
        assert token
        return token

    def test_get_requires_token(self, child):
        token = self._enable_auth(child)
        status, _ = _request(child[0], "GET", "/v1/tools")
        assert status == 401
        status, _ = _request(child[0], "GET", f"/v1/tools?token={token}")
        assert status == 200

    def test_get_file_journals_with_token(self, child):
        token = self._enable_auth(child)
        _child_write_file(child, "sess-auth-1", "a.txt", "x", token=token)
        status, body = _request(
            child[0], "GET",
            f"/v1/sessions/sess-auth-1/file-journals?token={token}",
        )
        assert status == 200
        assert body["turn_keys"]

    def test_post_with_bearer_setup_token(self, child):
        token = self._enable_auth(child)
        status, text = _request_text(
            child[0], "POST", "/v1/tools/call",
            {"tool_id": "write_file",
             "arguments": {"path": "bearer.txt", "content": "ok"}},
            headers={"Authorization": f"Bearer {token}",
                     **_forwarded_headers(session_id="sess-auth-2")},
        )
        assert status == 200, text
        status, _ = _request_text(
            child[0], "POST", "/v1/tools/call",
            {"tool_id": "write_file",
             "arguments": {"path": "bearer.txt", "content": "ok"}},
            headers=_forwarded_headers(session_id="sess-auth-2"),
        )
        assert status == 401


# ---------------------------------------------------------------------------
# Skill body endpoint (B3)
# ---------------------------------------------------------------------------

class TestSkillBodyEndpoint:
    def _register_skill(self, child, tmp_path):
        skill_dir = tmp_path / "skill_demo"
        skill_dir.mkdir(exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: skill-demo\ndescription: demo skill\n---\n\n# Demo\nRun the demo.",
            encoding="utf-8",
        )
        status, body = _request(
            child[0], "POST", "/v1/tools/skill", {"skill_dir": str(skill_dir)},
        )
        assert status == 201, body
        return body

    def test_returns_body_and_dir(self, child, tmp_path):
        body = self._register_skill(child, tmp_path)
        tool_id = body.get("tool_id") or body.get("tool", {}).get("tool_id")
        assert tool_id
        status, data = _request(child[0], "GET", f"/v1/tools/skill/{tool_id}")
        assert status == 200
        assert "Demo" in data["body"]
        assert data["skill_dir"]

    def test_unknown_skill_404(self, child):
        status, _ = _request(child[0], "GET", "/v1/tools/skill/nope")
        assert status == 404

    def test_proxy_fetch_skill_body(self, child, tmp_path):
        body = self._register_skill(child, tmp_path)
        tool_id = body.get("tool_id") or body.get("tool", {}).get("tool_id")
        p = RemoteToolProxy({"id": f"http://127.0.0.1:{child[0].port}",
                             "url": f"http://127.0.0.1:{child[0].port}/v1/setup"})
        # skills stay tool_type=skill (no callable) for parent disclosure
        cfg = next(c for c in p.list_tool_configs() if c.tool_id == tool_id)
        assert cfg.tool_type == "skill"
        assert not hasattr(cfg, "callable_fn")
        skill_body, skill_dir = p.fetch_skill_body(tool_id)
        assert skill_body and "Demo" in skill_body
        assert skill_dir
        # the model invokes skills by the child's original name (same as
        # local sessions); the proxy must resolve that too
        body2, _ = p.fetch_skill_body(cfg.name)
        assert body2 and "Demo" in body2


# ---------------------------------------------------------------------------
# journal_only revoke (B4)
# ---------------------------------------------------------------------------

class TestJournalOnlyRevoke:
    def test_restores_child_files(self, child, tmp_path):
        child_ws = tmp_path / "child_ws"
        _child_write_file(child, "sess-ro-1", "ro.txt", "content-v1")
        status, body = _request(
            child[0], "POST", "/v1/sessions/sess-ro-1/revoke?journal_only=true",
            {"timestamp": TURN_TS},
        )
        assert status == 200, body
        # the file was created by the journaled turn -> restored = removed
        assert not (child_ws / "ro.txt").exists()
        status, body = _request(
            child[0], "POST", "/v1/sessions/sess-ro-1/revoke?journal_only=true",
            {"timestamp": TURN_TS},
        )
        assert status == 200, body
        # second revoke finds no matching (non-revoked) journals
        assert body.get("journal", {}).get("skipped") is True

    def test_conflict_returns_409_structure(self, child, tmp_path):
        child_ws = tmp_path / "child_ws"
        _child_write_file(child, "sess-ro-3", "ro3.txt", "baseline-after")
        (child_ws / "ro3.txt").write_text("drifted", encoding="utf-8")
        status, body = _request(
            child[0], "POST", "/v1/sessions/sess-ro-3/revoke?journal_only=true",
            {"timestamp": TURN_TS},
        )
        assert status == 409
        assert body.get("error") == "JournalConflict"
        assert any("ro3.txt" in f for f in body.get("files", []))
        assert body.get("can_force") is True

    def test_forced_revoke_restores(self, child, tmp_path):
        child_ws = tmp_path / "child_ws"
        _child_write_file(child, "sess-ro-4", "ro4.txt", "keep-me")
        (child_ws / "ro4.txt").write_text("drifted", encoding="utf-8")
        status, body = _request(
            child[0], "POST", "/v1/sessions/sess-ro-4/revoke?journal_only=true",
            {"timestamp": TURN_TS, "forced": True},
        )
        assert status == 200, body
        assert not (child_ws / "ro4.txt").exists()


# ---------------------------------------------------------------------------
# Parent-side routing (A2/A3/A6/A7)
# ---------------------------------------------------------------------------

class TestParentRemoteRouting:
    def _make_parent_session(self, parent, env_id, timestamp=TURN_TS):
        """Create a minimal parent conversation with a remote_env binding."""
        sid = "sess-parent-1"
        session_dir = parent[1] / "chat_data" / sid
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "conversation.json").write_text(
            json.dumps({
                "meta": {"remote_env": env_id, "turn_count": 1},
                "messages": [
                    {"role": "user", "content": "hello", "timestamp": timestamp},
                ],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        return sid

    def test_revoke_routes_journal_to_child_first(self, parent, child, tmp_path):
        env_id = _add_env(parent, f"http://127.0.0.1:{child[0].port}/v1/setup")
        sid = self._make_parent_session(parent, env_id)
        # The child owns a journaled file change for the same session id.
        child_ws = tmp_path / "child_ws"
        _child_write_file(child, sid, "child_owned.txt", "owned")
        assert (child_ws / "child_owned.txt").exists()

        status, body = _request(
            parent[0], "POST", f"/v1/sessions/{sid}/revoke",
            {"timestamp": TURN_TS},
        )
        assert status == 200, body
        # child file restored (created by the journaled turn -> removed)
        assert not (child_ws / "child_owned.txt").exists()
        # parent conversation truncated
        conv = json.loads(
            (parent[1] / "chat_data" / sid / "conversation.json").read_text(encoding="utf-8")
        )
        assert conv["messages"] == []

    def test_revoke_child_conflict_keeps_local_messages(self, parent, child, tmp_path):
        env_id = _add_env(parent, f"http://127.0.0.1:{child[0].port}/v1/setup")
        sid = self._make_parent_session(parent, env_id)
        child_ws = tmp_path / "child_ws"
        _child_write_file(child, sid, "conflict.txt", "after-state")
        (child_ws / "conflict.txt").write_text("drifted", encoding="utf-8")

        status, body = _request(
            parent[0], "POST", f"/v1/sessions/{sid}/revoke",
            {"timestamp": TURN_TS},
        )
        assert status == 409
        assert body.get("error") == "JournalConflict"
        # local messages must NOT be deleted while the child journal conflicts
        conv = json.loads(
            (parent[1] / "chat_data" / sid / "conversation.json").read_text(encoding="utf-8")
        )
        assert len(conv["messages"]) == 1

    def test_revoke_unreachable_child_is_502(self, parent):
        # Registered env points at a dead port.
        status, body = _request(parent[0], "POST", "/v1/remote-envs", {
            "url": "http://127.0.0.1:1/v1/setup",
        })
        assert status == 200
        env_id = body["envs"][0]["id"]
        sid = self._make_parent_session(parent, env_id)
        status, body = _request(
            parent[0], "POST", f"/v1/sessions/{sid}/revoke",
            {"timestamp": TURN_TS},
        )
        assert status == 502

    def test_revoke_offline_tunnel_env_is_502(self, parent):
        # Tunnel env record with no live connection: the proxy raises
        # RemoteToolCallError (a RuntimeError, NOT OSError/URLError) — the
        # handler must still answer with a clean 502 JSON instead of
        # escaping the exception and dropping the connection.
        envs_path = parent[1] / "remote_envs.json"
        envs = (
            json.loads(envs_path.read_text(encoding="utf-8"))
            if envs_path.exists() else []
        )
        tunnel_id = "abcd1234ef567890"
        envs.append({
            "id": "tunnel:" + tunnel_id,
            "url": "",
            "transport": "ws-tunnel",
            "tunnel_id": tunnel_id,
            "created_at": TURN_TS,
            "online": False,
            "last_seen": "",
        })
        envs_path.write_text(json.dumps(envs, ensure_ascii=False), encoding="utf-8")
        sid = self._make_parent_session(parent, "tunnel:" + tunnel_id)
        status, body = _request(
            parent[0], "POST", "/v1/sessions/" + sid + "/revoke",
            {"timestamp": TURN_TS},
        )
        assert status == 502, body
        assert "Cannot reach remote environment" in body.get("error", "")

    def test_delete_session_cleans_child_session_dir(self, parent, child):
        env_id = _add_env(parent, f"http://127.0.0.1:{child[0].port}/v1/setup")
        sid = self._make_parent_session(parent, env_id)
        # Child session dir exists (orphan journals + delegate sub-sessions)
        _child_write_file(child, sid, "orphan.txt", "x")
        child_session_dir = child[1] / "chat_data" / sid
        assert child_session_dir.is_dir()

        status, body = _request(parent[0], "DELETE", f"/v1/sessions/{sid}")
        assert status == 200
        # parent session dir is gone immediately
        assert not (parent[1] / "chat_data" / sid).exists()
        # child cleanup is fire-and-forget: poll briefly
        deadline = time.time() + 10
        while child_session_dir.exists() and time.time() < deadline:
            time.sleep(0.1)
        assert not child_session_dir.exists(), "child orphan session dir not cleaned"

    def test_infer_unknown_remote_env_404(self, parent):
        status, body = _request(
            parent[0], "POST", "/v1/infer/stream",
            {
                "model_id": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "session_id": "new",
                "remote_env": "http://nope.example:1",
            },
        )
        assert status == 404

    def test_infer_dead_child_degrades_to_tool_call_error(self, parent):
        """Simplified design: the tool list comes only from the parent
        registry, so an unreachable child no longer fails request
        preparation with 502 — the model still gets the parent's tools, and
        only the actual tool call returns an error result."""
        from runtime.models import ModelConfig

        status, body = _request(parent[0], "POST", "/v1/remote-envs", {
            "url": "http://127.0.0.1:1/v1/setup",
        })
        assert status == 200
        env_id = body["envs"][0]["id"]

        parent_runtime = parent[0]._server.runtime  # type: ignore[attr-defined]
        parent_runtime._model_registry.register(  # type: ignore[attr-defined]
            ModelConfig(
                model_id="e2e-model",
                api_base="http://model.invalid",
                model_name="e2e",
                api_protocol="openai",
            )
        )

        tool_call_chunk = json.dumps({
            "choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1",
                 "function": {"name": "write_file",
                              "arguments": json.dumps(
                                  {"path": "x.txt", "content": "x"})}},
            ]}}],
        })
        final_chunk = json.dumps({"choices": [{"delta": {"content": "done"}}]})
        streams = [
            io.BytesIO(f"data: {tool_call_chunk}\n\ndata: [DONE]\n\n".encode()),
            io.BytesIO(f"data: {final_chunk}\n\ndata: [DONE]\n\n".encode()),
        ]
        real_urlopen = urllib.request.urlopen

        def selective_urlopen(req, **kwargs):
            url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
            if url.startswith("http://model.invalid"):
                stream = streams.pop(0)
                mock_resp = MagicMock()
                mock_resp.__iter__ = lambda self: iter(stream.readlines())
                mock_resp.read = stream.read
                mock_resp.close = MagicMock()
                mock_resp.__enter__ = lambda s: s
                mock_resp.__exit__ = MagicMock(return_value=False)
                return mock_resp
            # proxy -> dead child: real connection attempt (refused)
            return real_urlopen(req, **kwargs)

        with patch("urllib.request.urlopen", side_effect=selective_urlopen):
            status, body = _request(parent[0], "POST", "/v1/infer", {
                "model_id": "e2e-model",
                "tool_ids": ["write_file"],
                "messages": [{"role": "user", "content": "write a file"}],
                "session_id": "new",
                "remote_env": env_id,
            })
        assert status == 200, body
        assert body.get("success") is True, body
        tool_msgs = [m for m in body.get("messages", []) if m.get("role") == "tool"]
        assert tool_msgs, body.get("messages")
        assert tool_msgs[0].get("content", "").startswith("Error:"), tool_msgs[0]


# ---------------------------------------------------------------------------
# End-to-end: parent inference loop + child proxy tool + child exec
# ---------------------------------------------------------------------------

class TestRemoteInferEndToEnd:
    def test_remote_tool_call_round_trip(self, parent, child, tmp_path):
        from runtime.models import ModelConfig

        env_id = _add_env(parent, f"http://127.0.0.1:{child[0].port}/v1/setup")
        child_base = f"http://127.0.0.1:{child[0].port}"

        # Parent-side model (OpenAI protocol; urlopen is mocked per-URL below)
        parent_runtime = parent[0]._server.runtime  # type: ignore[attr-defined]
        parent_runtime._model_registry.register(  # type: ignore[attr-defined]
            ModelConfig(
                model_id="e2e-model",
                api_base="http://model.invalid",
                model_name="e2e",
                api_protocol="openai",
            )
        )

        def _sse(lines):
            return io.BytesIO("".join(lines).encode("utf-8"))

        tool_call_chunk = json.dumps({
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {
                                    "name": "write_file",
                                    "arguments": json.dumps(
                                        {"path": "e2e.txt", "content": "remote-e2e"}
                                    ),
                                },
                            },
                        ],
                    },
                },
            ],
        })
        final_chunk = json.dumps({"choices": [{"delta": {"content": "done"}}]})
        streams = [
            _sse([f"data: {tool_call_chunk}\n\n", "data: [DONE]\n\n"]),
            _sse([f"data: {final_chunk}\n\n", "data: [DONE]\n\n"]),
        ]
        real_urlopen = urllib.request.urlopen

        def selective_urlopen(req, **kwargs):
            url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
            if url.startswith("http://model.invalid"):
                # model endpoint: replay the scripted OpenAI stream
                stream = streams.pop(0)
                mock_resp = MagicMock()
                mock_resp.__iter__ = lambda self: iter(stream.readlines())
                mock_resp.read = stream.read
                mock_resp.close = MagicMock()
                mock_resp.__enter__ = lambda s: s
                mock_resp.__exit__ = MagicMock(return_value=False)
                return mock_resp
            # everything else (parent API + proxy -> child) is real HTTP
            return real_urlopen(req, **kwargs)

        with patch("urllib.request.urlopen", side_effect=selective_urlopen):
            status, body = _request(parent[0], "POST", "/v1/infer", {
                "model_id": "e2e-model",
                "tool_ids": ["write_file"],
                "messages": [{"role": "user", "content": "write a file"}],
                "session_id": "new",
                "remote_env": env_id,
            })
        assert status == 200, body
        assert body.get("success") is True, body
        sid = body.get("session_id")
        assert sid

        # 1. the tool actually ran in the CHILD workspace
        assert (tmp_path / "child_ws" / "e2e.txt").read_text(encoding="utf-8") == "remote-e2e"
        # 2. child file journal exists under the PARENT session id
        child_session_dir = child[1] / "chat_data" / sid
        assert list(child_session_dir.glob("file_journals/*/manifest.json")), "no child journal"
        # 3. parent response carries the child's tool result
        tool_msgs = [m for m in body.get("messages", []) if m.get("role") == "tool"]
        assert tool_msgs, body.get("messages")
        assert "journal_id" in tool_msgs[0].get("content", "")
        # tool messages are recorded with the child's original name and id
        # (compact display behaves exactly like local tools)
        assert tool_msgs[0].get("name") == "write_file"
        assert tool_msgs[0].get("tool_id") == "write_file"
        # 4. binding persisted in parent conversation meta; tool_ids keep the
        # child's original names (the frontend restores them directly)
        conv = json.loads(
            (parent[1] / "chat_data" / sid / "conversation.json").read_text(encoding="utf-8")
        )
        assert conv["meta"].get("remote_env") == env_id
        assert conv["meta"].get("tool_ids") == ["write_file"]
        # 5. the parent registry stays untouched: write_file is still the
        # local built-in (no proxy entries leaked in)
        local_wf = parent_runtime._tool_registry.get("write_file")  # type: ignore[attr-defined]
        assert local_wf is not None
        assert getattr(local_wf, "is_remote_proxy", False) is False
        assert all("__" not in t.tool_id for t in parent_runtime._tool_registry.list_all())  # type: ignore[attr-defined]

    def test_remote_infer_forwards_session_workspace(self, parent, child, tmp_path):
        """Full chain: the parent infer body's ``workspace`` (the session
        workspace shown in the top bar, a child-side path in remote mode)
        reaches the child's tool execution, so relative paths land there."""
        from runtime.models import ModelConfig

        env_id = _add_env(parent, f"http://127.0.0.1:{child[0].port}/v1/setup")
        child_ws = tmp_path / "child_ws"
        sub = child_ws / "session_ws"
        sub.mkdir()

        parent_runtime = parent[0]._server.runtime  # type: ignore[attr-defined]
        parent_runtime._model_registry.register(  # type: ignore[attr-defined]
            ModelConfig(
                model_id="e2e-model-ws",
                api_base="http://model.invalid",
                model_name="e2e",
                api_protocol="openai",
            )
        )

        def _sse(lines):
            return io.BytesIO("".join(lines).encode("utf-8"))

        tool_call_chunk = json.dumps({
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {
                                    "name": "write_file",
                                    "arguments": json.dumps(
                                        {"path": "ws_e2e.txt", "content": "in-session-ws"}
                                    ),
                                },
                            },
                        ],
                    },
                },
            ],
        })
        final_chunk = json.dumps({"choices": [{"delta": {"content": "done"}}]})
        streams = [
            _sse([f"data: {tool_call_chunk}\n\n", "data: [DONE]\n\n"]),
            _sse([f"data: {final_chunk}\n\n", "data: [DONE]\n\n"]),
        ]
        real_urlopen = urllib.request.urlopen

        def selective_urlopen(req, **kwargs):
            url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
            if url.startswith("http://model.invalid"):
                stream = streams.pop(0)
                mock_resp = MagicMock()
                mock_resp.__iter__ = lambda self: iter(stream.readlines())
                mock_resp.read = stream.read
                mock_resp.close = MagicMock()
                mock_resp.__enter__ = lambda s: s
                mock_resp.__exit__ = MagicMock(return_value=False)
                return mock_resp
            return real_urlopen(req, **kwargs)

        with patch("urllib.request.urlopen", side_effect=selective_urlopen):
            status, body = _request(parent[0], "POST", "/v1/infer", {
                "model_id": "e2e-model-ws",
                "tool_ids": ["write_file"],
                "messages": [{"role": "user", "content": "write a file"}],
                "session_id": "new",
                "remote_env": env_id,
                "workspace": str(sub),
            })
        assert status == 200, body
        assert body.get("success") is True, body
        # the file landed in the forwarded session workspace on the CHILD
        assert (sub / "ws_e2e.txt").read_text(encoding="utf-8") == "in-session-ws"
        assert not (child_ws / "ws_e2e.txt").exists()

    def test_remote_infer_auto_exposes_exec_cli_for_open_child_terminal(
        self, parent, child, tmp_path
    ):
        """A remote session whose child has a live terminal auto-exposes
        exec_cli — parity with local execution, driven by the parent backend
        rather than relying on the web frontend to append it."""
        import threading

        from runtime.models import ModelConfig
        from runtime.server_state import _terminal_sessions, _terminal_sessions_lock

        env_id = _add_env(parent, f"http://127.0.0.1:{child[0].port}/v1/setup")
        parent_runtime = parent[0]._server.runtime  # type: ignore[attr-defined]
        parent_runtime._model_registry.register(  # type: ignore[attr-defined]
            ModelConfig(
                model_id="e2e-model",
                api_base="http://model.invalid",
                model_name="e2e",
                api_protocol="openai",
            )
        )

        captured: dict = {}

        def _tool_names():
            names = []
            for tool in (captured.get("body") or {}).get("tools") or []:
                fn = tool.get("function") if isinstance(tool, dict) else None
                names.append((fn or {}).get("name"))
            return names

        def _sse(lines):
            return io.BytesIO("".join(lines).encode("utf-8"))

        final_chunk = json.dumps({"choices": [{"delta": {"content": "ok"}}]})
        real_urlopen = urllib.request.urlopen

        def selective_urlopen(req, **kwargs):
            url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
            if url.startswith("http://model.invalid"):
                try:
                    captured["body"] = json.loads(req.data.decode("utf-8"))
                except Exception:
                    captured["body"] = {}
                stream = _sse([f"data: {final_chunk}\n\n", "data: [DONE]\n\n"])
                mock_resp = MagicMock()
                mock_resp.__iter__ = lambda self: iter(stream.readlines())
                mock_resp.read = stream.read
                mock_resp.close = MagicMock()
                mock_resp.__enter__ = lambda s: s
                mock_resp.__exit__ = MagicMock(return_value=False)
                return mock_resp
            return real_urlopen(req, **kwargs)

        # 1. No child terminal yet -> exec_cli is NOT auto-exposed.
        captured["body"] = None
        with patch("urllib.request.urlopen", side_effect=selective_urlopen):
            status, body = _request(parent[0], "POST", "/v1/infer", {
                "model_id": "e2e-model",
                "tool_ids": [],
                "messages": [{"role": "user", "content": "hi"}],
                "session_id": "new",
                "remote_env": env_id,
            })
        assert status == 200, body
        sid = body.get("session_id")
        assert sid
        assert "exec_cli" not in _tool_names()

        # The terminal runs on the child; inject one directly (in-process the
        # child shares the module-level registry, so /v1/terminals reports it).
        terminal_id = f"{sid}:auto"
        with _terminal_sessions_lock:
            _terminal_sessions[terminal_id] = {
                "session_id": sid,
                "active": True,
                "disconnected_at": None,
                "output_buffer": [],
                "buffer_lock": threading.Lock(),
                "sock": None,
            }
        try:
            # 2. Same session, now with a live child terminal -> exec_cli
            #    appears in the model's tool list without the frontend.
            captured["body"] = None
            with patch("urllib.request.urlopen", side_effect=selective_urlopen):
                status, body = _request(parent[0], "POST", "/v1/infer", {
                    "model_id": "e2e-model",
                    "tool_ids": [],
                    "messages": [{"role": "user", "content": "again"}],
                    "session_id": sid,
                    "remote_env": env_id,
                })
            assert status == 200, body
            assert "exec_cli" in _tool_names()
        finally:
            with _terminal_sessions_lock:
                _terminal_sessions.pop(terminal_id, None)

    def test_remote_infer_skips_local_file_ref_expansion(self, parent, child):
        """Remote sessions must not expand <file> refs against the parent workspace."""
        from runtime.models import ModelConfig

        env_id = _add_env(parent, f"http://127.0.0.1:{child[0].port}/v1/setup")
        parent_runtime = parent[0]._server.runtime  # type: ignore[attr-defined]
        parent_runtime._model_registry.register(  # type: ignore[attr-defined]
            ModelConfig(
                model_id="e2e-model-2",
                api_base="http://model.invalid",
                model_name="e2e",
                api_protocol="openai",
            )
        )
        final_chunk = json.dumps({"choices": [{"delta": {"content": "ok"}}]})
        stream = io.BytesIO((f"data: {final_chunk}\n\ndata: [DONE]\n\n").encode("utf-8"))
        real_urlopen = urllib.request.urlopen
        child_base = f"http://127.0.0.1:{child[0].port}"

        def selective_urlopen(req, **kwargs):
            url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
            if url.startswith("http://model.invalid"):
                mock_resp = MagicMock()
                mock_resp.__iter__ = lambda self: iter(stream.readlines())
                mock_resp.read = stream.read
                mock_resp.close = MagicMock()
                mock_resp.__enter__ = lambda s: s
                mock_resp.__exit__ = MagicMock(return_value=False)
                return mock_resp
            return real_urlopen(req, **kwargs)

        with patch("urllib.request.urlopen", side_effect=selective_urlopen):
            # <file> pointing at a non-existent parent file: local mode would
            # 400; remote mode must keep the raw text and succeed.
            status, body = _request(parent[0], "POST", "/v1/infer", {
                "model_id": "e2e-model-2",
                "tool_ids": [],
                "messages": [{"role": "user", "content": "read <file>no/such/file.txt</file>"}],
                "session_id": "new",
                "remote_env": env_id,
            })
        assert status == 200, body
        assert body.get("success") is True

    def test_remote_infer_stream_round_trip(self, parent, child, tmp_path):
        """/v1/infer/stream: SSE carries the remote tool round-trip end to end."""
        from runtime.models import ModelConfig

        env_id = _add_env(parent, f"http://127.0.0.1:{child[0].port}/v1/setup")

        parent_runtime = parent[0]._server.runtime  # type: ignore[attr-defined]
        parent_runtime._model_registry.register(  # type: ignore[attr-defined]
            ModelConfig(
                model_id="e2e-model-stream",
                api_base="http://model.invalid",
                model_name="e2e",
                api_protocol="openai",
            )
        )

        def _sse(lines):
            return io.BytesIO("".join(lines).encode("utf-8"))

        tool_call_chunk = json.dumps({
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_s1",
                                "function": {
                                    "name": "write_file",
                                    "arguments": json.dumps(
                                        {"path": "e2e-stream.txt", "content": "stream-e2e"}
                                    ),
                                },
                            },
                        ],
                    },
                },
            ],
        })
        final_chunk = json.dumps({"choices": [{"delta": {"content": "stream-done"}}]})
        streams = [
            _sse([f"data: {tool_call_chunk}\n\n", "data: [DONE]\n\n"]),
            _sse([f"data: {final_chunk}\n\n", "data: [DONE]\n\n"]),
        ]
        real_urlopen = urllib.request.urlopen

        def selective_urlopen(req, **kwargs):
            url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
            if url.startswith("http://model.invalid"):
                stream = streams.pop(0)
                mock_resp = MagicMock()
                mock_resp.__iter__ = lambda self: iter(stream.readlines())
                mock_resp.read = stream.read
                mock_resp.close = MagicMock()
                mock_resp.__enter__ = lambda s: s
                mock_resp.__exit__ = MagicMock(return_value=False)
                return mock_resp
            return real_urlopen(req, **kwargs)

        with patch("urllib.request.urlopen", side_effect=selective_urlopen):
            status, raw = _request_text(
                parent[0], "POST", "/v1/infer/stream",
                {
                    "model_id": "e2e-model-stream",
                    "tool_ids": ["write_file"],
                    "messages": [{"role": "user", "content": "write a file"}],
                    "session_id": "new",
                    "remote_env": env_id,
                },
            )
        assert status == 200, raw
        assert "event: init" in raw or '"type": "init"' in raw, raw
        assert "stream-done" in raw
        assert "data: [DONE]" in raw

        # tool executed in the child workspace + child journal under parent session id
        assert (tmp_path / "child_ws" / "e2e-stream.txt").read_text(encoding="utf-8") == "stream-e2e"
        # parent persisted the conversation with the remote binding
        convs = list((parent[1] / "chat_data").glob("*/conversation.json"))
        assert convs, "no parent conversation persisted"
        conv = json.loads(convs[0].read_text(encoding="utf-8"))
        assert conv["meta"].get("remote_env") == env_id
        sid = conv["meta"].get("session_id") or conv.get("session_id")
        assert list((child[1] / "chat_data" / sid).glob("file_journals/*/manifest.json")), "no child journal"
        # the parent registry stays untouched after the stream completes
        assert all("__" not in t.tool_id for t in parent_runtime._tool_registry.list_all())  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Oversized tool result guard (remote vs local)
# ---------------------------------------------------------------------------

class TestRemoteResultLengthGuard:
    def test_remote_proxy_result_truncated_inline_not_tmp(self, monkeypatch):
        """Remote proxy tools: oversized results are truncated with an inline
        preview (child cannot read the parent's /tmp)."""
        from runtime.models import ToolConfig
        from runtime.runtime import Runtime
        from unittest.mock import patch

        cfg = ToolConfig(
            tool_id="exec_shell",
            tool_type="function",
            name="exec_shell",
            description="",
            parameters={"type": "object", "properties": {}},
        )
        cfg.is_remote_proxy = True
        big = "x" * (300_000)
        with patch("runtime.runtime.env_int", side_effect=lambda k, d: (
            8000 if k == "TOOL_RESULT_REMOTE_PREVIEW_LENGTH" else d
        )):
            out = Runtime._guard_tool_result_length(big, cfg)
        assert "/tmp" not in out
        assert out.startswith("工具返回了长度超长的内容")
        assert "x" * 8000 in out
        assert len(out) < 10_000

    def test_local_result_still_uses_tmp_file(self):
        """Local tools keep the original /tmp temp-file behavior."""
        from runtime.models import ToolConfig
        from runtime.runtime import Runtime

        cfg = ToolConfig(
            tool_id="exec_shell",
            tool_type="function",
            name="exec_shell",
            description="",
            parameters={"type": "object", "properties": {}},
        )
        big = "y" * (300_000)
        out = Runtime._guard_tool_result_length(big, cfg)
        assert "/tmp" in out
        assert len(out) < 1000

    def test_small_result_unchanged(self):
        from runtime.runtime import Runtime
        assert Runtime._guard_tool_result_length("small", None) == "small"


# ---------------------------------------------------------------------------
# Tool-call network timeout follows the tool-level effective timeout
# ---------------------------------------------------------------------------

class TestProxyCallTimeout:
    def _proxy(self):
        return RemoteToolProxy({
            "id": "http://10.0.0.8:7988",
            "url": "http://10.0.0.8:7988/v1/setup",
        })

    def test_call_forwards_portable_context_in_header(self):
        """RemoteToolProxy.call ships the portable context subset as one
        X-Agents-Request-Context header and leaves the payload pure."""
        from runtime.common import (
            clear_request_context, decode_forwarded_context,
            FORWARDED_CONTEXT_HEADER, set_request_context,
        )

        proxy = RemoteToolProxy({
            "id": "http://10.0.0.9:7988",
            "url": "http://10.0.0.9:7988/v1/setup",
        })
        seen = {}

        def _fake_http(url, *, method="GET", body=None, bearer=False,
                       timeout=None, extra_headers=None):
            seen["headers"] = extra_headers or {}
            seen["body"] = json.loads(body.decode("utf-8"))
            return 200, b"ok"

        set_request_context(
            workspace="/srv/ws", session_id="sess-fwd",
            user_message_timestamp=TURN_TS, depth=1, agent_id="agent-a",
            agent_ids=["agent-a"], all_agent_ids=["agent-a"],
            model_id="gpt-x", available_tool_ids=["write_file"],
            remote_tool_proxy=object(), sse_callback=lambda *a: None,
        )
        try:
            with patch.object(proxy, "_http_request", _fake_http):
                proxy.call("write_file", {"path": "a.txt", "content": "x"})
        finally:
            clear_request_context([
                "workspace", "session_id", "user_message_timestamp", "depth",
                "agent_id", "agent_ids", "all_agent_ids", "model_id",
                "available_tool_ids", "remote_tool_proxy", "sse_callback",
            ])

        # payload stays a pure tool-call contract
        assert seen["body"] == {
            "tool_id": "write_file",
            "arguments": {"path": "a.txt", "content": "x"},
        }
        fwd = decode_forwarded_context(seen["headers"][FORWARDED_CONTEXT_HEADER])
        assert fwd["workspace"] == "/srv/ws"
        assert fwd["session_id"] == "sess-fwd"
        assert fwd["user_message_timestamp"] == TURN_TS
        assert fwd["depth"] == 1
        assert fwd["agent_id"] == "agent-a"
        assert fwd["agent_ids"] == ["agent-a"]
        assert fwd["all_agent_ids"] == ["agent-a"]
        assert fwd["model_id"] == "gpt-x"
        assert fwd["available_tool_ids"] == ["write_file"]
        # host-local / parent-only keys never leak into the header
        assert "remote_tool_proxy" not in fwd
        assert "sse_callback" not in fwd

    def test_uses_tool_exec_timeout_from_request_context(self):
        """Long-running tools (exec_shell with a big timeout, remote
        delegate/talk_to) must not be cut at the fixed network timeout:
        the proxy uses the tool-level effective timeout the inference loop
        put into the request context."""
        from runtime.common import set_request_context, clear_request_context
        from runtime.remote_tool_proxy import _TOOL_CALL_FALLBACK_TIMEOUT

        proxy = self._proxy()
        seen = {}

        def _fake_http(url, *, method="GET", body=None, bearer=False, timeout=None,
                       extra_headers=None):
            seen["timeout"] = timeout
            return 200, b"ok"

        with patch.object(proxy, "_http_request", _fake_http):
            set_request_context(tool_exec_timeout=432.5)
            try:
                proxy.call("exec_shell", {"command": "sleep 400"})
            finally:
                clear_request_context(["tool_exec_timeout"])
        assert seen["timeout"] == 432.5

        # No tool timeout in the context (guard disabled / non-inference
        # path) -> finite fallback, not the short default.
        with patch.object(proxy, "_http_request", _fake_http):
            proxy.call("exec_shell", {"command": "sleep 400"})
        assert seen["timeout"] == _TOOL_CALL_FALLBACK_TIMEOUT

    def test_admin_calls_keep_default_network_timeout(self):
        """Non-tool admin calls (tool list, skill body, revoke) keep the
        short default network timeout."""
        from runtime.remote_tool_proxy import _HTTP_TIMEOUT_SECONDS

        proxy = self._proxy()
        captured = {}

        class _FakeResp:
            status = 200

            def read(self):
                return b'{"tools": []}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def _fake_urlopen(req, timeout=None):
            captured["timeout"] = timeout
            return _FakeResp()

        with patch("urllib.request.urlopen", _fake_urlopen):
            proxy.list_tools(force=True)
        assert captured["timeout"] == _HTTP_TIMEOUT_SECONDS
# ---------------------------------------------------------------------------
# Session log directory resolution (local conversation dir)
# ---------------------------------------------------------------------------

def _make_parent_session_raw(parent, sid, env_id, timestamp=TURN_TS):
    """Create a minimal parent conversation with a remote_env binding."""
    session_dir = parent[1] / "chat_data" / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "conversation.json").write_text(
        json.dumps({
            "meta": {"remote_env": env_id, "turn_count": 1},
            "messages": [{"role": "user", "content": "hi", "timestamp": timestamp}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return sid


class TestSessionLogDir:
    def test_local_session_log_dir(self, parent):
        # Local session resolves the parent's own conversation directory.
        sid = "sess-log-local"
        (parent[1] / "chat_data" / sid).mkdir(parents=True)
        (parent[1] / "chat_data" / sid / "conversation.json").write_text(
            json.dumps({"meta": {}, "messages": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        status, body = _request(parent[0], "GET", f"/v1/sessions/{sid}/log-dir")
        assert status == 200
        assert body["path"] == str(parent[1] / "chat_data" / sid)

    def test_unknown_session_is_404(self, parent):
        status, body = _request(parent[0], "GET", "/v1/sessions/sess-nope/log-dir")
        assert status == 404

    def test_child_log_dir_missing_is_404(self, child):
        # 子端无 file journal 时不创建会话目录，log-dir 严格 404（不预创建）。
        status, body = _request(child[0], "GET", "/v1/sessions/sess-none/log-dir")
        assert status == 404
        assert not (child[1] / "chat_data" / "sess-none").exists()

    def test_remote_bound_session_log_dir_returns_local_path(self, parent, child):
        # 会话绑定远程环境时 log-dir 仍返回本地（父端）会话目录：推理在父端
        # 发生、目录必然存在；不转发子端、子端不产生任何副作用。
        env_id = _add_env(parent, f"http://127.0.0.1:{child[0].port}/v1/setup")
        sid = "sess-log-remote"
        _make_parent_session_raw(parent, sid, env_id)
        assert not (child[1] / "chat_data" / sid).exists()

        status, body = _request(parent[0], "GET", f"/v1/sessions/{sid}/log-dir")
        assert status == 200, body
        assert body["path"] == str(parent[1] / "chat_data" / sid)
        # 子端无副作用：没有 file journal 就不建目录
        assert not (child[1] / "chat_data" / sid).exists()

    def test_child_log_dir_exists_after_file_journal(self, parent, child):
        # 产生 file journal 后子端会话目录存在，子端 log-dir 直接可解析。
        env_id = _add_env(parent, f"http://127.0.0.1:{child[0].port}/v1/setup")
        sid = "sess-log-j"
        _make_parent_session_raw(parent, sid, env_id)
        _child_write_file(child, sid, "hello.txt", "hi")
        status, body = _request(child[0], "GET", f"/v1/sessions/{sid}/log-dir")
        assert status == 200, body
        assert body["path"] == str(child[1] / "chat_data" / sid)

    def test_remote_bound_log_dir_omits_journal_link_without_child_journal(self, parent, child):
        # 子端无 file journal：父端 log-dir 不带 remote_journal 字段，子端仍无副作用。
        env_id = _add_env(parent, f"http://127.0.0.1:{child[0].port}/v1/setup")
        sid = "sess-log-rj-none"
        _make_parent_session_raw(parent, sid, env_id)
        status, body = _request(parent[0], "GET", f"/v1/sessions/{sid}/log-dir")
        assert status == 200, body
        assert body["path"] == str(parent[1] / "chat_data" / sid)
        assert "remote_journal" not in body
        assert not (child[1] / "chat_data" / sid).exists()

    def test_remote_bound_log_dir_includes_journal_link(self, parent, child):
        # 子端已有 file journal：父端 log-dir 附带 remote_journal 软链接字段。
        env_id = _add_env(parent, f"http://127.0.0.1:{child[0].port}/v1/setup")
        sid = "sess-log-rj-yes"
        _make_parent_session_raw(parent, sid, env_id)
        _child_write_file(child, sid, "hello.txt", "hi")
        status, body = _request(parent[0], "GET", f"/v1/sessions/{sid}/log-dir")
        assert status == 200, body
        assert body["path"] == str(parent[1] / "chat_data" / sid)
        assert body["remote_journal"] == {
            "env_id": env_id,
            "path": str(child[1] / "chat_data" / sid),
        }

    def test_remote_bound_log_dir_link_absent_when_child_offline(self, parent, child):
        # 子端离线：探测失败只是不附字段，主响应仍是本地路径。
        env_id = _add_env(parent, f"http://127.0.0.1:{child[0].port}/v1/setup")
        sid = "sess-log-rj-off"
        _make_parent_session_raw(parent, sid, env_id)
        child[0].stop()  # 停掉子端，探测应快速失败（连接拒绝）
        status, body = _request(parent[0], "GET", f"/v1/sessions/{sid}/log-dir")
        assert status == 200, body
        assert body["path"] == str(parent[1] / "chat_data" / sid)
        assert "remote_journal" not in body
