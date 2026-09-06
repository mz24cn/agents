"""Tests for https access and SNI-based multi-certificate loading.

Covers:
- _resolve_bind_config: AGENTS_URL parsing, bind-address derivation,
  override priority and defaults;
- RuntimeHTTPServer.start()/start_background(): host/port/protocol
  resolution at start time (AGENTS_URL read from the process environment);
- TLS: SNI-based per-domain certificates from DATA_DIR/certs
  ({domain}.pem / {domain}.key) and the fallback certificate behaviour
  (default.* pair, generated self-signed cert, first available cert).
"""

from __future__ import annotations

import datetime
import json
import os
import socket
import ssl
import time
import urllib.request
from unittest import mock

import pytest

from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer, _resolve_bind_config

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    _HAVE_CRYPTOGRAPHY = True
except ImportError:  # pragma: no cover
    _HAVE_CRYPTOGRAPHY = False

needs_cryptography = pytest.mark.skipif(
    not _HAVE_CRYPTOGRAPHY,
    reason="the 'cryptography' package is required to generate test certificates",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_cert(certs_dir: str, domain: str, cn: str | None = None) -> None:
    """Write a self-signed {domain}.pem / {domain}.key pair to certs_dir."""
    os.makedirs(certs_dir, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn or domain)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(os.path.join(certs_dir, f"{domain}.pem"), "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(os.path.join(certs_dir, f"{domain}.key"), "wb") as fh:
        fh.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )


def _cert_cn(ssl_sock: ssl.SSLSocket) -> str:
    """Return the CN of the certificate presented by the server.

    Some Python builds require one extra no-op do_handshake() call before
    getpeercert() reports the handshake as done; retry through that path.
    Must be called while the connection is still open.
    """
    try:
        der = ssl_sock.getpeercert(binary_form=True)
    except ValueError:
        ssl_sock.do_handshake()
        der = ssl_sock.getpeercert(binary_form=True)
    cert = x509.load_der_x509_certificate(der)
    attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return attrs[0].value if attrs else ""


def _tls_probe(port: int, sni: str | None, request: bytes = b"GET /v1/models HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"):
    """Open a TLS connection (optional SNI), send *request*, read the reply.

    Returns (server_cert_cn, status_line, body_bytes).
    """
    raw = socket.create_connection(("127.0.0.1", port), timeout=15)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sock = ctx.wrap_socket(raw, server_hostname=sni) if sni else ctx.wrap_socket(raw)
    try:
        cn = _cert_cn(sock)
        sock.sendall(request)
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    status = data.split(b"\r\n", 1)[0]
    body = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
    return cn, status, body


def _http_get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return resp.status, resp.read()


@pytest.fixture()
def patched_paths(tmp_path):
    """Point the server's module-level path constants at a temp dir."""
    with mock.patch("runtime.server._MODELS_PATH", str(tmp_path / "models.json")), \
         mock.patch("runtime.server._TOOLS_PATH", str(tmp_path / "tools.json")), \
         mock.patch("runtime.server._MCP_SERVERS_PATH", str(tmp_path / "mcp_servers.json")), \
         mock.patch("runtime.server._PROMPT_TEMPLATES_PATH", str(tmp_path / "prompt_templates.json")), \
         mock.patch("runtime.server._ENV_PATH", str(tmp_path / "env.json")), \
         mock.patch("runtime.server._CERTS_DIR", str(tmp_path / "certs")), \
         mock.patch("runtime.server._AUTH_PATH", str(tmp_path / "auth_token.json")), \
         mock.patch("runtime.server._DATA_DIR", str(tmp_path)):
        yield tmp_path


def _make_server(patched_paths, **kwargs) -> RuntimeHTTPServer:
    runtime = Runtime(ModelRegistry(), ToolRegistry())
    srv = RuntimeHTTPServer(runtime)
    srv.start_background(host="127.0.0.1", port=0, **kwargs)
    return srv


# ---------------------------------------------------------------------------
# _resolve_bind_config
# ---------------------------------------------------------------------------


def test_unrecognized_scheme_is_ignored(monkeypatch):
	"""An AGENTS_URL with an unrecognized scheme is ignored entirely."""
	monkeypatch.setenv("AGENTS_URL", "ftp://domain:21")
	cfg = _resolve_bind_config()
	assert cfg == {
		"protocol": "http",
 "domain": "0.0.0.0",
  "bind_host": "0.0.0.0",
  "port": 7988,
  }


def test_no_agents_url_defaults_to_all_interfaces_http(monkeypatch, patched_paths):
	# Resolution defaults (no AGENTS_URL, no explicit start arguments).
	# The server itself binds an ephemeral port to avoid clashing with a
	# service that may already run on the default port 7988.
	monkeypatch.delenv("AGENTS_URL", raising=False)
	cfg = _resolve_bind_config()
	assert cfg == {
		"protocol": "http",
"domain": "0.0.0.0",
  "bind_host": "0.0.0.0",
  "port": 7988,
  }
	srv = RuntimeHTTPServer(Runtime(ModelRegistry(), ToolRegistry()))
	try:
		srv.start_background(port=0)
		assert srv.protocol == "http"
		assert srv._bind_host == "0.0.0.0"
		status, body = _http_get(f"http://127.0.0.1:{srv.port}/v1/models")
		assert status == 200
		assert json.loads(body) == {"models": []}
	finally:
		srv.stop()


class TestResolveBindConfig:
    def test_defaults_without_agents_url(self, monkeypatch):
        monkeypatch.delenv("AGENTS_URL", raising=False)
        cfg = _resolve_bind_config()
        assert cfg == {
            "protocol": "http",
           "domain": "0.0.0.0",
            "port": 7988,
            "bind_host": "0.0.0.0",
        }

    def test_agents_url_https_domain(self, monkeypatch):
        monkeypatch.setenv("AGENTS_URL", "https://domain:7988/")
        cfg = _resolve_bind_config()
        assert cfg["protocol"] == "https"
        assert cfg["domain"] == "domain"
        assert cfg["port"] == 7988
        # A domain name is not a bindable address: listen on all interfaces.
        assert cfg["bind_host"] == "0.0.0.0"

    def test_agents_url_localhost_binds_loopback(self, monkeypatch):
        monkeypatch.setenv("AGENTS_URL", "http://localhost:9000")
        cfg = _resolve_bind_config()
        assert cfg["protocol"] == "http"
        assert cfg["domain"] == "localhost"
        assert cfg["port"] == 9000
        assert cfg["bind_host"] == "127.0.0.1"

    def test_agents_url_ip_binds_ip(self, monkeypatch):
        monkeypatch.setenv("AGENTS_URL", "https://192.168.1.10:8443")
        cfg = _resolve_bind_config()
        assert cfg["protocol"] == "https"
        assert cfg["domain"] == "192.168.1.10"
        assert cfg["port"] == 8443
        assert cfg["bind_host"] == "192.168.1.10"

    def test_agents_url_without_port_uses_scheme_default(self, monkeypatch):
        monkeypatch.setenv("AGENTS_URL", "http://domain")
        assert _resolve_bind_config()["port"] == 80
        monkeypatch.setenv("AGENTS_URL", "https://domain")
        assert _resolve_bind_config()["port"] == 443


    def test_explicit_arguments_override_agents_url(self, monkeypatch):
        monkeypatch.setenv("AGENTS_URL", "https://domain:7988/")
        cfg = _resolve_bind_config(host="127.0.0.1", port=12345, protocol="http")
        assert cfg == {
            "protocol": "http",
            "domain": "127.0.0.1",
            "port": 12345,
            "bind_host": "127.0.0.1",
        }

    def test_partial_overrides_keep_url_values(self, monkeypatch):
        monkeypatch.setenv("AGENTS_URL", "https://domain:7988/")
        cfg = _resolve_bind_config(port=9000)
        assert cfg["port"] == 9000
        assert cfg["protocol"] == "https"
        assert cfg["domain"] == "domain"

    def test_invalid_protocol_override_is_ignored(self, monkeypatch):
        monkeypatch.delenv("AGENTS_URL", raising=False)
        cfg = _resolve_bind_config(protocol="ws")
        assert cfg["protocol"] == "http"


# ---------------------------------------------------------------------------
# start() time resolution (AGENTS_URL from the process environment)
# ---------------------------------------------------------------------------


class TestStartResolvesBindConfig:

    def test_agents_url_read_at_start(self, monkeypatch, patched_paths):
        # port 0 in the URL yields an ephemeral bind port; the URL otherwise
        # fully determines the resolution (no explicit start arguments).
        monkeypatch.setenv("AGENTS_URL", "http://127.0.0.1:0")
        srv = RuntimeHTTPServer(Runtime(ModelRegistry(), ToolRegistry()))
        try:
            srv.start_background()
            assert srv.protocol == "http"
            assert srv._bind_host == "127.0.0.1"
            status, body = _http_get(f"http://127.0.0.1:{srv.port}/v1/models")
            assert status == 200
        finally:
            srv.stop()

    def test_explicit_arguments_win_over_agents_url(self, monkeypatch, patched_paths):
        monkeypatch.setenv("AGENTS_URL", "https://domain:7988/")
        srv = RuntimeHTTPServer(Runtime(ModelRegistry(), ToolRegistry()))
        try:
            srv.start_background(host="127.0.0.1", port=0, protocol="http")
            assert srv.protocol == "http"
            assert srv._bind_host == "127.0.0.1"
            status, _ = _http_get(f"http://127.0.0.1:{srv.port}/v1/models")
            assert status == 200
        finally:
            srv.stop()


# ---------------------------------------------------------------------------
# SNI multi-certificate loading
# ---------------------------------------------------------------------------


@needs_cryptography
class TestSniMultiCert:
    def test_per_domain_certificates_selected_by_sni(self, patched_paths):
        certs_dir = str(patched_paths / "certs")
        _write_cert(certs_dir, "alpha.example")
        _write_cert(certs_dir, "beta.example")

        srv = _make_server(patched_paths, protocol="https")
        try:
            cn, status, body = _tls_probe(srv.port, "alpha.example")
            assert cn == "alpha.example"
            assert status == b"HTTP/1.1 200 OK"
            assert json.loads(body) == {"models": []}

            cn, status, body = _tls_probe(srv.port, "beta.example")
            assert cn == "beta.example"
            assert status == b"HTTP/1.1 200 OK"

            # SNI matching is case-insensitive.
            cn, status, _ = _tls_probe(srv.port, "ALPHA.EXAMPLE")
            assert cn == "alpha.example"
            assert status == b"HTTP/1.1 200 OK"
        finally:
            srv.stop()

    def test_unknown_sni_falls_back_but_stays_https(self, patched_paths):
        certs_dir = str(patched_paths / "certs")
        _write_cert(certs_dir, "alpha.example")

        srv = _make_server(patched_paths, protocol="https")
        try:
            # No certificate for "unknown.example": the fallback certificate
            # is used instead, https keeps working and the request succeeds.
            cn, status, body = _tls_probe(srv.port, "unknown.example")
            assert cn != "alpha.example"
            assert status == b"HTTP/1.1 200 OK"
            assert json.loads(body) == {"models": []}

            # Same for clients that do not send SNI at all.
            cn, status, _ = _tls_probe(srv.port, None)
            assert cn != "alpha.example"
            assert status == b"HTTP/1.1 200 OK"
        finally:
            srv.stop()

    def test_default_pair_is_the_fallback(self, patched_paths):
        certs_dir = str(patched_paths / "certs")
        _write_cert(certs_dir, "default", cn="fallback-default")
        _write_cert(certs_dir, "www.example")

        srv = _make_server(patched_paths, protocol="https")
        try:
            assert _tls_probe(srv.port, "www.example")[0] == "www.example"
            assert _tls_probe(srv.port, "nope.example")[0] == "fallback-default"
            assert _tls_probe(srv.port, None)[0] == "fallback-default"
        finally:
            srv.stop()

    def test_no_certs_uses_generated_self_signed_fallback(self, patched_paths):
        # Empty certs directory: a self-signed fallback certificate keeps
        # https reachable for every SNI name.
        os.makedirs(patched_paths / "certs", exist_ok=True)
        srv = _make_server(patched_paths, protocol="https")
        try:
            cn, status, body = _tls_probe(srv.port, "anything.example")
            assert status == b"HTTP/1.1 200 OK"
            assert json.loads(body) == {"models": []}
            assert cn  # some (self-signed) certificate was presented
        finally:
            srv.stop()

    def test_new_cert_picked_up_without_restart(self, monkeypatch, patched_paths):
        """Certificates dropped into the certs dir work after the short
        miss-recheck window expires."""
        import runtime.server as server_module

        certs_dir = str(patched_paths / "certs")
        _write_cert(certs_dir, "default", cn="fallback-default")

        srv = _make_server(patched_paths, protocol="https")
        try:
            # First miss for the new domain.
            assert _tls_probe(srv.port, "late.example")[0] == "fallback-default"
            # Wait out the (shortened) miss cache, then add the certificate.
            monkeypatch.setattr(server_module, "_SNI_MISS_RECHECK_SECONDS", 0.1)
            time.sleep(0.15)
            _write_cert(certs_dir, "late.example")
            cn, status, _ = _tls_probe(srv.port, "late.example")
            assert cn == "late.example"
            assert status == b"HTTP/1.1 200 OK"
        finally:
            srv.stop()

# ---------------------------------------------------------------------------
# Authorization for the /v1/terminals/ws terminal endpoint (security regression)
# ---------------------------------------------------------------------------


class TestWebSocketEndpointAuth:
    """The /v1/terminals/ws terminal endpoint spawns a real shell process and must be
    gated by the same authorization as the rest of /v1/*.  Regression
    coverage for the unauthenticated WebSocket RCE: the endpoint used to
    live at /ws (outside the /v1/ auth prefix), so a bare upgrade yielded a
    shell with no token/cookie at all.
    """

    @staticmethod
    def _ws_upgrade_status(port, cookie=None, path="/v1/terminals/ws"):
        """Raw WebSocket upgrade request; returns the HTTP status line."""
        import base64 as _b64

        raw = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            key = _b64.b64encode(os.urandom(16)).decode()
            cookie_line = f"Cookie: {cookie}\r\n" if cookie else ""
            raw.sendall(
                (
                    f"GET {path}?terminal_id=ws-auth-test HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\n"
                    f"{cookie_line}"
                    "Sec-WebSocket-Version: 13\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = raw.recv(4096)
                if not chunk:
                    break
                data += chunk
            return data.split(b"\r\n", 1)[0]
        finally:
            raw.close()

    @staticmethod
    def _post_json(srv, path_, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.port}{path_}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.headers

    @staticmethod
    def _login_cookie(srv, password):
        status, headers = TestWebSocketEndpointAuth._post_json(
            srv, "/v1/auth/login", {"password": password}
        )
        assert status == 200
        set_cookie = headers.get("Set-Cookie", "")
        assert "agent_service_session=" in set_cookie
        return (
            "agent_service_session="
            + set_cookie.split("agent_service_session=", 1)[1].split(";", 1)[0]
        )

    @staticmethod
    def _cleanup_terminals(srv, cookie=None, expect_id="ws-auth-test:auto", timeout=5.0):
        """Delete the terminal spawned by the test (PTY children outlive the
        WebSocket connection by design, so the test must not leave shells)."""
        deadline = time.monotonic() + timeout
        headers = {"Cookie": cookie} if cookie else {}
        terminals = []
        while time.monotonic() < deadline:
            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.port}/v1/terminals", headers=headers
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                terminals = json.loads(resp.read()).get("terminals", [])
            if any(t.get("terminal_id") == expect_id for t in terminals):
                break
            time.sleep(0.1)
        for t in terminals:
            if t.get("terminal_id") == expect_id:
                dreq = urllib.request.Request(
                    f"http://127.0.0.1:{srv.port}/v1/terminals/{t['terminal_id']}",
                    headers=headers,
                    method="DELETE",
                )
                urllib.request.urlopen(dreq, timeout=10)

    def test_ws_rejected_without_credentials(self, patched_paths):
        srv = _make_server(patched_paths)
        try:
            self._post_json(srv, "/v1/auth/config", {"password": "test-password-123"})
            assert self._ws_upgrade_status(srv.port) == b"HTTP/1.1 401 Unauthorized"
        finally:
            srv.stop()

    def test_legacy_ws_paths_do_not_upgrade(self, patched_paths):
        """The pre-rename /ws and /v1/ws locations must never complete a
        WebSocket handshake (they now fall through to static-file serving /
        the JSON 404 handler)."""
        srv = _make_server(patched_paths)
        try:
            for legacy in ("/ws", "/v1/ws"):
                status = self._ws_upgrade_status(srv.port, path=legacy)
                assert status != b"HTTP/1.1 101 Switching Protocols"
        finally:
            srv.stop()

    def test_ws_allowed_with_session_cookie(self, patched_paths):
        srv = _make_server(patched_paths)
        try:
            self._post_json(srv, "/v1/auth/config", {"password": "test-password-123"})
            cookie = self._login_cookie(srv, "test-password-123")
            assert (
                self._ws_upgrade_status(srv.port, cookie)
                == b"HTTP/1.1 101 Switching Protocols"
            )
            self._cleanup_terminals(srv, cookie)
        finally:
            srv.stop()

    def test_ws_open_when_auth_disabled(self, patched_paths):
        srv = _make_server(patched_paths)
        try:
            assert (
                self._ws_upgrade_status(srv.port)
                == b"HTTP/1.1 101 Switching Protocols"
            )
            self._cleanup_terminals(srv)
        finally:
            srv.stop()

    def test_oversized_json_body_rejected_with_413(self, patched_paths, monkeypatch):
        import runtime.handler_base as hb

        monkeypatch.setattr(hb, "_MAX_JSON_BODY_BYTES", 1024)
        srv = _make_server(patched_paths)
        try:
            body = json.dumps({"pad": "x" * 2048}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.port}/v1/env",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(req, timeout=15)
            assert excinfo.value.code == 413
        finally:
            srv.stop()

    @needs_cryptography
    def test_session_cookie_has_secure_flag_over_https(self, patched_paths):
        os.makedirs(patched_paths / "certs", exist_ok=True)
        srv = _make_server(patched_paths, protocol="https")
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(
                f"https://127.0.0.1:{srv.port}/v1/auth/config",
                data=json.dumps({"password": "test-password-123"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                assert resp.status == 200
            req = urllib.request.Request(
                f"https://127.0.0.1:{srv.port}/v1/auth/login",
                data=json.dumps({"password": "test-password-123"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                assert resp.status == 200
                set_cookie = resp.headers.get("Set-Cookie", "")
            assert "agent_service_session=" in set_cookie
            assert "Secure" in set_cookie
        finally:
            srv.stop()
