"""One connection carries many requests: an answered-but-unread body is a landmine.

A browser reuses a single TCP connection for a whole burst of calls, and
``http.server`` parses whatever comes next on that connection as the next request
line. So any answer written *without* consuming the request body plants the body
bytes in front of the next request:

    POST /v1/sessions/frames/hold HTTP/1.1        <- answered 404 by a server
    {...}                                          <-   build, body left unread
    {"conn":"tab-1","seconds":300}GET /v1/sessions/261117_112514 HTTP/1.1

The second request -- perfectly well formed as far as the browser is concerned --
is then rejected as ``501 Unsupported method ('{"conn":"tab-1","seconds":300}GET')``.
That is what "clicking a finished session in the sidebar made it disappear" looked
like: the ``GET /v1/sessions/{id}`` download failed with a 501 whose only clue was
the payload of an unrelated ``/frames/hold`` command.

These tests hold the wire contract with a raw socket (a client that pipelines on
purpose, i.e. exactly what a browser does by accident) and assert the second
request is judged on its own merits.
"""

import json
import socket
from unittest.mock import patch

import pytest

from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer


@pytest.fixture()
def server(tmp_path):
    with patch("runtime.server._MODELS_PATH", str(tmp_path / "models.json")), \
         patch("runtime.server._TOOLS_PATH", str(tmp_path / "tools.json")), \
         patch("runtime.server._PROMPT_TEMPLATES_PATH", str(tmp_path / "prompt_templates.json")), \
         patch("runtime.server._AUTH_PATH", str(tmp_path / "auth.json")), \
         patch("runtime.server._DATA_DIR", str(tmp_path)):
        srv = RuntimeHTTPServer(Runtime(ModelRegistry(), ToolRegistry()))
        srv.start_background(host="127.0.0.1", port=0)
        yield srv
        srv.stop()


# ---------------------------------------------------------------------------
# A hand-written client: one socket, several requests, no reconnection to hide
# a desync behind.
# ---------------------------------------------------------------------------


def _request(method: str, path: str, payload=None) -> bytes:
    headers = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n"
    body = b""
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers += "Content-Type: application/json\r\n"
        headers += f"Content-Length: {len(body)}\r\n"
    return (headers + "\r\n").encode("ascii") + body


def _round_trip(sock: socket.socket, raw: bytes) -> tuple[int, dict, bytes]:
    """Send one request, read exactly one response back."""
    sock.sendall(raw)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].decode("latin-1").split()[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    expected = int(headers.get("content-length") or 0)
    while len(body) < expected:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return status, headers, body


def _connect(server) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    return sock


# ---------------------------------------------------------------------------


def test_unknown_post_route_does_not_desync_the_next_request(server):
    """The 404 for a route this build does not have must not poison the socket.

    This is the shipped failure: a frontend bundle newer than the running backend
    sends ``POST /v1/sessions/frames/hold``; the backend answers 404 and stops
    caring, and the JSON body it never read becomes the first half of the next
    request line.
    """
    sock = _connect(server)
    try:
        status, _, body = _round_trip(
            sock,
            _request("POST", "/v1/sessions/frames/definitely-not-a-route",
                     {"conn": "tab-1", "seconds": 300}),
        )
        assert status == 404
        assert b"definitely-not-a-route" in body

        status2, _, _ = _round_trip(sock, _request("GET", "/v1/models"))
        # 501 would mean the leftover body was read as a request line.
        assert status2 == 200
    finally:
        sock.close()


def test_unknown_get_route_does_not_desync_the_next_request(server):
    """Same guard on the GET table, where no body is expected but not promised away."""
    sock = _connect(server)
    try:
        status, _, _ = _round_trip(
            sock, _request("GET", "/v1/no-such-endpoint", {"conn": "tab-1"}))
        assert status == 404
        status2, _, _ = _round_trip(sock, _request("GET", "/v1/models"))
        assert status2 == 200
    finally:
        sock.close()


def test_unknown_delete_route_does_not_desync_the_next_request(server):
    sock = _connect(server)
    try:
        status, _, _ = _round_trip(
            sock, _request("DELETE", "/v1/sessions/tab-1/no-such-sub", {"force": True}))
        assert status == 404
        status2, _, _ = _round_trip(sock, _request("GET", "/v1/models"))
        assert status2 == 200
    finally:
        sock.close()


def test_unauthorized_post_does_not_desync_the_next_request(server):
    """A 401 is an answer too: the credential-less body still has to be consumed.

    ``apiFetch`` retries every /v1/ call after re-authenticating, so an expired
    session turns into a stream of 401s -- each one a chance to desync the
    connection the retry (and every later call) is riding on.
    """
    auth_manager = server._server.auth_manager  # type: ignore[attr-defined]
    auth_manager.update_config(password="str0ng-passphrase")
    try:
        sock = _connect(server)
        try:
            status, _, body = _round_trip(
                sock,
                _request("POST", "/v1/sessions/frames/hold", {"conn": "tab-1", "seconds": 300}),
            )
            assert status == 401
            assert b"unauthorized" in body

            status2, _, _ = _round_trip(sock, _request("GET", "/v1/models"))
            # Unauthorized again -- but parsed as a GET, not as 501.
            assert status2 == 401
        finally:
            sock.close()
    finally:
        auth_manager.disable_auth()


def test_endpoint_that_ignores_its_body_leaves_the_connection_aligned(server):
    """Endpoints that never read a body have to discard one they are handed.

    ``POST /v1/sessions/{id}/read`` is called without a payload today; a client
    that sends one anyway must not be able to corrupt the next request.
    """
    sock = _connect(server)
    try:
        status, _, _ = _round_trip(
            sock, _request("POST", "/v1/sessions/261119_101010/read", {"note": "marked"}))
        assert status == 200
        status2, _, _ = _round_trip(sock, _request("GET", "/v1/models"))
        assert status2 == 200
    finally:
        sock.close()
