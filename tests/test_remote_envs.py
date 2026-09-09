"""Tests for remote environment management (远程环境管理).

The parent environment manages child environments from its perspective:
records live in DATA_DIR/remote_envs.json (local-only metadata — never part
of the online-update delta tar or the self-extracting setup payload).

Status checks use the child's /v1/setup?op=hello (queried cross-origin by
the web UI).  hello is authorized like any other /v1/ endpoint: when the
child has auth enabled, the registered URL must carry ?token=....  Updates
use the PUSH model: the parent builds the delta from its own files and
POSTs it to the child's /v1/setup?op=push — the child never needs to know
or reach the parent's address.
"""

from __future__ import annotations

import contextlib
import http.server
import io
import json
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

import pytest

from runtime.auth_manager import AuthManager
from runtime.env_manager import EnvManager
from runtime.handler_api import _platform_arch, _platform_os
from runtime.remote_env_manager import (
    RemoteEnvManager,
    build_setup_request_url,
    canonical_setup_url,
    normalize_setup_url,
    snapshot_from_hello,
)
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------


class TestNormalizeSetupUrl:
    def test_bare_host_url(self):
        n = normalize_setup_url("http://172.28.70.13:7988/")
        assert n["id"] == "http://172.28.70.13:7988"
        assert n["path"] == "/v1/setup"
        assert n["query"] == []

    def test_explicit_setup_path(self):
        n = normalize_setup_url("http://172.28.70.13:7988/v1/setup")
        assert n["id"] == "http://172.28.70.13:7988"
        assert n["path"] == "/v1/setup"

    def test_setup_path_with_token(self):
        n = normalize_setup_url("http://172.28.70.13:7988/v1/setup?token=abc&op=hello")
        assert n["path"] == "/v1/setup"
        # op is per-request (stripped); token is kept for authorization.
        assert n["query"] == [("token", "abc")]

    def test_all_three_forms_share_id(self):
        ids = {
            normalize_setup_url("http://172.28.70.13:7988/")["id"],
            normalize_setup_url("http://172.28.70.13:7988/v1/setup")["id"],
            normalize_setup_url("http://172.28.70.13:7988/v1/setup?token=x")["id"],
        }
        assert ids == {"http://172.28.70.13:7988"}

    def test_https_scheme(self):
        n = normalize_setup_url("https://example.com:8443/")
        assert n["id"] == "https://example.com:8443"
        assert n["scheme"] == "https"
        assert n["netloc"] == "example.com:8443"

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "ftp://host/", "http://", "host:7988/", "http://host:notaport/"],
    )
    def test_invalid_urls(self, raw):
        with pytest.raises(ValueError):
            normalize_setup_url(raw)


class TestCanonicalSetupUrl:
    def test_bare_host(self):
        assert canonical_setup_url("http://172.28.70.13:7988/") == "http://172.28.70.13:7988/v1/setup"

    def test_keeps_token_drops_op(self):
        url = canonical_setup_url("http://172.28.70.13:7988/v1/setup?token=abc&op=hello")
        assert url == "http://172.28.70.13:7988/v1/setup?token=abc"


class TestBuildSetupRequestUrl:
    def test_hello_from_bare_host(self):
        url = build_setup_request_url("http://172.28.70.13:7988/", {"op": "hello"})
        assert url == "http://172.28.70.13:7988/v1/setup?op=hello"

    def test_update_params_keep_token(self):
        url = build_setup_request_url(
            "http://172.28.70.13:7988/v1/setup?token=tok&op=hello",
            {
                "op": "update",
                "source": "http://parent:7988/v1/setup?token=p",
                "frontend_build": "250908_120000",
                "backend_build": "250908_120000",
                "last_config": "250908_120000",
            },
        )
        parsed = urllib.parse.urlsplit(url)
        assert parsed.path == "/v1/setup"
        params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        assert params["token"] == "tok"
        assert params["op"] == "update"
        assert params["source"] == "http://parent:7988/v1/setup?token=p"
        assert params["frontend_build"] == "250908_120000"
        assert params["last_config"] == "250908_120000"


class TestSnapshotFromHello:
    def test_extracts_fields(self):
        snap = snapshot_from_hello({
            "frontend_build": "a",
            "backend_build": "b",
            "last_config": "c",
            "server_instance_id": "sid",
            "inference_active": True,
            "api_inference_active": False,
            "session_inference_active": True,
            "unrelated": "ignored",
            "app_title": "Demo",
            "app_logo": "/logo.png",
            "arch": "aarch64",
            "os": "macOS",
        })
        assert snap == {
            "frontend_build": "a",
            "backend_build": "b",
            "last_config": "c",
            "server_instance_id": "sid",
            "inference_active": True,
            "api_inference_active": False,
            "session_inference_active": True,
            "app_title": "Demo",
            "app_logo": "/logo.png",
            "arch": "aarch64",
            "os": "macOS",
        }

    def test_none_safe(self):
        snap = snapshot_from_hello(None)
        assert snap["frontend_build"] == ""
        assert snap["inference_active"] is False
        assert snap["api_inference_active"] is False
        assert snap["session_inference_active"] is False
        assert snap["app_title"] == ""
        assert snap["os"] == ""


# ---------------------------------------------------------------------------
# op=hello platform info normalization
# ---------------------------------------------------------------------------


class TestPlatformInfo:
    @pytest.mark.parametrize(
        ("machine", "expected"),
        [
            ("x86_64", "x86_64"),
            ("AMD64", "x86_64"),
            ("aarch64", "aarch64"),
            ("arm64", "aarch64"),
            ("riscv64", "riscv64"),
            ("", "unknown"),
        ],
    )
    def test_arch_normalization(self, machine, expected):
        with patch("runtime.handler_api.platform.machine", return_value=machine):
            assert _platform_arch() == expected

    @pytest.mark.parametrize(
        ("system", "expected"),
        [
            ("Linux", "linux"),
            ("Windows", "windows"),
            ("Darwin", "macOS"),
            ("FreeBSD", "FreeBSD"),
            ("", "unknown"),
        ],
    )
    def test_os_normalization(self, system, expected):
        with patch("runtime.handler_api.platform.system", return_value=system):
            assert _platform_os() == expected


# ---------------------------------------------------------------------------
# RemoteEnvManager
# ---------------------------------------------------------------------------


class TestRemoteEnvManager:
    def _manager(self, tmp_path):
        return RemoteEnvManager(str(tmp_path / "remote_envs.json"))

    def test_read_missing_file(self, tmp_path):
        assert self._manager(tmp_path).read() == []

    def test_upsert_and_persist(self, tmp_path):
        manager = self._manager(tmp_path)
        envs = manager.upsert(
            "http://10.0.0.5:7988/",
            {"frontend_build": "v1", "inference_active": True,
             "app_title": "Demo", "app_logo": "🤖", "arch": "x86_64", "os": "linux"},
        )
        assert len(envs) == 1
        record = envs[0]
        assert record["id"] == "http://10.0.0.5:7988"
        assert record["url"] == "http://10.0.0.5:7988/v1/setup"
        assert record["frontend_build"] == "v1"
        assert record["inference_active"] is True
        # App / platform snapshot fields are persisted too.
        assert record["app_title"] == "Demo"
        assert record["app_logo"] == "🤖"
        assert record["arch"] == "x86_64"
        assert record["os"] == "linux"
        assert record["checked_at"]
        on_disk = json.loads((tmp_path / "remote_envs.json").read_text(encoding="utf-8"))
        assert on_disk == envs

    def test_upsert_dedupes_by_host(self, tmp_path):
        manager = self._manager(tmp_path)
        manager.upsert("http://10.0.0.5:7988/v1/setup?token=a", {})
        envs = manager.upsert("http://10.0.0.5:7988/", {"frontend_build": "v2"})
        assert len(envs) == 1
        assert envs[0]["url"] == "http://10.0.0.5:7988/v1/setup"
        assert envs[0]["frontend_build"] == "v2"

    def test_update_snapshot(self, tmp_path):
        manager = self._manager(tmp_path)
        manager.upsert("http://10.0.0.5:7988/", None)
        envs = manager.update_snapshot(
            "http://10.0.0.5:7988",
            {"backend_build": "b2", "inference_active": True},
        )
        assert envs[0]["backend_build"] == "b2"
        assert envs[0]["inference_active"] is True
        assert envs[0]["checked_at"]
        with pytest.raises(KeyError):
            manager.update_snapshot("http://10.9.9.9:1", {"backend_build": "x"})

    def test_remove(self, tmp_path):
        manager = self._manager(tmp_path)
        manager.upsert("http://10.0.0.5:7988/", None)
        assert manager.remove("http://10.0.0.5:7988") == []
        with pytest.raises(KeyError):
            manager.remove("http://10.0.0.5:7988")

    def test_corrupt_file_degrades_to_empty(self, tmp_path):
        (tmp_path / "remote_envs.json").write_text("not json", encoding="utf-8")
        assert self._manager(tmp_path).read() == []
        # Subsequent writes still work (the list is rebuilt from this record).
        envs = self._manager(tmp_path).upsert("http://10.0.0.6:7988/", None)
        assert len(envs) == 1

    def test_non_list_file_degrades_to_empty(self, tmp_path):
        (tmp_path / "remote_envs.json").write_text("{}", encoding="utf-8")
        assert self._manager(tmp_path).read() == []

    def test_legacy_object_format_still_readable(self, tmp_path):
        # Older versions wrote {"envs": [...], "source_url": ...}; the
        # source_url field must simply be ignored.
        (tmp_path / "remote_envs.json").write_text(json.dumps({
            "envs": [
                {
                    "id": "http://10.0.0.5:7988",
                    "url": "http://10.0.0.5:7988/v1/setup",
                    "created_at": "2025-09-08T15:30:00",
                }
            ],
            "source_url": "http://10.1.1.1:7988/v1/setup",
        }), encoding="utf-8")
        envs = self._manager(tmp_path).read()
        assert len(envs) == 1
        assert envs[0]["id"] == "http://10.0.0.5:7988"


# ---------------------------------------------------------------------------
# remote_envs.json stays out of updates and exports
# ---------------------------------------------------------------------------


class TestNotExported:
    def test_delta_tar_excludes_remote_envs(self, tmp_path):
        project_root = tmp_path / "project"
        data_dir = tmp_path / "agents_runtime"
        (project_root / "web").mkdir(parents=True)
        data_dir.mkdir()
        (data_dir / "remote_envs.json").write_text("[{}]", encoding="utf-8")
        # A whitelisted config file so the delta is non-empty.
        (data_dir / "models.json").write_text("[]", encoding="utf-8")
        manager = EnvManager(env_path=str(data_dir / "env.json"))
        tar_bytes = manager.build_delta_tar(
            project_root=str(project_root),
            data_dir=str(data_dir),
            frontend_since=0,
            backend_since=0,
            config_since=0,
        )
        assert tar_bytes is not None
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
            names = [m.name for m in tar.getmembers()]
        assert "agents_runtime/models.json" in names
        assert "agents_runtime/remote_envs.json" not in names

    def test_setup_payload_excludes_remote_envs(self, tmp_path):
        project_root = tmp_path / "project"
        data_dir = tmp_path / "agents_runtime"
        project_root.mkdir()
        data_dir.mkdir()
        (data_dir / "remote_envs.json").write_text("[{}]", encoding="utf-8")
        manager = EnvManager(env_path=str(data_dir / "env.json"))
        payload = manager._build_setup_payload(
            project_root=str(project_root),
            data_dir=str(data_dir),
            runtime=None,
            prompt_template_manager=None,
            agent_manager=None,
            include_project=False,
            include_env=True,  # even when env.json itself is exported
        )
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
            names = [m.name for m in tar.getmembers()]
        assert any("agents_runtime/env.json" in n for n in names)
        assert not any("remote_envs.json" in n for n in names)


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


@pytest.fixture()
def runtime():
    return Runtime(ModelRegistry(), ToolRegistry())


@pytest.fixture()
def server(runtime, tmp_path):
    with patch("runtime.server._MODELS_PATH", str(tmp_path / "models.json")), \
         patch("runtime.server._TOOLS_PATH", str(tmp_path / "tools.json")), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", str(tmp_path / "prompt_templates.json")), \
         patch("runtime.server._DATA_DIR", str(tmp_path)), \
         patch("runtime.server._ENV_PATH", str(tmp_path / "env.json")), \
         patch("runtime.server._REMOTE_ENVS_PATH", str(tmp_path / "remote_envs.json")), \
         patch("runtime.server._AUTH_PATH", str(tmp_path / "auth_token.json")), \
         patch("runtime.server._AGENTS_DIR", str(tmp_path / "agents")):
        srv = RuntimeHTTPServer(runtime)
        srv.start_background(host="127.0.0.1", port=0)
        yield srv
        srv.stop()


def _request(server, method, path, payload=None, headers=None):
    url = f"http://127.0.0.1:{server.port}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(
        url, data=data, headers=req_headers, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _request_raw(server, method, path, raw, headers=None):
    """Like _request but sends a raw (non-JSON) body, e.g. a tar.gz delta."""
    url = f"http://127.0.0.1:{server.port}{path}"
    req_headers = {"Content-Type": "application/gzip"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(
        url, data=raw, headers=req_headers, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _make_tar_bytes(files):
    """Build a tar.gz of {arcname: text content} in memory."""
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tar:
        for arcname, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return bio.getvalue()


class TestRemoteEnvsEndpoints:
    def test_list_empty(self, server):
        status, body = _request(server, "GET", "/v1/remote-envs")
        assert status == 200
        assert body["envs"] == []

    def test_add_requires_url(self, server):
        status, body = _request(server, "POST", "/v1/remote-envs", {})
        assert status == 400
        assert "url" in body["error"]

    def test_add_rejects_invalid_url(self, server):
        status, body = _request(server, "POST", "/v1/remote-envs", {"url": "nope"})
        assert status == 400
        assert body["error"]

    def test_add_persists_with_snapshot(self, server, tmp_path):
        status, body = _request(server, "POST", "/v1/remote-envs", {
            "url": "http://172.28.70.13:7988/v1/setup?token=tok&op=hello",
            "snapshot": {
                "frontend_build": "250908_120000",
                "backend_build": "250908_120000",
                "last_config": "",
                "inference_active": True,
            },
        })
        assert status == 200
        envs = body["envs"]
        assert len(envs) == 1
        assert envs[0]["id"] == "http://172.28.70.13:7988"
        # The stored URL keeps the token (and drops the per-request op).
        assert envs[0]["url"] == "http://172.28.70.13:7988/v1/setup?token=tok"
        assert envs[0]["frontend_build"] == "250908_120000"
        assert envs[0]["inference_active"] is True
        on_disk = json.loads((tmp_path / "remote_envs.json").read_text(encoding="utf-8"))
        assert on_disk == envs

    def test_hello_reports_app_metadata_and_platform(self, server):
        # hello carries the environment's app identity (env.json) and the
        # platform it runs on, for the parent's remote-environment list.
        server._env_manager.set("APP_TITLE", "Demo App")
        server._env_manager.set("APP_LOGO", "/logo.png")
        status, body = _request(server, "GET", "/v1/setup?op=hello")
        assert status == 200
        assert body["app_title"] == "Demo App"
        assert body["app_logo"] == "/logo.png"
        assert isinstance(body["arch"], str) and body["arch"]
        assert isinstance(body["os"], str) and body["os"]

    def test_hello_app_metadata_defaults_to_empty(self, server):
        status, body = _request(server, "GET", "/v1/setup?op=hello")
        assert status == 200
        assert body["app_title"] == ""
        assert body["app_logo"] == ""

    def test_hello_survives_broken_env_json(self, server, tmp_path):
        # hello is a lightweight probe: a corrupt env.json must not break it.
        (tmp_path / "env.json").write_text("not json", encoding="utf-8")
        status, body = _request(server, "GET", "/v1/setup?op=hello")
        assert status == 200
        assert body["app_title"] == ""
        assert body["app_logo"] == ""
        assert body["arch"] and body["os"]

    def test_add_upserts_same_host(self, server):
        _request(server, "POST", "/v1/remote-envs", {"url": "http://10.0.0.5:7988/"})
        status, body = _request(server, "POST", "/v1/remote-envs", {
            "url": "http://10.0.0.5:7988/v1/setup?token=x",
            "snapshot": {"frontend_build": "v2"},
        })
        assert status == 200
        assert len(body["envs"]) == 1
        assert body["envs"][0]["frontend_build"] == "v2"

    def test_update_snapshot(self, server):
        _request(server, "POST", "/v1/remote-envs", {"url": "http://10.0.0.5:7988/"})
        status, body = _request(
            server, "PUT", "/v1/remote-envs/http://10.0.0.5:7988",
            {"snapshot": {"backend_build": "b9", "inference_active": False}},
        )
        assert status == 200
        assert body["envs"][0]["backend_build"] == "b9"
        assert body["envs"][0]["inference_active"] is False

    def test_update_snapshot_requires_body(self, server):
        _request(server, "POST", "/v1/remote-envs", {"url": "http://10.0.0.5:7988/"})
        status, _ = _request(server, "PUT", "/v1/remote-envs/http://10.0.0.5:7988", {})
        assert status == 400

    def test_update_snapshot_missing(self, server):
        status, _ = _request(
            server, "PUT", "/v1/remote-envs/http://10.9.9.9:1",
            {"snapshot": {"backend_build": "x"}},
        )
        assert status == 404

    def test_delete(self, server):
        _request(server, "POST", "/v1/remote-envs", {"url": "http://10.0.0.5:7988/"})
        status, body = _request(server, "DELETE", "/v1/remote-envs/http://10.0.0.5:7988")
        assert status == 200
        assert body == {"envs": []}
        status, _ = _request(server, "DELETE", "/v1/remote-envs/http://10.0.0.5:7988")
        assert status == 404


# ---------------------------------------------------------------------------
# op=update: source falls back to the local env.json SETUP_SOURCE
# ---------------------------------------------------------------------------


class TestSetupUpdateSourceFallback:
    def test_falls_back_to_env_json_setup_source(self, server, tmp_path):
        # Record SETUP_SOURCE (pointing at this server itself) in env.json.
        (tmp_path / "env.json").write_text(
            json.dumps({"SETUP_SOURCE": f"http://127.0.0.1:{server.port}/v1/setup"}),
            encoding="utf-8",
        )
        # Current versions as delta baselines: a self-delta is empty at most,
        # so no files change and no backend restart can be triggered.
        status, hello = _request(server, "GET", "/v1/setup?op=hello")
        assert status == 200
        query = urllib.parse.urlencode({
            "op": "update",
            "frontend_build": hello["frontend_build"],
            "backend_build": hello["backend_build"],
            "last_config": hello["last_config"],
        })
        status, body = _request(server, "GET", f"/v1/setup?{query}")
        assert status == 200
        assert body["restart_backend"] is False
        assert body["updated"] in (True, False)

    def test_missing_source_and_no_setup_source(self, server):
        query = urllib.parse.urlencode({
            "op": "update",
            "frontend_build": "0",
            "backend_build": "0",
            "last_config": "0",
        })
        status, body = _request(server, "GET", f"/v1/setup?{query}")
        assert status == 400
        assert "source" in body["error"]


# ---------------------------------------------------------------------------
# Update source resolution (the address child environments pull updates from)
# ---------------------------------------------------------------------------


def write_env_json(data):
    """Write env.json at the patched _ENV_PATH (per-test tmp dir)."""
    import runtime.server as rs
    with open(rs._ENV_PATH, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data))


# ---------------------------------------------------------------------------
# op=push: applying a parent-pushed delta (child side)
# ---------------------------------------------------------------------------


class TestSetupPushOp:
    def test_requires_post(self, server):
        status, _ = _request(server, "GET", "/v1/setup?op=push")
        assert status == 405

    def test_rejects_empty_body(self, server):
        status, body = _request_raw(server, "POST", "/v1/setup?op=push", b"")
        assert status == 400
        assert body["error"]

    def test_rejects_downgrade(self, server):
        # Target versions older than what the local environment runs.
        tar = _make_tar_bytes({"agents_runtime/models.json": "[]"})
        status, body = _request_raw(
            server, "POST",
            "/v1/setup?op=push&frontend_build=250101_000000"
            "&backend_build=250101_000000&last_config=250101_000000",
            tar,
        )
        assert status == 400
        assert "older than local" in body["error"]

    def test_rejects_invalid_target_version(self, server):
        tar = _make_tar_bytes({"agents_runtime/models.json": "[]"})
        status, body = _request_raw(
            server, "POST",
            "/v1/setup?op=push&frontend_build=not_a_version",
            tar,
        )
        assert status == 400
        assert "frontend_build" in body["error"]

    def test_applies_config_only_delta(self, server, tmp_path):
        # Baselines = the server's own current versions (no project files in
        # the delta), plus a config member: only the config file is applied.
        status, hello = _request(server, "GET", "/v1/setup?op=hello")
        assert status == 200
        tar = _make_tar_bytes({"agents_runtime/models.json": "[]"})
        status, body = _request_raw(
            server, "POST",
            "/v1/setup?op=push&frontend_build={f}&backend_build={b}&last_config={c}".format(
                f=hello["frontend_build"], b=hello["backend_build"], c=hello["last_config"],
            ),
            tar,
        )
        assert status == 200
        assert body["updated"] is True
        assert body["restart_backend"] is False
        assert body["updated_files"] == ["agents_runtime/models.json"]
        written = (tmp_path / "models.json").read_text(encoding="utf-8")
        assert written == "[]"

    def test_rejects_oversized_body(self, server):
        tar = _make_tar_bytes({"agents_runtime/models.json": "[]"})
        with patch("runtime.handler_api._MAX_PUSH_BODY_BYTES", 16):
            status, body = _request_raw(
                server, "POST", "/v1/setup?op=push", tar,
            )
        assert status == 413

    def test_requires_setup_token_when_auth_enabled(self, server):
        # Enable authorization (first-run mode is open to /v1/auth/config).
        status, config = _request(
            server, "POST", "/v1/auth/config", {"password": "test-pass"},
        )
        assert status == 200
        token = config.get("setup_token", "")
        assert token

        tar = _make_tar_bytes({"agents_runtime/models.json": "[]"})
        # No token -> unauthorized (before the op is even dispatched).
        status, _ = _request_raw(server, "POST", "/v1/setup?op=push", tar)
        assert status == 401
        # Wrong token -> unauthorized.
        status, _ = _request_raw(
            server, "POST", "/v1/setup?op=push&token=bad", tar,
        )
        assert status == 401
        # Valid token -> auth passes; the downgrade guard then rejects the
        # deliberately old target versions.
        status, body = _request_raw(
            server, "POST",
            f"/v1/setup?op=push&token={token}&frontend_build=250101_000000",
            tar,
        )
        assert status == 400
        assert "older than local" in body["error"]

    def test_accepts_api_key_via_token_param(self, server):
        # Persistent setup links (remote env management, SETUP_SOURCE for
        # online updates) may carry the long-lived as_ API key instead of
        # the one-hour st_ setup token.
        status, config = _request(
            server, "POST", "/v1/auth/config", {"password": "test-pass"},
        )
        assert status == 200
        api_key = config.get("api_key", "")
        assert api_key.startswith("as_")

        # op=hello is authorized via the token query param.
        status, body = _request(server, "GET", f"/v1/setup?op=hello&token={api_key}")
        assert status == 200
        assert "frontend_build" in body

        # op=push is authorized the same way; the downgrade guard then
        # rejects the deliberately old target versions.
        tar = _make_tar_bytes({"agents_runtime/models.json": "[]"})
        status, body = _request_raw(
            server,
            "POST",
            f"/v1/setup?op=push&token={api_key}&frontend_build=250101_000000",
            tar,
        )
        assert status == 400
        assert "older than local" in body["error"]

    def test_rejects_api_key_without_as_prefix(self, server):
        # A garbage as_-prefixed token must not authenticate.
        status, _config = _request(
            server, "POST", "/v1/auth/config", {"password": "test-pass"},
        )
        assert status == 200
        status, _ = _request(server, "GET", "/v1/setup?op=hello&token=as_bogus")
        assert status == 401

    def test_hello_requires_auth_when_auth_enabled(self, server):
        # op=hello is no longer publicly exposed: it follows the same
        # authorization as every other /v1/ endpoint.
        status, config = _request(
            server, "POST", "/v1/auth/config", {"password": "test-pass"},
        )
        assert status == 200
        token = config.get("setup_token", "")
        assert token
        # No token -> 401.
        status, _ = _request(server, "GET", "/v1/setup?op=hello")
        assert status == 401
        # Wrong token -> 401.
        status, _ = _request(server, "GET", "/v1/setup?op=hello&token=bad")
        assert status == 401
        # Valid setup token -> 200 with version and inference info.
        status, body = _request(server, "GET", f"/v1/setup?op=hello&token={token}")
        assert status == 200
        assert "frontend_build" in body
        assert "inference_active" in body

    def test_rejects_legacy_json_setup_token(self, server):
        # The pre-compact signed-JSON format (payload.scope == "setup") is no
        # longer accepted: only the compact st_ format (or an as_ API key via
        # the token param) works now.
        status, _config = _request(
            server, "POST", "/v1/auth/config", {"password": "test-pass"},
        )
        assert status == 200
        auth_manager = server._server.auth_manager  # type: ignore[attr-defined]
        legacy = AuthManager._sign_payload(
            {"v": 1, "scope": "setup", "exp": int(time.time()) + 3600},
            auth_manager._setup_secret,
        )
        assert "." in legacy  # sanity: this is the old signed-JSON shape
        status, _ = _request(server, "GET", f"/v1/setup?op=hello&token={legacy}")
        assert status == 401


# ---------------------------------------------------------------------------
# push-update: parent pushes a delta to a child
# (POST /v1/remote-envs/{id}/push-update)
# ---------------------------------------------------------------------------


class _FakeChildServer:
    """Minimal stand-in for an (older) child environment.

    Serves canned responses and records every request so tests can assert
    which protocol the parent used (push vs. pull fallback).
    """

    def __init__(self, hello, push_response, update_response):
        self.hello = hello
        self.push_response = push_response
        self.update_response = update_response
        self.push_bodies = []
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
                outer.requests.append(("GET", parsed.path, params))
                if params.get("op") == "hello":
                    self._send(200, outer.hello)
                elif params.get("op") == "update":
                    self._send(*outer.update_response)
                else:
                    self._send(400, {"error": f"Unsupported setup op: {params.get('op', '')}"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b""
                parsed = urllib.parse.urlparse(self.path)
                params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
                outer.requests.append(("POST", parsed.path, params, body))
                if params.get("op") == "push":
                    outer.push_bodies.append(body)
                    self._send(*outer.push_response)
                else:
                    self._send(400, {"error": f"Unsupported setup op: {params.get('op', '')}"})

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self):
        return self._httpd.server_address[1]

    def url(self):
        return f"http://127.0.0.1:{self.port}/v1/setup"

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()


def _add_env(server, url):
    status, body = _request(server, "POST", "/v1/remote-envs", {"url": url})
    assert status == 200
    assert len(body["envs"]) == 1
    return body["envs"][0]["id"]


@contextlib.contextmanager
def _real_child_server(tmp_path, name="child_data"):
    """Start a second real RuntimeHTTPServer (isolated data dir) as the child."""
    child_data = tmp_path / name
    child_data.mkdir()
    with patch("runtime.server._MODELS_PATH", str(child_data / "models.json")), \
         patch("runtime.server._TOOLS_PATH", str(child_data / "tools.json")), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", str(child_data / "prompt_templates.json")), \
         patch("runtime.server._DATA_DIR", str(child_data)), \
         patch("runtime.server._ENV_PATH", str(child_data / "env.json")), \
         patch("runtime.server._REMOTE_ENVS_PATH", str(child_data / "remote_envs.json")), \
         patch("runtime.server._AUTH_PATH", str(child_data / "auth_token.json")), \
         patch("runtime.server._AGENTS_DIR", str(child_data / "agents")):
        child = RuntimeHTTPServer(Runtime(ModelRegistry(), ToolRegistry()))
        child.start_background(host="127.0.0.1", port=0)
    try:
        yield child, child_data
    finally:
        child.stop()


def _wait_child_hello(child):
    """Wait until the child answers op=hello; return its version snapshot."""
    for _ in range(50):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{child.port}/v1/setup?op=hello", timeout=1
            ) as resp:
                return json.loads(resp.read())
        except OSError:
            time.sleep(0.1)
    raise AssertionError("child server never came up")


class TestRemoteEnvPushUpdate:
    def test_unknown_env(self, server):
        status, _ = _request(server, "POST", "/v1/remote-envs/http://10.9.9.9:1/push-update")
        assert status == 404

    def test_up_to_date_child_is_not_pushed(self, server):
        status, hello = _request(server, "GET", "/v1/setup?op=hello")
        assert status == 200
        child = _FakeChildServer(
            hello={
                "frontend_build": hello["frontend_build"],
                "backend_build": hello["backend_build"],
                "last_config": hello["last_config"],
            },
            push_response=(200, {"updated": True}),
            update_response=(200, {"updated": True}),
        )
        try:
            env_id = _add_env(server, child.url())
            status, result = _request(
                server, "POST", f"/v1/remote-envs/{env_id}/push-update",
            )
            assert status == 200
            assert result["updated"] is False
            assert result["reason"] == "up-to-date"
            assert result["method"] == "push"
            # Nothing was pushed or pulled: the only traffic was the hello probe.
            assert child.push_bodies == []
            assert all(r[2].get("op") == "hello" for r in child.requests)
        finally:
            child.stop()

    def test_pushes_delta_to_real_child(self, server, tmp_path):
        # A real second server as the child: same code, empty data dir.  Its
        # last_config is empty while the parent has a fresh config file, so
        # the delta carries the config only (no .py -> no backend restart).
        with _real_child_server(tmp_path) as (child, child_data):
            child_hello = _wait_child_hello(child)
            assert child_hello["last_config"] == ""

            # Fresh config on the parent side -> the delta is non-empty.
            (tmp_path / "models.json").write_text("[]", encoding="utf-8")

            env_id = _add_env(server, f"http://127.0.0.1:{child.port}/v1/setup")
            status, result = _request(
                server, "POST", f"/v1/remote-envs/{env_id}/push-update",
            )
            assert status == 200, result
            assert result["ok"] is True
            assert result["updated"] is True
            assert result["restart_backend"] is False
            assert result["method"] == "push"
            assert "agents_runtime/models.json" in result["updated_files"]

            # The child's hello reports app metadata + platform, and the
            # parent propagated them into the returned remote snapshot.
            assert child_hello["arch"] and child_hello["os"]
            assert result["remote"]["arch"] == child_hello["arch"]
            assert result["remote"]["os"] == child_hello["os"]
            assert result["remote"]["app_title"] == child_hello["app_title"]

            # The child really received and applied the config file.
            assert (child_data / "models.json").read_text(encoding="utf-8") == "[]"
            after = _wait_child_hello(child)
            assert after["last_config"] == result["local"]["last_config"]

            # A second push is a no-op (nothing newer any more).
            status, result = _request(
                server, "POST", f"/v1/remote-envs/{env_id}/push-update",
            )
            assert status == 200
            assert result["updated"] is False
            assert result["reason"] == "up-to-date"

    def test_pushes_to_auth_enabled_child(self, server, tmp_path):
        # The child has authorization enabled: the registered URL must carry
        # the child's setup token (?token=...) so both the hello probe and
        # the push get through the child's auth gate.
        with _real_child_server(tmp_path) as (child, child_data):
            _wait_child_hello(child)
            status, config = _request(
                child, "POST", "/v1/auth/config", {"password": "child-pass"},
            )
            assert status == 200
            token = config.get("setup_token", "")
            assert token
            # hello is no longer public: rejected without a token,
            # answered with the setup token.
            status, _ = _request(child, "GET", "/v1/setup?op=hello")
            assert status == 401
            status, body = _request(child, "GET", f"/v1/setup?op=hello&token={token}")
            assert status == 200
            assert "frontend_build" in body

            (tmp_path / "models.json").write_text("[]", encoding="utf-8")
            env_id = _add_env(
                server, f"http://127.0.0.1:{child.port}/v1/setup?token={token}",
            )
            status, result = _request(
                server, "POST", f"/v1/remote-envs/{env_id}/push-update",
            )
            assert status == 200, result
            assert result["updated"] is True
            assert result["method"] == "push"
            assert (child_data / "models.json").read_text(encoding="utf-8") == "[]"

    def test_auth_enabled_child_without_token_reports_child_auth(self, server, tmp_path):
        # The child has auth enabled but the parent registered a bare URL
        # (no ?token=...): the hello probe is rejected and the parent reports
        # child_auth (not child_unreachable), so the user knows to re-register
        # the environment with the full setup link.
        with _real_child_server(tmp_path, name="child_data_auth") as (child, _child_data):
            _wait_child_hello(child)
            status, config = _request(
                child, "POST", "/v1/auth/config", {"password": "child-pass"},
            )
            assert status == 200
            assert config.get("setup_token", "")

            env_id = _add_env(server, f"http://127.0.0.1:{child.port}/v1/setup")
            status, result = _request(
                server, "POST", f"/v1/remote-envs/{env_id}/push-update",
            )
            assert status == 400
            assert result["error"] == "child_auth"

    def test_old_child_reports_push_not_supported(self, server):
        # Older build: op=push is unknown -> the parent reports that the
        # child must be upgraded first (there is no pull fallback).
        child = _FakeChildServer(
            hello={"frontend_build": "250101_000000", "backend_build": "250101_000000"},
            push_response=(400, {"error": "Unsupported setup op: push"}),
            update_response=(200, {"updated": True, "restart_backend": False}),
        )
        try:
            env_id = _add_env(server, child.url())
            status, result = _request(
                server, "POST", f"/v1/remote-envs/{env_id}/push-update",
            )
            assert status == 400
            assert result["error"] == "push_not_supported"
            # A real delta was pushed before the child answered...
            assert len(child.push_bodies) == 1
            assert child.push_bodies[0]
            # ...and the parent did NOT fall back to op=update.
            assert not any(r[2].get("op") == "update" for r in child.requests)
        finally:
            child.stop()

    def test_child_unreachable(self, server):
        env_id = _add_env(server, "http://127.0.0.1:1/v1/setup")
        status, result = _request(
            server, "POST", f"/v1/remote-envs/{env_id}/push-update",
        )
        assert status == 502
        assert result["error"] == "child_unreachable"

    def test_child_auth_rejected(self, server):
        child = _FakeChildServer(
            hello={"frontend_build": "250101_000000", "backend_build": "250101_000000"},
            push_response=(401, {"error": "unauthorized", "message": "Authentication required"}),
            update_response=(200, {}),
        )
        try:
            env_id = _add_env(server, child.url())
            status, result = _request(
                server, "POST", f"/v1/remote-envs/{env_id}/push-update",
            )
            assert status == 400
            assert result["error"] == "child_auth"
            # An auth failure must not be retried through op=update either.
            assert not any(r[2].get("op") == "update" for r in child.requests)
        finally:
            child.stop()

    def test_child_busy_passthrough(self, server):
        child = _FakeChildServer(
            hello={"frontend_build": "250101_000000", "backend_build": "250101_000000"},
            push_response=(409, {
                "error": "inference_active",
                "message": "Cannot update while inference sessions are active",
            }),
            update_response=(200, {}),
        )
        try:
            env_id = _add_env(server, child.url())
            status, result = _request(
                server, "POST", f"/v1/remote-envs/{env_id}/push-update",
            )
            assert status == 409
            assert result["error"] == "inference_active"
            assert not any(r[2].get("op") == "update" for r in child.requests)
        finally:
            child.stop()
