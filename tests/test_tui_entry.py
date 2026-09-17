"""Tests for tui.py (contract §4.4): argument parsing, env.json port
discovery, auth assembly branches, and the --register success/failure
paths.  ``ServiceClient`` is stubbed; no real service is contacted.
"""

import importlib.util
import json
import os
import sys
import urllib.error

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tui.chat_client import ApiError  # noqa: E402


def _load_tui_module():
    """Load tui.py (the repo-root script) without colliding with the
    ``tui`` package of the same name."""
    path = os.path.join(ROOT, "tui.py")
    spec = importlib.util.spec_from_file_location("tui_entry", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tui_mod = _load_tui_module()


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class StubClient:
    """Records construction args; behavior is injected via class attributes
    (reset by the ``stubbed`` fixture before every test)."""

    instances = []
    # -- per-test knobs (class level so tests can set them before main()) --
    auth_status_result = {"auth_enabled": False}
    auth_status_exc = None
    login_result = True
    login_exc = None
    tunnel_register_result = {"ok": True}
    tunnel_register_exc = None

    def __init__(self, base_url, token="", ssl_context=None):
        self.base_url = base_url
        self.token = token
        self.ssl_context = ssl_context
        self.last_password = None
        self.last_register_url = None
        StubClient.instances.append(self)

    def auth_status(self):
        if self.auth_status_exc is not None:
            raise self.auth_status_exc
        return self.auth_status_result

    def login(self, password):
        self.last_password = password
        if self.login_exc is not None:
            raise self.login_exc
        return self.login_result

    def tunnel_register(self, parent_url):
        self.last_register_url = parent_url
        if self.tunnel_register_exc is not None:
            raise self.tunnel_register_exc
        return self.tunnel_register_result


@pytest.fixture()
def stubbed(monkeypatch):
    """Stub ServiceClient in tui.py and tui.app.run/TuiOptions (the lazy
    import inside main() picks these up at call time)."""
    import tui.app as app_mod

    StubClient.instances = []
    StubClient.auth_status_result = {"auth_enabled": False}
    StubClient.auth_status_exc = None
    StubClient.login_result = True
    StubClient.login_exc = None
    StubClient.tunnel_register_result = {"ok": True}
    StubClient.tunnel_register_exc = None

    class FakeTuiOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    calls = {"run": []}

    def fake_run(client, opts):
        calls["run"].append((client, opts))
        return calls.get("code", 0)

    monkeypatch.setattr(tui_mod, "ServiceClient", StubClient)
    monkeypatch.setattr(app_mod, "run", fake_run)
    monkeypatch.setattr(app_mod, "TuiOptions", FakeTuiOptions)
    return calls


@pytest.fixture()
def runtime_tmp(monkeypatch, tmp_path):
    """Point AGENTS_RUNTIME_DIR at a fresh temp dir."""
    monkeypatch.setenv("AGENTS_RUNTIME_DIR", str(tmp_path))
    return tmp_path


def _write_env_json(runtime_dir, agents_url):
    payload = {"AGENTS_URL": agents_url} if agents_url is not None else {}
    with open(os.path.join(str(runtime_dir), "env.json"), "w",
              encoding="utf-8") as fh:
        json.dump(payload, fh)


def _last_client():
    return StubClient.instances[-1]


# ===========================================================================
# Argument parsing
# ===========================================================================

class TestArgParsing:
    def test_all_flags(self):
        args = tui_mod.build_parser().parse_args([
            "--port", "8123",
            "--token", "as_abc",
            "--login",
            "--session", "2024-05-05_120000",
            "--new",
            "--register", "http://parent:7988/?token=st_x",
            "--plain",
        ])
        assert args.port == 8123
        assert args.token == "as_abc"
        assert args.login is True
        assert args.session == "2024-05-05_120000"
        assert args.new is True
        assert args.register == "http://parent:7988/?token=st_x"
        assert args.plain is True

    def test_defaults(self):
        args = tui_mod.build_parser().parse_args([])
        assert args.port is None
        assert args.token == ""
        assert args.login is False
        assert args.session is None
        assert args.new is False
        assert args.register is None
        assert args.plain is False

    def test_invalid_port_rejected(self):
        with pytest.raises(SystemExit) as excinfo:
            tui_mod.main(["--port", "99999"])
        assert excinfo.value.code == 2


# ===========================================================================
# env.json port discovery (contract §3.2)
# ===========================================================================

class TestPortDiscovery:
    def test_default_port_without_env_json(self, runtime_tmp):
        base, ctx = tui_mod.resolve_base_url(None)
        assert base == "http://127.0.0.1:7988"
        assert ctx is None

    def test_env_json_url_port(self, runtime_tmp):
        _write_env_json(runtime_tmp, "http://0.0.0.0:8123/")
        base, ctx = tui_mod.resolve_base_url(None)
        assert base == "http://127.0.0.1:8123"
        assert ctx is None

    def test_env_json_https_scheme(self, runtime_tmp):
        _write_env_json(runtime_tmp, "https://my.domain:9443/")
        base, ctx = tui_mod.resolve_base_url(None)
        assert base == "https://127.0.0.1:9443"
        import ssl
        assert isinstance(ctx, ssl.SSLContext)

    def test_port_flag_wins_over_env_json(self, runtime_tmp):
        _write_env_json(runtime_tmp, "http://0.0.0.0:8123/")
        base, _ = tui_mod.resolve_base_url(8888)
        assert base == "http://127.0.0.1:8888"

    def test_port_flag_with_https_env_json_keeps_scheme(self, runtime_tmp):
        _write_env_json(runtime_tmp, "https://domain.example:9443")
        base, ctx = tui_mod.resolve_base_url(8000)
        assert base == "https://127.0.0.1:8000"
        assert ctx is not None

    def test_env_json_url_without_port_falls_back(self, runtime_tmp):
        _write_env_json(runtime_tmp, "http://my.domain")
        base, ctx = tui_mod.resolve_base_url(None)
        assert base == "http://127.0.0.1:7988"
        assert ctx is None

    def test_broken_env_json_ignored(self, runtime_tmp):
        with open(os.path.join(str(runtime_tmp), "env.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{not json")
        base, ctx = tui_mod.resolve_base_url(None)
        assert base == "http://127.0.0.1:7988"

    def test_runtime_dir_env_overrides_home(self, monkeypatch, tmp_path):
        other = tmp_path / "rt"
        other.mkdir()
        monkeypatch.setenv("AGENTS_RUNTIME_DIR", str(other))
        _write_env_json(other, "http://0.0.0.0:7001")
        assert tui_mod.runtime_dir() == str(other)
        base, _ = tui_mod.resolve_base_url(None)
        assert base == "http://127.0.0.1:7001"


# ===========================================================================
# Auth assembly branches (contract §4.4 step 3)
# ===========================================================================

class TestAuthAssembly:
    def test_auth_disabled_connects_directly(self, stubbed, runtime_tmp):
        code = tui_mod.main([])
        assert code == 0
        client = _last_client()
        assert client.token == ""
        assert stubbed["run"], "tui.app.run must be called"
        _, opts = stubbed["run"][-1]
        assert opts.kwargs["session_id"] is None
        assert opts.kwargs["new_session"] is False
        assert opts.kwargs["plain"] is False
        assert opts.kwargs["register_result"] is None

    def test_auth_enabled_non_interactive_without_token_errors(self,
                                                               stubbed,
                                                               runtime_tmp,
                                                               capsys,
                                                               monkeypatch):
        StubClient.auth_status_result = None  # 401
        # Force the non-interactive branch regardless of pytest's stdin.
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        code = tui_mod.main([])
        captured = capsys.readouterr()
        assert code == 1
        assert "--token" in captured.err and "--login" in captured.err
        assert not stubbed["run"]

    def test_auth_enabled_interactive_login_success(self, stubbed,
                                                    runtime_tmp, monkeypatch):
        StubClient.auth_status_result = None  # 401 → enabled
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(
            "getpass.getpass", lambda prompt="": "the-password"
        )
        code = tui_mod.main([])
        assert code == 0
        assert _last_client().last_password == "the-password"
        assert stubbed["run"]

    def test_login_failure_exits_1(self, stubbed, runtime_tmp, capsys,
                                   monkeypatch):
        StubClient.login_result = False
        monkeypatch.setattr(
            "getpass.getpass", lambda prompt="": "wrong-password"
        )
        code = tui_mod.main(["--login"])
        assert code == 1
        assert "密码错误" in capsys.readouterr().err
        assert not stubbed["run"]

    def test_login_getpass_eof_exits_1(self, stubbed, runtime_tmp, capsys,
                                       monkeypatch):
        """A closed stdin (EOF) during the prompt must not crash the entry."""

        def raising_getpass(prompt=""):
            raise EOFError

        monkeypatch.setattr("getpass.getpass", raising_getpass)
        code = tui_mod.main(["--login"])
        assert code == 1
        assert not stubbed["run"]

    def test_login_flag_forces_prompt(self, stubbed, runtime_tmp,
                                      monkeypatch):
        StubClient.auth_status_result = {"auth_enabled": False}
        monkeypatch.setattr(
            "getpass.getpass", lambda prompt="": "pw123456"
        )
        code = tui_mod.main(["--login"])
        assert code == 0
        assert _last_client().last_password == "pw123456"
        assert stubbed["run"]

    def test_login_api_error_exits_1(self, stubbed, runtime_tmp, capsys,
                                     monkeypatch):
        StubClient.login_exc = ApiError(500, "boom")
        monkeypatch.setattr(
            "getpass.getpass", lambda prompt="": "pw"
        )
        code = tui_mod.main(["--login"])
        assert code == 1
        assert "boom" in capsys.readouterr().err

    def test_token_skips_probe_and_login(self, stubbed, runtime_tmp,
                                         monkeypatch):
        monkeypatch.setattr(
            "getpass.getpass",
            lambda prompt="": pytest.fail("must not prompt with --token"),
        )
        code = tui_mod.main(["--token", "as_given"])
        assert code == 0
        assert _last_client().token == "as_given"
        assert stubbed["run"]

    def test_bad_token_warns_but_continues(self, stubbed, runtime_tmp,
                                           capsys):
        StubClient.auth_status_result = None  # 401 → token rejected
        code = tui_mod.main(["--token", "as_stale"])
        assert code == 0
        assert "401" in capsys.readouterr().err
        assert stubbed["run"]


# ===========================================================================
# Service unreachable (contract §4.4 step 4)
# ===========================================================================

class TestUnreachable:
    def test_connection_refused_exit_2(self, stubbed, runtime_tmp, capsys):
        StubClient.auth_status_exc = urllib.error.URLError(
            "[Errno 111] Connection refused"
        )
        code = tui_mod.main([])
        captured = capsys.readouterr()
        assert code == 2
        assert _last_client().base_url in captured.err
        assert "127.0.0.1" in captured.err
        assert not stubbed["run"]

    def test_unreachable_message_mentions_port_hint(self, stubbed,
                                                    runtime_tmp, capsys):
        StubClient.auth_status_exc = urllib.error.URLError("timed out")
        _write_env_json(runtime_tmp, "http://0.0.0.0:8123/")
        code = tui_mod.main([])
        captured = capsys.readouterr()
        assert code == 2
        assert "8123" in captured.err


# ===========================================================================
# --register headless registration (contract §4.4 step 5)
# ===========================================================================

class TestRegister:
    def test_register_success_continues_into_tui(self, stubbed, runtime_tmp,
                                                 capsys):
        code = tui_mod.main(["--register", "http://parent:7988/?token=st_p"])
        captured = capsys.readouterr()
        assert code == 0
        client = _last_client()
        assert client.last_register_url == "http://parent:7988/?token=st_p"
        assert "已注册到母端" in captured.out
        assert "http://parent:7988/?token=st_p" in captured.out
        _, opts = stubbed["run"][-1]
        assert opts.kwargs["register_result"] == {"ok": True}

    def test_register_api_error_exit_1(self, stubbed, runtime_tmp, capsys):
        StubClient.tunnel_register_exc = ApiError(
            400, "invalid parent URL: not a url"
        )
        code = tui_mod.main(["--register", "not-a-url"])
        captured = capsys.readouterr()
        assert code == 1
        assert "invalid parent URL" in captured.err
        assert not stubbed["run"]

    def test_register_unreachable_exit_1(self, stubbed, runtime_tmp, capsys):
        StubClient.tunnel_register_exc = urllib.error.URLError("no route")
        code = tui_mod.main(["--register", "http://parent:7988"])
        captured = capsys.readouterr()
        assert code == 1
        assert "no route" in captured.err
        assert not stubbed["run"]

    def test_register_failure_precedes_tui(self, stubbed, runtime_tmp,
                                           capsys):
        """Even with a valid TUI flow pending, a failed registration must
        stop the program before tui.app.run is reached."""
        StubClient.tunnel_register_exc = ApiError(400, "bad")
        code = tui_mod.main(["--register", "x", "--plain", "--new"])
        assert code == 1
        assert not stubbed["run"]


# ===========================================================================
# Handover to tui.app.run (contract §4.4 step 6)
# ===========================================================================

class TestRunHandover:
    def test_options_mapping(self, stubbed, runtime_tmp):
        code = tui_mod.main([
            "--port", "9000",
            "--session", "2024-05-05_120000",
            "--plain",
            "--new",
        ])
        assert code == 0
        client, opts = stubbed["run"][-1]
        assert client.base_url == "http://127.0.0.1:9000"
        assert opts.kwargs["session_id"] == "2024-05-05_120000"
        assert opts.kwargs["new_session"] is True
        assert opts.kwargs["plain"] is True

    def test_run_exit_code_propagates(self, stubbed, runtime_tmp):
        stubbed["code"] = 3
        assert tui_mod.main([]) == 3

    def test_keyboard_interrupt_maps_to_zero(self, stubbed, runtime_tmp,
                                              monkeypatch):
        import tui.app as app_mod

        def raising_run(client, opts):
            raise KeyboardInterrupt

        monkeypatch.setattr(app_mod, "run", raising_run)
        assert tui_mod.main([]) == 0
