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

import base64
import contextlib
import datetime
import http.server
import io
import json
import os
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

import pytest

from runtime.auth_manager import AuthManager
from runtime.env_manager import (
    EnvManager,
    invalidate_backend_mtime_cache,
    local_tz_offset_minutes,
)
import runtime.handler_api as handler_api_module
from runtime.handler_api import _platform_arch, _platform_os
from runtime.remote_env_manager import (
    RemoteEnvManager,
    SECONDARY_TOKEN_HEADER,
    attach_url_credentials,
    auth_headers,
    basic_auth_header,
    build_setup_request_url,
    canonical_setup_url,
    hello_tz_offset_minutes,
    mask_url_credentials,
    normalize_setup_url,
    restamp_to_local,
    snapshot_from_hello,
    strip_url_credentials,
    url_credentials,
)
from runtime.remote_tool_proxy import child_endpoint
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer
from runtime import wsutil
# Browser-side WebSocket handshake + multiplexed-channel helpers (the same
# ones the tunnel tests use): the direct-child bridge must behave exactly like
# the tunnel one, so it is driven by the same client code.
from tests.test_tunnel import _ws_browser_connect, _ws_channel_echo


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


class TestCredentialBearingSetupUrl:
    """第四种 SETUP_SOURCE / 环境 URL 形态：``https://user:password@host:port/``。

    这个凭据既可能是环境前面那道 Basic 认证网关的，也可能是环境自身的访问
    密码（母环境 ``/v1/`` 也接受它）。要点是 userinfo **不能留在 netloc**：
    ``http.client`` 会把 ``user:pass@host`` 整体当主机名去解析（连不上，且
    错误信息里看不出原因），拨号前必须换成 ``Authorization: Basic``。
    """

    URL = "https://gw:secret@proxy.example.com:27988/"

    def test_userinfo_is_split_off_the_host(self):
        n = normalize_setup_url(self.URL)
        assert n["id"] == "https://proxy.example.com:27988"
        assert n["netloc"] == "proxy.example.com:27988"
        assert n["username"] == "gw"
        assert n["password"] == "secret"
        assert n["userinfo"] == "gw:secret@"

    def test_credentials_never_reach_the_record_id(self):
        # 记录 id 是展示标识，也是 remote_envs.json 的键：密码不进 id，带凭据
        # 与不带的写法因此归并成同一条记录。
        bare = normalize_setup_url("https://proxy.example.com:27988/")
        assert normalize_setup_url(self.URL)["id"] == bare["id"]
        assert "secret" not in bare["id"]

    def test_canonical_and_request_urls_keep_the_credential(self):
        assert canonical_setup_url(self.URL) == \
            "https://gw:secret@proxy.example.com:27988/v1/setup"
        assert build_setup_request_url(self.URL, {"op": "hello"}) == \
            "https://gw:secret@proxy.example.com:27988/v1/setup?op=hello"

    def test_percent_encoded_password_round_trip(self):
        # 密码里的 @ : / 用百分号编码写，解析回原值；重新拼回的规范 URL 仍可解析
        n = normalize_setup_url("https://user%40name:p%40ss%3Ax@host:7988/")
        assert (n["username"], n["password"]) == ("user@name", "p@ss:x")
        again = normalize_setup_url(canonical_setup_url("https://user%40name:p%40ss%3Ax@host:7988/"))
        assert (again["username"], again["password"]) == ("user@name", "p@ss:x")

    def test_basic_header_is_the_url_credential(self):
        assert basic_auth_header("gw", "secret") == "Basic Z3c6c2VjcmV0"
        user, password, auth = url_credentials(self.URL)
        assert (user, password) == ("gw", "secret")
        assert auth == "Basic Z3c6c2VjcmV0"
        assert url_credentials("http://host:7988/") == ("", "", "")

    def test_attach_moves_credential_into_the_header(self):
        url, headers = attach_url_credentials(build_setup_request_url(self.URL, {"op": "hello"}))
        assert url == "https://proxy.example.com:27988/v1/setup?op=hello"
        assert headers == {"Authorization": "Basic Z3c6c2VjcmV0"}
        assert strip_url_credentials(self.URL) == "https://proxy.example.com:27988/"

    def test_attach_keeps_an_explicit_authorization(self):
        # 一次调用只能有一种认证；已显式给了 header 时不再从 URL 合成
        url, headers = attach_url_credentials(self.URL, {"Authorization": "Bearer as_x"})
        assert url == "https://proxy.example.com:27988/"
        assert headers == {"Authorization": "Bearer as_x"}

    def test_attach_is_a_no_op_without_credential(self):
        url, headers = attach_url_credentials("http://host:7988/v1/setup", {"Accept": "application/json"})
        assert url == "http://host:7988/v1/setup"
        assert headers == {"Accept": "application/json"}

    def test_auth_headers_splits_two_credentials(self):
        # 只有一种凭据时就是普通的 Authorization；两种都有时凭据占 Authorization
        # （它是过网关的那一把），token 走自己的头，不进 URL
        assert auth_headers("st_x") == {"Authorization": "Bearer st_x"}
        assert auth_headers("", "Basic dXNlcjpwdw==") == {"Authorization": "Basic dXNlcjpwdw=="}
        assert auth_headers("st_x", "Basic dXNlcjpwdw==") == {
            "Authorization": "Basic dXNlcjpwdw==",
            SECONDARY_TOKEN_HEADER: "st_x",
        }
        assert auth_headers() == {}

    def test_ipv6_literal_keeps_its_brackets(self):
        assert normalize_setup_url("http://user:pw@[::1]:7988/")["id"] == "http://[::1]:7988"

    def test_userinfo_without_host_is_rejected(self):
        with pytest.raises(ValueError):
            normalize_setup_url("https://user:pw@")

    def test_mask_hides_the_password(self):
        # 界面/状态里出现的地址都是掩码版
        assert mask_url_credentials(self.URL) == "https://gw:***@proxy.example.com:27988/"
        assert mask_url_credentials("http://user@host:7988/") == "http://user@host:7988/"
        assert mask_url_credentials("http://host:7988/") == "http://host:7988/"

    def test_child_endpoint_splits_credential_from_dial_base(self):
        # 工具代理 / env-proxy 桥：base 必须可建连，凭据单独作为 header 值
        base, token, basic = child_endpoint({
            "url": "http://gw:pw@127.0.0.1:7988/sub/v1/setup?token=st_x",
        })
        assert base == "http://127.0.0.1:7988/sub"
        assert token == "st_x"
        assert basic == "Basic " + base64.b64encode(b"gw:pw").decode()
        base, token, basic = child_endpoint({"url": "http://user:pw@127.0.0.1:7988/v1/setup"})
        assert (base, token, basic) == ("http://127.0.0.1:7988", "", "Basic " + base64.b64encode(b"user:pw").decode())
        assert child_endpoint({"url": "http://127.0.0.1:7988/v1/setup"})[2] == ""


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
            "workspace": "/srv/agent",
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
            "workspace": "/srv/agent",
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
# Build stamps carry the timezone they were taken in
#
# frontend_build / backend_build / last_config are wall-clock strings
# (YYMMdd_HHmmss) formatted in each environment's OWN zone.  Compared as text
# across zones, a UTC child is permanently eight hours "older" to a UTC+8
# parent: every push looks needed, the child's advertised version never moves,
# and the child's own "refusing push: older than local" guard protects against
# a real downgrade in name only.  tz_offset is what puts both sides on one
# time axis; a peer that does not send one keeps the historical behaviour.
# ---------------------------------------------------------------------------

_BUILD_STAMP_FMT = "%y%m%d_%H%M%S"


def _own_offset_minutes(stamp: str) -> int:
    """Minutes east of UTC of this host, as it stood at *stamp*'s date."""
    offset = datetime.datetime.strptime(stamp, _BUILD_STAMP_FMT).astimezone().utcoffset()
    return int(offset.total_seconds() // 60)


def _stamp_in_a_zone_away(stamp: str, delta_minutes: int) -> str:
    """*stamp* rewritten by a host ``delta_minutes`` off this host's zone.

    The instant is unchanged; only the wall clock moves.  Read back without
    knowing the zone, that same text is ``delta_minutes`` off the instant it
    named -- the whole cross-timezone bug in one string.
    """
    naive = datetime.datetime.strptime(stamp, _BUILD_STAMP_FMT)
    instant = naive.timestamp()
    zone = datetime.timezone(
        datetime.timedelta(minutes=_own_offset_minutes(stamp) + delta_minutes))
    return datetime.datetime.fromtimestamp(instant, zone).strftime(_BUILD_STAMP_FMT)


class TestBuildStampTimezones:
    def test_hello_advertises_the_zone_its_stamps_were_taken_in(self, server):
        status, body = _request(server, "GET", "/v1/setup?op=hello")
        assert status == 200
        assert body["tz_offset"] == local_tz_offset_minutes()

    def test_own_zone_never_defaults_to_utc(self, server, monkeypatch):
        # A versions snapshot that carries no zone must not turn into a claim of
        # being UTC: a peer cannot tell that claim from the truth, and it quietly
        # re-bases every comparison it makes against our numbers.
        monkeypatch.setattr("runtime.env_manager.local_tz_offset_minutes", lambda: 480)
        with patch("runtime.env_manager.compute_setup_versions",
                   return_value=dict(_LOCAL_PUSH_VERSIONS)):
            status, body = _request(server, "GET", "/v1/setup?op=hello")
        assert status == 200
        assert body["tz_offset"] == 480

    def test_restamping_moves_the_wall_clock_not_the_instant(self):
        stamp = "260209_120000"
        # What a host eight hours behind us writes for the instant our own
        # 12:00 names -- and putting it back must land on 12:00 again.
        peer_wrote = _stamp_in_a_zone_away(stamp, -480)
        assert peer_wrote == "260209_040000"
        assert restamp_to_local(
            peer_wrote, _own_offset_minutes(stamp) - 480) == stamp

    def test_a_foreign_peer_snapshot_is_put_on_the_local_axis(self, monkeypatch):
        # A UTC child seen by a UTC+8 parent: the numbers shown, the upgrade/
        # downgrade arrows and the delta mtime thresholds all speak local time.
        monkeypatch.setattr(
            "runtime.remote_env_manager._local_tz_offset_minutes", lambda: 480)
        snap = snapshot_from_hello({
            "frontend_build": "260209_120000",
            "backend_build": "260209_120000",
            "last_config": "260209_120000",
            "tz_offset": 0,
        })
        assert snap["tz_offset"] == 0
        assert snap["frontend_build"] == "260209_200000"
        assert snap["backend_build"] == "260209_200000"
        assert snap["last_config"] == "260209_200000"

    def test_a_peer_that_sends_no_zone_is_left_on_the_historical_axis(self):
        snap = snapshot_from_hello({
            "frontend_build": "260209_120000",
            "backend_build": "260209_120000",
            "last_config": "260209_120000",
        })
        assert "tz_offset" not in snap
        assert snap["backend_build"] == "260209_120000"

    def test_a_peer_in_our_own_zone_is_recorded_but_not_rewritten(self, monkeypatch):
        monkeypatch.setattr(
            "runtime.remote_env_manager._local_tz_offset_minutes", lambda: 480)
        snap = snapshot_from_hello({"backend_build": "260209_120000", "tz_offset": 480})
        assert snap["tz_offset"] == 480
        assert snap["backend_build"] == "260209_120000"

    def test_unparsable_stamps_and_zones_are_passed_through(self):
        assert restamp_to_local("250101", 480) == "250101"
        assert restamp_to_local("", 480) == ""
        assert restamp_to_local("0", 480) == "0"
        assert hello_tz_offset_minutes({}) is None
        assert hello_tz_offset_minutes({"tz_offset": "east-ish"}) is None
        assert hello_tz_offset_minutes({"tz_offset": True}) is None

    def test_the_record_keeps_the_zone_only_when_the_peer_reported_it(self, tmp_path):
        # Writing 0 for a peer that never reported a zone would paint it UTC in
        # the UI and silently re-base every comparison.
        manager = RemoteEnvManager(str(tmp_path / "remote_envs.json"))
        manager.upsert("http://10.0.0.5:7988/",
                       snapshot={"backend_build": "260209_120000", "tz_offset": 0})
        manager.upsert("http://10.0.0.9:7988/",
                       snapshot={"backend_build": "260209_120000"})
        envs = manager.read()
        assert envs[0]["tz_offset"] == 0
        assert "tz_offset" not in envs[1]

    def test_a_target_stamp_from_another_zone_is_not_a_downgrade(self, server):
        # The parent's stamp names the same instant as this environment's own
        # version, only written in a zone eight hours behind ours: knowing the
        # zone, the push is not a downgrade and goes through.
        stamp = _LOCAL_PUSH_VERSIONS["backend_build"]
        target = _stamp_in_a_zone_away(stamp, -480)
        tar = _make_tar_bytes({"agents_runtime/models.json": "[]"})
        with patch("runtime.env_manager.compute_setup_versions",
                   return_value=dict(_LOCAL_PUSH_VERSIONS,
                                     tz_offset=_own_offset_minutes(stamp))):
            status, body = _request_raw(
                server, "POST",
                "/v1/setup?op=push&frontend_build={t}&backend_build={t}"
                "&last_config={t}&tz_offset={z}".format(
                    t=target, z=_own_offset_minutes(stamp) - 480),
                tar,
            )
        assert status == 200, body
        assert body["updated"] is True

    def test_the_same_stamp_without_a_zone_is_still_read_as_older(self, server):
        # The historical comparison, kept on purpose for peers that predate
        # tz_offset -- and the reason the zone has to travel at all: the same
        # instant looks eight hours older when the zone is dropped.
        stamp = _LOCAL_PUSH_VERSIONS["backend_build"]
        target = _stamp_in_a_zone_away(stamp, -480)
        tar = _make_tar_bytes({"agents_runtime/models.json": "[]"})
        with patch("runtime.env_manager.compute_setup_versions",
                   return_value=_LOCAL_PUSH_VERSIONS):
            status, body = _request_raw(
                server, "POST",
                "/v1/setup?op=push&frontend_build={t}&backend_build={t}"
                "&last_config={t}".format(t=target),
                tar,
            )
        assert status == 400
        assert "older than local" in body["error"]

    def test_an_unusable_zone_falls_back_to_local_time(self, server):
        # A garbage tz_offset must not turn into a 400: the request is answered
        # the way it was before zones existed (a baseline in the far future
        # means "nothing newer" -> 304).
        future = "291231_235900"
        path = ("/v1/setup?op=delta&frontend_build={f}&backend_build={f}"
                "&last_config={f}&tz_offset=east-ish".format(f=future))
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}{path}", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        assert status == 304

    def test_a_pushed_update_forgets_the_cached_mtime(self, tmp_path):
        # backend_build is derived from project mtimes behind a short-TTL cache.
        # An applied delta rewrites those files; while the cache lives the very
        # next hello still advertises the pre-update version ("updated, but the
        # version number did not change").
        project = tmp_path / "project"
        (project / "agents_runtime").mkdir(parents=True)
        source = project / "agents_runtime" / "thing.py"
        source.write_text("print('old')\n", encoding="utf-8")
        manager = EnvManager(env_path=str(tmp_path / "env.json"))

        before = manager.get_backend_build_mtime(str(project))
        source.write_text("print('new')\n", encoding="utf-8")
        os.utime(source, (before + 120, before + 120))
        assert manager.get_backend_build_mtime(str(project)) == before

        invalidate_backend_mtime_cache(str(project))
        assert manager.get_backend_build_mtime(str(project)) == before + 120

    def test_invalidating_one_root_leaves_another_cached(self, tmp_path):
        first = tmp_path / "first"
        second = tmp_path / "second"
        for root in (first, second):
            root.mkdir()
            (root / "thing.py").write_text("x\n", encoding="utf-8")
        manager = EnvManager(env_path=str(tmp_path / "env.json"))
        first_before = manager.get_backend_build_mtime(str(first))
        second_before = manager.get_backend_build_mtime(str(second))

        for root in (first, second):
            source = root / "thing.py"
            source.write_text("y\n", encoding="utf-8")
            os.utime(source, (first_before + 120, first_before + 120))

        invalidate_backend_mtime_cache(str(first))
        assert manager.get_backend_build_mtime(str(first)) == first_before + 120
        assert manager.get_backend_build_mtime(str(second)) == second_before

        invalidate_backend_mtime_cache()  # no root: forget everything
        assert manager.get_backend_build_mtime(str(second)) == first_before + 120


# ---------------------------------------------------------------------------
# A parent that has just pushed must show the child's NEW version, not the
# hello snapshot it took before the push.
# ---------------------------------------------------------------------------


class TestPushRefreshesTheAdvertisedVersion:
    def test_the_response_carries_the_childs_post_push_versions(self, server, tmp_path):
        # A config-only delta: the child applies it and stays up, so the parent
        # can (and now does) re-probe hello right away.  Before that, the row in
        # the environment list kept the pre-push numbers until the next tunnel
        # reconnect -- for a child that does not restart, never.
        with _real_child_server(tmp_path, name="child_refresh") as (child, child_data):
            child_hello = _wait_child_hello(child)
            assert child_hello["last_config"] == ""

            (tmp_path / "models.json").write_text("[]", encoding="utf-8")
            env_id = _add_env(server, f"http://127.0.0.1:{child.port}/v1/setup")
            status, result = _request(
                server, "POST", f"/v1/remote-envs/{env_id}/push-update",
            )
            assert status == 200, result
            assert result["updated"] is True
            assert result["restart_backend"] is False

            # Fresh, not the pre-push hello: the child now advertises what the
            # parent pushed, and so does the persisted record.
            assert result["remote"]["last_config"] == result["local"]["last_config"]
            status, listed = _request(server, "GET", "/v1/remote-envs")
            assert status == 200
            assert listed["envs"][0]["last_config"] == result["local"]["last_config"]

    def test_the_apply_path_drops_the_advertised_mtime_cache(self, server, tmp_path):
        # backend_build is scanned from the project tree behind a short TTL, so
        # applying a delta must drop that cache or the next hello describes the
        # files as they were before the update.  Only the wiring is checked here:
        # pushing an actual project file would rewrite the checkout the tests run
        # from, so a config-only member stands in for "files were rewritten".
        seen: list = []

        def _fake_invalidate(project_root=None):
            seen.append(project_root)

        tar = _make_tar_bytes({"agents_runtime/models.json": "[]"})
        with patch("runtime.env_manager.invalidate_backend_mtime_cache",
                   _fake_invalidate):
            status, body = _request_raw(server, "POST", "/v1/setup?op=push", tar)
        assert status == 200, body
        assert seen == [os.path.realpath(
            os.path.join(os.path.dirname(handler_api_module.__file__), ".."))]


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

    def test_upsert_persists_online(self, tmp_path):
        manager = self._manager(tmp_path)
        envs = manager.upsert("http://10.0.0.5:7988/", {"frontend_build": "v1"}, True)
        assert envs[0]["online"] is True
        on_disk = json.loads((tmp_path / "remote_envs.json").read_text(encoding="utf-8"))
        assert on_disk[0]["online"] is True

    def test_update_snapshot_online_only_keeps_versions(self, tmp_path):
        manager = self._manager(tmp_path)
        manager.upsert("http://10.0.0.5:7988/", {"backend_build": "b1"}, True)
        # 检查失败时只回写在线状态：已有的版本快照字段必须保留
        envs = manager.update_snapshot("http://10.0.0.5:7988", None, False)
        assert envs[0]["online"] is False
        assert envs[0]["backend_build"] == "b1"
        envs = manager.update_snapshot("http://10.0.0.5:7988", None, True)
        assert envs[0]["online"] is True
        assert envs[0]["backend_build"] == "b1"

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


class TestCredentialUrlEndToEnd:
    """凭据写在 URL 里（``:密码@``）时对真实子环境的调用。

    登记探测（GET hello）、刷新状态、浏览器桥（GET/POST 子环境 API）都只靠
    ``Authorization: Basic`` 授权，不带 ``?token=``。子环境开了访问密码也连得通
    —— 这正是旧写法做不到的（userinfo 留在 host 里，连 DNS 都过不去）。
    """

    @staticmethod
    def _enable_child_auth(child, password: str) -> None:
        status, config = _request(child, "POST", "/v1/auth/config", {"password": password})
        assert status == 200
        assert config.get("setup_token")

    def test_add_refresh_and_bridge_share_the_url_credential(self, server, tmp_path):
        ws = tmp_path / "cred_child_ws"
        ws.mkdir()
        (ws / "secret.txt").write_text("top secret", encoding="utf-8")
        with _real_child_server(tmp_path, name="child_credential_url",
                                workspace=str(ws)) as (child, _data):
            _wait_child_hello(child)
            self._enable_child_auth(child, "child-pass")

            status, body = _request(
                server, "POST", "/v1/remote-envs",
                {"url": f"http://gateway:child-pass@127.0.0.1:{child.port}/v1/setup"},
            )
            assert status == 200, body
            env = body["envs"][0]
            # 记录 id 只有可建连的 host（密码不进 id，也不进 remote_envs.json 的键）；
            # 拨号用的 URL 保留凭据
            assert env["id"] == f"http://127.0.0.1:{child.port}"
            assert "child-pass" in env["url"]

            status, body = _request(
                server, "POST",
                f"/v1/remote-envs/{urllib.parse.quote(env['id'], safe='')}/hello",
            )
            assert status == 200, body
            assert body.get("ok") is True

            # 浏览器的文件视图走的是另一条桥（GET/POST 子环境 API）：同一份凭据
            status, raw = _bridge(server, env["id"],
                                  "/v1/workspace/list?path=.&page=1&page_size=50&restrict=0")
            assert status == 200, raw[:200]
            data = json.loads(raw)
            names = [e.get("name") for e in data.get("entries", data.get("files", []))]
            assert "secret.txt" in names, (names, raw[:300])
            status, raw = _bridge(server, env["id"], "/v1/workspace/mkdir",
                                  method="POST", payload={"parent_path": ".", "name": "cred_dir"})
            assert status == 200, raw[:200]
            assert (ws / "cred_dir").is_dir()

    def test_password_and_token_together_reach_the_child(self, server, tmp_path):
        """一把 URL 里同时放网关口令和子环境 token。

        一个请求只有一个 ``Authorization``：URL 里的凭据占它（过网关靠的是它），
        token 改放 ``X-Agent-Service-Token``。这里的凭据故意不是子环境的口令，
        所以能不能通，全看 token 有没有跟着走过 POST。
        """
        ws = tmp_path / "cred_child_ws_both"
        ws.mkdir()
        with _real_child_server(tmp_path, name="child_credential_both",
                                workspace=str(ws)) as (child, _data):
            _wait_child_hello(child)
            status, config = _request(child, "POST", "/v1/auth/config",
                                      {"password": "child-pass"})
            assert status == 200, config
            setup_token = config["setup_token"]

            status, body = _request(
                server, "POST", "/v1/remote-envs",
                {"url": f"http://gateway:gateway-secret@127.0.0.1:{child.port}"
                        f"/v1/setup?token={setup_token}"},
            )
            assert status == 200, body
            env = body["envs"][0]
            assert env["id"] == f"http://127.0.0.1:{child.port}"

            status, raw = _bridge(server, env["id"], "/v1/workspace/mkdir",
                                  method="POST",
                                  payload={"parent_path": ".", "name": "both_dir"})
            assert status == 200, raw[:200]
            assert (ws / "both_dir").is_dir()

    def test_wrong_password_is_reported_as_child_auth(self, server, tmp_path):
        with _real_child_server(tmp_path, name="child_credential_bad") as (child, _data):
            _wait_child_hello(child)
            self._enable_child_auth(child, "child-pass")
            status, body = _request(
                server, "POST", "/v1/remote-envs",
                {"url": f"http://gw:nope@127.0.0.1:{child.port}/v1/setup"},
            )
            assert status == 400
            assert body["error"] == "child_auth"

    def test_userinfo_stays_off_the_request_line_and_host(self, server):
        # 一次拨号发出去的东西：请求行与 Host 里都不能有 '@'（那是旧写法失败的
        # 根因），凭据只出现在 Authorization 头
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append((self.path, self.headers.get("Host"),
                             self.headers.get("Authorization")))
                data = json.dumps({
                    "frontend_build": "250101_000000",
                    "backend_build": "250101_000000",
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            port = httpd.server_address[1]
            env_id = _seed_env(
                server, f"http://127.0.0.1:{port}",
                f"http://gw:pw@127.0.0.1:{port}/v1/setup",
            )
            status, body = _request(
                server, "POST",
                f"/v1/remote-envs/{urllib.parse.quote(env_id, safe='')}/hello",
            )
            assert status == 200, body
            path, host, auth = seen[-1]
            assert "@" not in path
            assert "@" not in (host or "")
            assert auth == "Basic " + base64.b64encode(b"gw:pw").decode("ascii")
        finally:
            httpd.shutdown()
            httpd.server_close()


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

    def test_add_persists_server_side_snapshot(self, server, tmp_path):
        # add now probes the child server-side: the snapshot comes from the
        # child's own hello, not from the request body.
        with _real_child_server(tmp_path) as (child, _child_data):
            status, hello = _request(child, "GET", "/v1/setup?op=hello")
            assert status == 200
            status, body = _request(server, "POST", "/v1/remote-envs", {
                "url": f"http://127.0.0.1:{child.port}/v1/setup?token=tok&op=hello",
            })
            assert status == 200
            envs = body["envs"]
            assert len(envs) == 1
            assert envs[0]["id"] == f"http://127.0.0.1:{child.port}"
            # The stored URL keeps the token (and drops the per-request op).
            assert envs[0]["url"] == f"http://127.0.0.1:{child.port}/v1/setup?token=tok"
            # The snapshot reflects the child's real hello (not a client-supplied one).
            assert envs[0]["frontend_build"] == hello["frontend_build"]
            assert envs[0]["online"] is True
            assert envs[0]["checked_at"]
            on_disk = json.loads((tmp_path / "remote_envs.json").read_text(encoding="utf-8"))
            assert on_disk == envs

    def test_add_reports_child_auth(self, server, tmp_path):
        # Child has auth enabled but the URL carries no token: the server-side
        # probe is rejected and the environment is NOT recorded.
        with _real_child_server(tmp_path, name="child_auth_add") as (child, _child_data):
            _wait_child_hello(child)
            status, config = _request(child, "POST", "/v1/auth/config", {"password": "child-pass"})
            assert status == 200
            assert config.get("setup_token", "")
            status, body = _request(server, "POST", "/v1/remote-envs", {
                "url": f"http://127.0.0.1:{child.port}/v1/setup",
            })
            assert status == 400
            assert body["error"] == "child_auth"
            # Nothing was persisted (the probe failed).
            status, listing = _request(server, "GET", "/v1/remote-envs")
            assert status == 200
            assert listing["envs"] == []

    def test_add_unreachable_child(self, server):
        # A child the server cannot route to: add reports child_unreachable.
        status, body = _request(server, "POST", "/v1/remote-envs", {
            "url": "http://127.0.0.1:1/v1/setup",
        })
        assert status == 502
        assert body["error"] == "child_unreachable"

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

    def test_add_upserts_same_host(self, server, tmp_path):
        # Adding the same host twice (reachable child) keeps a single record;
        # the URL (token) is refreshed, the snapshot is re-probed.
        with _real_child_server(tmp_path) as (child, _child_data):
            url = f"http://127.0.0.1:{child.port}/v1/setup"
            status, _ = _request(server, "POST", "/v1/remote-envs", {"url": url})
            assert status == 200
            status, body = _request(server, "POST", "/v1/remote-envs", {
                "url": f"{url}?token=x",
            })
            assert status == 200
            assert len(body["envs"]) == 1
            assert body["envs"][0]["url"] == f"{url}?token=x"
            assert body["envs"][0]["frontend_build"]

    def test_update_snapshot(self, server, tmp_path):
        env_id = _seed_env(server, "http://10.0.0.5:7988", "http://10.0.0.5:7988/")
        status, body = _request(
            server, "PUT", f"/v1/remote-envs/{env_id}",
            {"snapshot": {"backend_build": "b9", "inference_active": False}},
        )
        assert status == 200
        assert body["envs"][0]["backend_build"] == "b9"
        assert body["envs"][0]["inference_active"] is False

    def test_update_online_only_keeps_snapshot(self, server, tmp_path):
        env_id = _seed_env(
            server, "http://10.0.0.5:7988", "http://10.0.0.5:7988/",
            snapshot={"backend_build": "b1"}, online=True,
        )
        status, body = _request(
            server, "PUT", f"/v1/remote-envs/{env_id}",
            {"online": False},
        )
        assert status == 200
        assert body["envs"][0]["online"] is False
        # 只回写在线状态时版本快照不清空
        assert body["envs"][0]["backend_build"] == "b1"

    def test_update_snapshot_and_online(self, server, tmp_path):
        env_id = _seed_env(server, "http://10.0.0.5:7988", "http://10.0.0.5:7988/")
        status, body = _request(
            server, "PUT", f"/v1/remote-envs/{env_id}",
            {"snapshot": {"backend_build": "b9"}, "online": True},
        )
        assert status == 200
        assert body["envs"][0]["backend_build"] == "b9"
        assert body["envs"][0]["online"] is True

    def test_update_snapshot_requires_body(self, server, tmp_path):
        env_id = _seed_env(server, "http://10.0.0.5:7988", "http://10.0.0.5:7988/")
        status, _ = _request(server, "PUT", f"/v1/remote-envs/{env_id}", {})
        assert status == 400

    def test_update_snapshot_missing(self, server):
        status, _ = _request(
            server, "PUT", "/v1/remote-envs/http://10.9.9.9:1",
            {"snapshot": {"backend_build": "x"}},
        )
        assert status == 404

    def test_delete(self, server, tmp_path):
        env_id = _seed_env(server, "http://10.0.0.5:7988", "http://10.0.0.5:7988/")
        status, body = _request(server, "DELETE", f"/v1/remote-envs/{env_id}")
        assert status == 200
        assert body == {"envs": []}
        status, _ = _request(server, "DELETE", f"/v1/remote-envs/{env_id}")
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
# op=source-hello: server-side hello probe of an update source
# ---------------------------------------------------------------------------


class TestSetupSourceHello:
    def test_returns_remote_and_local(self, server, tmp_path):
        # A reachable source: the probe runs server-side and the response
        # carries both the remote snapshot and this environment's local
        # versions, so the check-update card needs a single request.
        with _real_child_server(tmp_path) as (child, _child_data):
            source = f"http://127.0.0.1:{child.port}/v1/setup"
            query = urllib.parse.urlencode({"op": "source-hello", "source": source})
            status, body = _request(server, "GET", f"/v1/setup?{query}")
            assert status == 200
            assert "frontend_build" in body["remote"]
            assert "backend_build" in body["remote"]
            assert "frontend_build" in body["local"]
            assert "backend_build" in body["local"]
            assert "last_config" in body["local"]
            assert isinstance(body["local"]["inference_active"], bool)

    def test_missing_source(self, server):
        status, body = _request(server, "GET", "/v1/setup?op=source-hello")
        assert status == 400
        assert "source" in body["error"]

    def test_invalid_source(self, server):
        query = urllib.parse.urlencode({"op": "source-hello", "source": "nope"})
        status, body = _request(server, "GET", f"/v1/setup?{query}")
        assert status == 400
        assert body["error"]

    def test_unreachable_source(self, server):
        query = urllib.parse.urlencode(
            {"op": "source-hello", "source": "http://127.0.0.1:1/v1/setup"})
        status, body = _request(server, "GET", f"/v1/setup?{query}")
        assert status == 502
        assert body["error"] == "source_unreachable"

    def test_source_auth_rejected(self, server, tmp_path):
        # The source has authorization enabled and the check carries no
        # token: it is reported as source_auth (not source_unreachable).
        with _real_child_server(tmp_path, name="child_source_auth") as (child, _child_data):
            _wait_child_hello(child)
            status, config = _request(
                child, "POST", "/v1/auth/config", {"password": "child-pass"},
            )
            assert status == 200
            assert config.get("setup_token", "")
            query = urllib.parse.urlencode(
                {"op": "source-hello", "source": f"http://127.0.0.1:{child.port}/v1/setup"})
            status, body = _request(server, "GET", f"/v1/setup?{query}")
            assert status == 400
            assert body["error"] == "source_auth"


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


# The downgrade guard only fires for axes whose *local* version is
# non-empty.  The local frontend version is derived from this checkout's
# web/dist (env_manager.frontend_build_version), which test checkouts may
# not have built.  Pin the local versions so the frontend-axis guard tests
# below pass with or without a frontend build in the worktree.
_LOCAL_PUSH_VERSIONS = {
    "frontend_build": "260209_000000",
    "backend_build": "260209_000000",
    "last_config": "260209_000000",
}


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
        with patch("runtime.env_manager.compute_setup_versions",
                   return_value=_LOCAL_PUSH_VERSIONS):
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
        # deliberately old target versions (local versions pinned, see
        # _LOCAL_PUSH_VERSIONS).
        with patch("runtime.env_manager.compute_setup_versions",
                   return_value=_LOCAL_PUSH_VERSIONS):
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
        # rejects the deliberately old target versions (local versions
        # pinned, see _LOCAL_PUSH_VERSIONS).
        tar = _make_tar_bytes({"agents_runtime/models.json": "[]"})
        with patch("runtime.env_manager.compute_setup_versions",
                   return_value=_LOCAL_PUSH_VERSIONS):
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
# op=push: replacing the compiled frontend (web/dist)
# ---------------------------------------------------------------------------


def _write_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestSetupPushFrontendDist:
    """A pushed frontend must never leave the environment without a dist.

    Regression: the receiver rmtree'd web/dist *before* copying anything, so a
    delta carrying only web/dist/build_version — what a parent whose failed
    build refreshed the stamp without producing output used to send — deleted
    the child's whole frontend and the UI answered {"error": "Not found: /"}.
    """

    # A target version far ahead of any real build, so the downgrade guard passes.
    _PUSH_PATH = "/v1/setup?op=push&frontend_build=300101_000000"

    @staticmethod
    def _make_project(tmp_path):
        project = tmp_path / "target"
        dist = project / "web" / "dist"
        _write_text(dist / "index.html", "<p>old</p>\n")
        _write_text(dist / "assets" / "index-old.js", "old\n")
        return project, dist

    @staticmethod
    def _push(server, project, tar):
        # _apply_setup_delta resolves the project root from __file__; point it
        # at the throwaway tree so the test never touches the real web/dist.
        with patch.object(
            handler_api_module, "__file__",
            str(project / "runtime" / "handler_api.py"),
        ):
            return _request_raw(server, "POST", TestSetupPushFrontendDist._PUSH_PATH, tar)

    @staticmethod
    def _staging_leftovers(project):
        return [
            entry.name for entry in (project / "web").iterdir()
            if entry.name.startswith(".dist-staging-")
        ]

    def test_incomplete_dist_is_refused_and_the_old_frontend_survives(self, server, tmp_path):
        project, dist = self._make_project(tmp_path)
        # The stamp a failed build leaves behind: no index.html, no assets.
        tar = _make_tar_bytes({"web/dist/build_version": "260915_165601"})

        status, body = self._push(server, project, tar)

        assert status == 500
        assert "index.html" in body["error"]
        assert (dist / "index.html").read_text(encoding="utf-8") == "<p>old</p>\n"
        assert (dist / "assets" / "index-old.js").is_file()
        assert self._staging_leftovers(project) == []

    def test_complete_dist_replaces_the_whole_web_dist(self, server, tmp_path):
        project, dist = self._make_project(tmp_path)
        tar = _make_tar_bytes({
            "web/dist/index.html": "<p>new</p>\n",
            "web/dist/assets/index-fresh.js": "fresh\n",
            "web/dist/build_version": "260915_170000",
        })

        status, body = self._push(server, project, tar)

        assert status == 200
        assert body["updated"] is True
        assert body["restart_backend"] is False
        assert sorted(body["updated_files"]) == [
            "web/dist/assets/index-fresh.js",
            "web/dist/build_version",
            "web/dist/index.html",
        ]
        assert (dist / "index.html").read_text(encoding="utf-8") == "<p>new</p>\n"
        assert (dist / "assets" / "index-fresh.js").is_file()
        # Content-hashed leftovers from the previous build are dropped.
        assert not (dist / "assets" / "index-old.js").exists()
        assert self._staging_leftovers(project) == []

    def test_update_slot_is_released_after_a_refused_frontend_delta(self, server, tmp_path):
        project, dist = self._make_project(tmp_path)
        status, _body = self._push(
            server, project, _make_tar_bytes({"web/dist/build_version": "260915_165601"}),
        )
        assert status == 500

        # A refusal must not leave the environment marked "update in progress".
        status, body = self._push(
            server, project, _make_tar_bytes({"web/dist/index.html": "<p>new</p>\n"}),
        )

        assert status == 200
        assert body["updated"] is True
        assert (dist / "index.html").read_text(encoding="utf-8") == "<p>new</p>\n"

    def test_delta_from_a_parent_with_a_stale_dist_stamp_installs_whole_dist(
        self, server, tmp_path,
    ):
        """End-to-end reproduction of the reported breakage.

        The parent's dist looks exactly like one left behind by a *failed*
        build: every asset is old, only the build_version stamp is fresh.  The
        delta must still carry the whole dist, because the receiver replaces
        the directory — this shipped the stamp alone before the fix, which
        deleted the child's index.html and assets.
        """
        parent = tmp_path / "parent-project"
        parent_dist = parent / "web" / "dist"
        _write_text(parent_dist / "index.html", "<p>parent</p>\n")
        _write_text(parent_dist / "assets" / "index-parent.js", "parent\n")
        _write_text(parent_dist / "build_version", "260915_170000")

        old, changed = 1_500_000_000, 1_700_000_000
        for path in parent.rglob("*"):
            if path.is_file():
                os.utime(path, (old, old))
        os.utime(parent_dist / "build_version", (changed, changed))

        tar = EnvManager(env_path=str(tmp_path / "parent-env.json")).build_delta_tar(
            project_root=str(parent),
            data_dir=str(tmp_path / "parent-data"),
            frontend_since=1_600_000_000,
            backend_since=1_600_000_000,
            config_since=1_600_000_000,
        )
        assert tar is not None

        project, dist = self._make_project(tmp_path)
        status, body = self._push(server, project, tar)

        assert status == 200
        assert body["updated"] is True
        assert (dist / "index.html").read_text(encoding="utf-8") == "<p>parent</p>\n"
        assert (dist / "assets" / "index-parent.js").is_file()
        assert not (dist / "assets" / "index-old.js").exists()


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


def _seed_env(server, env_id, url, snapshot=None, online=None):
    """Record an environment directly (bypassing the server-side hello probe).

    add() now requires a reachable child, so tests that only exercise the
    snapshot/online/delete bookkeeping seed the store through the manager.
    """
    server._remote_env_manager.upsert(url, snapshot=snapshot, online=online)
    return env_id


@contextlib.contextmanager
def _real_child_server(tmp_path, name="child_data", workspace=None):
    """Start a second real RuntimeHTTPServer (isolated data dir) as the child."""
    child_data = tmp_path / name
    child_data.mkdir()
    if workspace:
        os.makedirs(workspace, exist_ok=True)
        (child_data / "env.json").write_text(
            json.dumps({"AGENTS_WORKSPACE": workspace}), encoding="utf-8"
        )
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

    def test_bare_url_child_reports_child_auth(self, server, tmp_path):
        # A record registered without ?token=... against an auth-enabled child
        # (e.g. added before auth was enabled): the push probe is rejected and
        # the parent reports child_auth (not child_unreachable), so the user
        # knows to re-register the environment with the full setup link.
        with _real_child_server(tmp_path, name="child_data_auth") as (child, _child_data):
            _wait_child_hello(child)
            status, config = _request(
                child, "POST", "/v1/auth/config", {"password": "child-pass"},
            )
            assert status == 200
            assert config.get("setup_token", "")

            # add() itself would now refuse this URL (child_auth); seed the
            # pre-existing record directly to exercise the push-time path.
            env_id = _seed_env(
                server, f"http://127.0.0.1:{child.port}",
                f"http://127.0.0.1:{child.port}/v1/setup",
            )
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

    def test_child_unreachable(self, server, tmp_path):
        # The recorded child is down: push reports child_unreachable.
        env_id = _seed_env(server, "http://127.0.0.1:1", "http://127.0.0.1:1/v1/setup")
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


# ---------------------------------------------------------------------------
# Unified online field + shared tool proxy cache
# ---------------------------------------------------------------------------

class TestUnifiedOnlineField:
    def _manager(self, tmp_path):
        return RemoteEnvManager(str(tmp_path / "remote_envs.json"))

    def test_tunnel_record_uses_online_bool(self, tmp_path):
        manager = self._manager(tmp_path)
        envs = manager.upsert_tunnel("a" * 16, None, "")
        rec = envs[0]
        assert rec["online"] is False
        assert "status" not in rec
        assert rec["last_seen"] == ""

    def test_update_tunnel_status_last_seen_only_when_online(self, tmp_path):
        manager = self._manager(tmp_path)
        manager.upsert_tunnel("b" * 16, None, "")
        manager.update_tunnel_status("tunnel:" + "b" * 16, True)
        envs = manager.read()
        assert envs[0]["online"] is True
        first_seen = envs[0]["last_seen"]
        assert first_seen
        time.sleep(0.01)
        # detach: online -> False, last_seen keeps the last ONLINE time
        manager.update_tunnel_status("tunnel:" + "b" * 16, False)
        envs = manager.read()
        assert envs[0]["online"] is False
        assert envs[0]["last_seen"] == first_seen
        manager.update_tunnel_status("tunnel:" + "b" * 16, True)
        envs = manager.read()
        assert envs[0]["online"] is True
        assert envs[0]["last_seen"] >= first_seen

    def test_legacy_status_record_normalized_on_read(self, tmp_path):
        path = tmp_path / "remote_envs.json"
        path.write_text(json.dumps([
            {"id": "tunnel:" + "c" * 16, "url": "", "transport": "ws-tunnel",
             "tunnel_id": "c" * 16, "status": "online", "last_seen": "x"},
            {"id": "http://10.0.0.9:7988", "url": "http://10.0.0.9:7988/v1/setup"},
        ]), encoding="utf-8")
        manager = self._manager(tmp_path)
        envs = manager.read()
        assert envs[0]["online"] is True
        assert "status" not in envs[0]
        # direct record without an online field defaults to False
        assert envs[1]["online"] is False


class TestSharedProxyCache:
    def _manager(self, tmp_path):
        return RemoteEnvManager(str(tmp_path / "remote_envs.json"))

    def test_get_proxy_cached_per_env(self, tmp_path):
        manager = self._manager(tmp_path)
        manager.upsert("http://10.0.0.7:7988/", None)
        p1 = manager.get_proxy("http://10.0.0.7:7988")
        p2 = manager.get_proxy("http://10.0.0.7:7988")
        assert p1 is p2
        assert p1.env_id == "http://10.0.0.7:7988"

    def test_get_proxy_rebuilt_on_url_change(self, tmp_path):
        manager = self._manager(tmp_path)
        manager.upsert("http://10.0.0.7:7988/", None)
        p1 = manager.get_proxy("http://10.0.0.7:7988")
        manager.upsert("http://10.0.0.7:7988/v1/setup?token=zz", None)
        p2 = manager.get_proxy("http://10.0.0.7:7988")
        assert p2 is not p1

    def test_get_proxy_missing_env_raises(self, tmp_path):
        manager = self._manager(tmp_path)
        with pytest.raises(KeyError):
            manager.get_proxy("http://10.9.9.9:1")

    def test_invalidate_tools_cache(self, tmp_path):
        manager = self._manager(tmp_path)
        manager.upsert("http://10.0.0.7:7988/", None)
        proxy = manager.get_proxy("http://10.0.0.7:7988")
        proxy._tools_cache = [{"tool_id": "stale"}]
        proxy._tools_fetched_at = time.monotonic()
        manager.invalidate_tools_cache("http://10.0.0.7:7988")
        assert proxy._tools_cache is None
        assert proxy._tools_fetched_at == 0.0

    def test_remove_drops_proxy(self, tmp_path):
        manager = self._manager(tmp_path)
        manager.upsert("http://10.0.0.7:7988/", None)
        manager.get_proxy("http://10.0.0.7:7988")
        manager.remove("http://10.0.0.7:7988")
        assert manager._proxies == {}


# ---------------------------------------------------------------------------
# Browser bridge for DIRECT children: /v1/env-proxy/{env_id}/{child path}
# ---------------------------------------------------------------------------


def _bridge(parent, env_id, child_path, method="GET", payload=None, headers=None):
    """Call the parent's same-origin bridge; returns (status, raw bytes)."""
    url = (f"http://127.0.0.1:{parent.port}/v1/env-proxy/"
           f"{urllib.parse.quote(env_id, safe='')}{child_path}")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = dict(headers or {})
    if data is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# -- raw terminal socket helpers -------------------------------------------
#
# The terminal endpoint is one multiplexed WebSocket for the whole tab; a
# child-environment terminal is a channel of it, named by env_id on the open
# frame (runtime/terminal_env_bridge.py).

def _ws_open_channel(sock, channel: str, env_id: str | None = None,
                     cols: int = 80, rows: int = 24) -> None:
    frame = {"op": "open", "c": channel, "cols": cols, "rows": rows}
    if env_id:
        frame["env_id"] = env_id
    wsutil.ws_send_frame(sock, json.dumps(frame).encode("utf-8"),
                         wsutil.OP_TEXT, mask=True)


def _ws_wait_for(sock, pred, timeout: float = 15.0):
    """Next control frame on a browser terminal socket satisfying ``pred``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            frame = wsutil.ws_recv_frame(sock, expect_masked=False)
        except OSError:
            return None
        if frame is None:
            return None
        if frame[0] != wsutil.OP_TEXT:
            continue
        try:
            msg = json.loads(frame[1].decode("utf-8", "replace"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(msg, dict) and pred(msg):
            return msg
    return None


def _ws_channel_echo_input(sock, channel: str, command: bytes, marker: bytes,
                           timeout: float = 30.0) -> bytes:
    """Type ``command`` into an already-open channel and collect its output."""
    wsutil.ws_send_frame(sock, json.dumps(
        {"op": "input", "c": channel, "d": command.decode("ascii")},
    ).encode("utf-8"), wsutil.OP_TEXT, mask=True)
    buf = b""
    deadline = time.monotonic() + timeout
    while marker not in buf and time.monotonic() < deadline:
        try:
            frame = wsutil.ws_recv_frame(sock, expect_masked=False)
        except OSError:
            break
        if frame is None:
            break
        buf += frame[1]
    return buf


def _ws_close(sock) -> None:
    try:
        wsutil.ws_send_close(sock, 1000, mask=True)
    except OSError:
        pass
    sock.close()


class TestEnvProxyDirectBridge:
    """直连子环境：文件管理走母端同源 HTTP 桥，终端走共享 mux 的 env_id 通道。

    隧道环境的桥早已存在；这里验证同一个 handler 的**直连传输**：路径重建、
    请求/响应头白名单、错误体透传都是共用的，只有最后一跳（urllib vs 隧道）
    不同。要点是浏览器永远不需要子端地址与 token —— 母端从登记记录里注入。
    终端不是请求，因此不在 HTTP 桥里：它挂在本 tab 唯一那条
    ``WS /v1/terminals/ws`` 上，open 帧带 env_id 决定 PTY 在哪个环境跑。
    """

    def test_reads_and_writes_are_forwarded(self, server, tmp_path):
        ws = str(tmp_path / "child_ws")
        with _real_child_server(tmp_path, workspace=ws) as (child, _data):
            _wait_child_hello(child)
            env_id = _add_env(server, f"http://127.0.0.1:{child.port}/v1/setup")
            (tmp_path / "child_ws" / "bridged.txt").write_text("bridge ok", encoding="utf-8")

            status, raw = _bridge(
                server, env_id, "/v1/workspace/list?path=.&page=1&page_size=50&restrict=0")
            assert status == 200, raw
            names = [e.get("name") for e in json.loads(raw).get("files", [])]
            assert "bridged.txt" in names, names

            status, raw = _bridge(
                server, env_id, "/v1/workspace/content?path=bridged.txt&restrict=0")
            assert status == 200, raw
            assert raw == b"bridge ok"

            status, raw = _bridge(server, env_id, "/v1/workspace/mkdir",
                                  method="POST", payload={"parent_path": ".", "name": "bridged_dir"})
            assert status == 200, raw
            assert (tmp_path / "child_ws" / "bridged_dir").is_dir()

    def test_query_and_percent_encoded_paths_survive_the_hop(self, server, tmp_path):
        """预览/下载是 URL 消费的接口：路径与查询参数必须原样到达子端。"""
        ws = str(tmp_path / "child_ws")
        with _real_child_server(tmp_path, workspace=ws) as (child, _data):
            _wait_child_hello(child)
            env_id = _add_env(server, f"http://127.0.0.1:{child.port}/v1/setup")
            (tmp_path / "child_ws" / "a b 图.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")

            quoted = urllib.parse.quote("a b 图.png", safe="")
            status, raw = _bridge(
                server, env_id, f"/v1/workspace/content?path={quoted}&restrict=0")
            assert status == 200, raw
            assert raw.startswith(b"\x89PNG")

    def test_child_token_is_injected_by_the_parent(self, server, tmp_path):
        """子端开了密码：浏览器不带任何凭据，母端用登记的 token 代它授权。"""
        ws = str(tmp_path / "child_ws")
        with _real_child_server(tmp_path, workspace=ws) as (child, _data):
            _wait_child_hello(child)
            status, config = _request(child, "POST", "/v1/auth/config", {"password": "child-pass"})
            assert status == 200, config
            token = config.get("setup_token", "")
            assert token
            env_id = _add_env(
                server, f"http://127.0.0.1:{child.port}/v1/setup?token={token}")

            # 不带凭据直连子端会被拒；走母端桥则母端注入 token
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{child.port}/v1/workspace/list?path=.&restrict=0",
                    timeout=10)
            assert exc.value.code == 401

            status, raw = _bridge(
                server, env_id, "/v1/workspace/list?path=.&page=1&page_size=50&restrict=0")
            assert status == 200, raw
            assert "files" in json.loads(raw)

    def test_writes_are_authorized_too(self, server, tmp_path):
        """子端 ?token= 的豁免只对 GET 生效；PUT/POST/DELETE 要靠 Authorization 头。

        桥替浏览器补的就是这个头 —— 否则上传分片、改名、删除会在子端 401。
        """
        ws = str(tmp_path / "child_ws")
        with _real_child_server(tmp_path, workspace=ws) as (child, _data):
            _wait_child_hello(child)
            status, config = _request(child, "POST", "/v1/auth/config", {"password": "child-pass"})
            assert status == 200, config
            token = config.get("setup_token", "")
            assert token
            env_id = _add_env(
                server, f"http://127.0.0.1:{child.port}/v1/setup?token={token}")

            status, raw = _bridge(server, env_id, "/v1/workspace/mkdir", method="POST",
                                  payload={"parent_path": ".", "name": "authed_dir"})
            assert status == 200, raw
            assert (tmp_path / "child_ws" / "authed_dir").is_dir()

            status, raw = _bridge(server, env_id, "/v1/workspace/nope.txt", method="DELETE")
            assert status != 401, raw

    def test_unknown_env_id_is_404(self, server):
        """桥只按 remote_envs.json 里的 id 寻址，不会被用来拨任意主机。"""
        for bad in ("http://nope.example:1", "tunnel:0000000000000000"):
            status, _raw = _bridge(server, bad, "/v1/env")
            assert status == 404, (bad, status)

    def test_unreachable_child_is_502(self, server):
        env_id = _seed_env(server, "http://127.0.0.1:1", "http://127.0.0.1:1/v1/setup")
        status, raw = _bridge(server, env_id, "/v1/workspace/list?path=.&restrict=0")
        assert status == 502, raw
        body = json.loads(raw)
        assert body.get("error") == "child_unreachable", body
        assert body.get("message"), body

    def test_child_verdict_reaches_the_browser(self, server, tmp_path):
        """子端自己的 4xx（文件不存在 / 上传缺片）必须原样回到浏览器。"""
        ws = str(tmp_path / "child_ws")
        with _real_child_server(tmp_path, workspace=ws) as (child, _data):
            _wait_child_hello(child)
            env_id = _add_env(server, f"http://127.0.0.1:{child.port}/v1/setup")
            status, raw = _bridge(
                server, env_id, "/v1/workspace/content?path=nope.txt&restrict=0")
            assert status == 400, (status, raw)
            assert "nope.txt" in raw.decode("utf-8", "replace")

    # -- terminal channels routed by env_id ---------------------------------
    #
    # The terminal used to be the one remote-execution surface the browser
    # dialed itself -- and briefly, one bridge socket per environment.  Both
    # shapes cost the page a connection per environment, and the browser's
    # per-origin pool is only ~6 wide, so the channel now rides the tab's
    # single ``WS /v1/terminals/ws`` and ``env_id`` says whose PTY runs it.

    def test_terminal_channel_routes_to_the_direct_child(self, server, tmp_path):
        """env_id 通道：浏览器拨母端，母端拨子端，PTY 在子端跑。"""
        ws = str(tmp_path / "child_ws")
        with _real_child_server(tmp_path, workspace=ws) as (child, _data):
            _wait_child_hello(child)
            env_id = _add_env(server, f"http://127.0.0.1:{child.port}/v1/setup")

            tag = f"dws-{int(time.time() * 1000)}"
            marker = f"DIRECT_ENV_ECHO_{tag}".encode("ascii")
            sock = _ws_browser_connect(server.port, "/v1/terminals/ws")
            try:
                buf = _ws_channel_echo(sock, tag, b"echo " + marker + b"\r", marker,
                                       env_id=env_id)
                assert marker in buf, f"echo marker not seen; got {buf[-300:]!r}"

                # The PTY really lives on the child (both servers here share
                # this process' terminal table, so "absent on the parent" is not
                # something the test can tell apart -- what matters is that the
                # child registered the session the channel asked for).
                status, child_terms = _request(child, "GET", "/v1/terminals")
                assert status == 200, child_terms
                assert [t for t in child_terms.get("terminals", [])
                        if t.get("session_id") == tag], child_terms
            finally:
                _ws_close(sock)

    def test_two_channels_of_one_environment_share_the_child_hop(self, server, tmp_path):
        """同一环境的两个终端共用母端→子端那一条连接（连接数关键所在）。

        Proof from the child side: both channels get their own PTY, and closing
        one leaves the other -- and the connection it rides -- working.
        """
        ws = str(tmp_path / "child_ws")
        with _real_child_server(tmp_path, workspace=ws) as (child, _data):
            _wait_child_hello(child)
            env_id = _add_env(server, f"http://127.0.0.1:{child.port}/v1/setup")

            stamp = int(time.time() * 1000)
            a, b = f"a-{stamp}", f"b-{stamp}"
            marker_a = f"ENV_CH_A_{stamp}".encode("ascii")
            marker_b = f"ENV_CH_B_{stamp}".encode("ascii")
            sock = _ws_browser_connect(server.port, "/v1/terminals/ws")
            try:
                buf = _ws_channel_echo(sock, a, b"echo " + marker_a + b"\r", marker_a,
                                       env_id=env_id)
                assert marker_a in buf, f"channel a: {buf[-300:]!r}"
                buf = _ws_channel_echo(sock, b, b"echo " + marker_b + b"\r", marker_b,
                                       env_id=env_id)
                assert marker_b in buf, f"channel b: {buf[-300:]!r}"

                # Close one channel; the other keeps talking on the same child
                # connection (it is refcounted per environment, not per channel).
                wsutil.ws_send_frame(sock, json.dumps({"op": "close", "c": a}).encode(),
                                     wsutil.OP_TEXT, mask=True)
                marker_c = f"ENV_CH_C_{stamp}".encode("ascii")
                buf = _ws_channel_echo_input(sock, b, b"echo " + marker_c + b"\r", marker_c)
                assert marker_c in buf, f"channel b after closing a: {buf[-300:]!r}"
            finally:
                _ws_close(sock)

    def test_child_token_is_injected_into_the_terminal_handshake(self, server, tmp_path):
        """子端开了密码：母端用登记 token 完成 WS 握手，浏览器不带子端凭据。"""
        ws = str(tmp_path / "child_ws")
        with _real_child_server(tmp_path, workspace=ws) as (child, _data):
            _wait_child_hello(child)
            status, config = _request(child, "POST", "/v1/auth/config", {"password": "child-pass"})
            assert status == 200, config
            token = config.get("setup_token", "")
            assert token
            env_id = _add_env(
                server, f"http://127.0.0.1:{child.port}/v1/setup?token={token}")

            tag = f"tok-{int(time.time() * 1000)}"
            marker = f"TOKEN_ECHO_{tag}".encode("ascii")
            sock = _ws_browser_connect(server.port, "/v1/terminals/ws")
            try:
                buf = _ws_channel_echo(sock, tag, b"echo " + marker + b"\r", marker,
                                       env_id=env_id)
                assert marker in buf, f"echo marker not seen; got {buf[-300:]!r}"
            finally:
                _ws_close(sock)

    def test_unauthorized_child_fails_the_channel_not_the_socket(self, server, tmp_path):
        """记录里没有 token 就拨不开子端：只在该通道上报错，共享 socket 继续服务。"""
        ws = str(tmp_path / "child_ws")
        with _real_child_server(tmp_path, workspace=ws) as (child, _data):
            _wait_child_hello(child)
            status, config = _request(child, "POST", "/v1/auth/config", {"password": "child-pass"})
            assert status == 200, config
            token = config.get("setup_token", "")
            assert token
            # Register with the token, then rewrite the record without it: the
            # parent can no longer authorize the child handshake, so the channel
            # answers an error frame -- the browser never learns a credential it
            # should not have, and the tab's other terminals (here: a local one)
            # keep running on the same socket.
            env_id = _add_env(
                server, f"http://127.0.0.1:{child.port}/v1/setup?token={token}")
            server._remote_env_manager.upsert(f"http://127.0.0.1:{child.port}/v1/setup")

            dead = f"nope-{int(time.time() * 1000)}"
            local = f"local-{int(time.time() * 1000)}"
            sock = _ws_browser_connect(server.port, "/v1/terminals/ws")
            try:
                _ws_open_channel(sock, dead, env_id=env_id)
                _ws_open_channel(sock, local)
                msg = _ws_wait_for(sock, lambda m: m.get("c") == dead and m.get("op") == "error")
                assert msg is not None, "no error frame for the unreachable environment"
                assert "unreachable" in msg.get("error", ""), msg
                opened = _ws_wait_for(sock, lambda m: m.get("c") == local and m.get("op") == "opened")
                assert opened is not None, opened
                marker = f"LOCAL_AFTER_FAILURE_{local}".encode("ascii")
                buf = _ws_channel_echo_input(sock, local, b"echo " + marker + b"\r", marker)
                assert marker in buf, buf[-300:]
            finally:
                _ws_close(sock)

    def test_unreachable_environment_reports_on_its_own_channel(self, server):
        """拨不通（连接被拒）也只影响该通道：不再有那一次失败的 HTTP 握手。"""
        env_id = _seed_env(server, "http://127.0.0.1:1", "http://127.0.0.1:1/v1/setup")
        dead = f"dead-{int(time.time() * 1000)}"
        sock = _ws_browser_connect(server.port, "/v1/terminals/ws")
        try:
            _ws_open_channel(sock, dead, env_id=env_id)
            msg = _ws_wait_for(sock, lambda m: m.get("c") == dead and m.get("op") == "error")
            assert msg is not None, "no error frame for the unreachable environment"
            assert "unreachable" in msg.get("error", ""), msg
        finally:
            _ws_close(sock)


