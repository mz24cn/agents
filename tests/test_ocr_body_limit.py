#coding=utf-8
"""Tests for the request-body size protection in accessories/OCR_mcp.py.

Covers the OCR_MAX_REQUEST_BODY_SIZE parser, the mcp SDK body-limit patch,
and the OversizedBodyDrainMiddleware ASGI behavior (drain-then-413,
pass-through cases, pathological sizes).

The OCR MCP server runs in its own conda environment (fastmcp + paddleocr +
cv2), so this file is meant to be run there, e.g.:

    /root/conda/envs/OCR/bin/python tests/test_ocr_body_limit.py

The file is pytest-compatible as well (no pytest-specific fixtures are used),
and it self-skips collection (``__test__ = False``) in interpreters where the
OCR dependencies are missing, so the generic agent test suite stays green.
"""

import asyncio
import os
import sys
import types

sys.path.insert(0, str(
    __import__("pathlib").Path(__file__).resolve().parent.parent / "accessories"))

try:
    import OCR_mcp as obl
except Exception as _exc:  # OCR deps (fastmcp/paddleocr/cv2) not installed here
    obl = None
    __test__ = False  # tell pytest not to collect this module
    _IMPORT_ERROR = _exc


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _http_scope(headers):
    return {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }


class _InnerRecorder:
    """Minimal inner ASGI app that records what it was called with."""

    def __init__(self):
        self.called = False
        self.scope = None

    async def __call__(self, scope, receive, send):
        self.called = True
        self.scope = scope
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _drive(middleware, scope, body_chunks, more_body_flags):
    """Run the middleware with scripted body messages; return the sends.

    body_chunks/more_body_flags are consumed in order; when exhausted the
    next receive() yields http.disconnect.
    """
    sends = []
    state = {"i": 0}

    async def receive():
        i = state["i"]
        state["i"] += 1
        if i < len(body_chunks):
            return {
                "type": "http.request",
                "body": body_chunks[i],
                "more_body": more_body_flags[i] if i < len(more_body_flags) else False,
            }
        return {"type": "http.disconnect"}

    async def send(message):
        sends.append(message)

    _run(middleware(scope, receive, send))
    return sends


def _install_fake_sdk(with_param=True):
    """Install fake mcp / mcp.server / mcp.server.streamable_http_manager.

    Returns (FakeSM, restore); call restore() in a finally block so the real
    modules (if any) are put back.
    """
    if with_param:
        def _init(self, app=None, json_response=False, stateless=False,
                  max_request_body_size=4 * 1024 * 1024):
            self.kwargs = {"max_request_body_size": max_request_body_size,
                           "app": app}
    else:
        def _init(self, app=None, json_response=False, stateless=False):
            self.kwargs = {"app": app}

    class FakeSM:
        __init__ = _init

    names = ("mcp", "mcp.server", "mcp.server.streamable_http_manager")
    saved = {name: sys.modules.get(name) for name in names}
    fake_mcp = types.ModuleType("mcp")
    fake_server_pkg = types.ModuleType("mcp.server")
    fake_shm = types.ModuleType("mcp.server.streamable_http_manager")
    fake_shm.StreamableHTTPSessionManager = FakeSM
    fake_mcp.server = fake_server_pkg
    fake_server_pkg.streamable_http_manager = fake_shm
    sys.modules["mcp"] = fake_mcp
    sys.modules["mcp.server"] = fake_server_pkg
    sys.modules["mcp.server.streamable_http_manager"] = fake_shm

    def restore():
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
    return FakeSM, restore


def _env_var(name, value):
    """Temporarily set an env var; returns (old_value, restore)."""
    old = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value

    def restore():
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old
    return old, restore


# ------------------------------------------------------------------
# parse_body_limit
# ------------------------------------------------------------------

def test_parse_empty_returns_default():
    assert obl.parse_body_limit(None, 123) == 123
    assert obl.parse_body_limit("", 123) == 123
    assert obl.parse_body_limit("   ", 123) == 123


def test_parse_plain_bytes():
    assert obl.parse_body_limit("1048576", 0) == 1048576


def test_parse_k_suffix():
    assert obl.parse_body_limit("512K", 0) == 512 * 1024


def test_parse_m_suffix():
    assert obl.parse_body_limit("32M", 0) == 32 * 1024 * 1024
    assert obl.parse_body_limit("4m", 0) == 4 * 1024 * 1024


def test_parse_float_m():
    assert obl.parse_body_limit("1.5M", 0) == int(1.5 * 1024 * 1024)


def test_parse_invalid_returns_default():
    assert obl.parse_body_limit("banana", 777) == 777
    assert obl.parse_body_limit("12X", 777) == 777


def test_parse_zero_and_negative_mean_disabled():
    assert obl.parse_body_limit("0", 777) == 0
    assert obl.parse_body_limit("-5", 777) == -5


def test_env_reader():
    _, restore = _env_var("OCR_MAX_REQUEST_BODY_SIZE", None)
    try:
        assert obl.read_body_limit_from_env(999) == 999
        _env_var("OCR_MAX_REQUEST_BODY_SIZE", "8M")
        assert obl.read_body_limit_from_env(999) == 8 * 1024 * 1024
    finally:
        restore()


# ------------------------------------------------------------------
# OversizedBodyDrainMiddleware
# ------------------------------------------------------------------

def test_oversized_drains_then_413():
    inner = _InnerRecorder()
    mw = obl.OversizedBodyDrainMiddleware(inner, max_body_size=1000)
    sends = _drive(mw, _http_scope({"Content-Length": "5000"}),
                   [b"a" * 2500, b"b" * 2500], [True, False])
    assert not inner.called
    start, chunk = sends[0], sends[1]
    assert start["status"] == 413
    text = chunk["body"].decode()
    assert "Request body too large" in text
    assert "5,000" in text          # declared size
    assert "1,000" in text          # limit
    assert "OCR_MAX_REQUEST_BODY_SIZE" in text
    headers = dict(start["headers"])
    assert headers[b"connection"] == b"close"


def test_within_limit_passes_through():
    inner = _InnerRecorder()
    mw = obl.OversizedBodyDrainMiddleware(inner, max_body_size=10000)
    sends = _drive(mw, _http_scope({"Content-Length": "500"}),
                   [b"a" * 500], [False])
    assert inner.called
    assert sends[0]["status"] == 200


def test_exact_limit_passes_through():
    inner = _InnerRecorder()
    mw = obl.OversizedBodyDrainMiddleware(inner, max_body_size=500)
    sends = _drive(mw, _http_scope({"Content-Length": "500"}),
                   [b"a" * 500], [False])
    assert inner.called
    assert sends[0]["status"] == 200


def test_no_content_length_passes_through():
    inner = _InnerRecorder()
    mw = obl.OversizedBodyDrainMiddleware(inner, max_body_size=10)
    sends = _drive(mw, _http_scope({}), [], [])
    assert inner.called
    assert sends[0]["status"] == 200


def test_disabled_limit_passes_through():
    inner = _InnerRecorder()
    mw = obl.OversizedBodyDrainMiddleware(inner, max_body_size=0)
    sends = _drive(mw, _http_scope({"Content-Length": "999999"}),
                   [b"a" * 999999], [False])
    assert inner.called
    assert sends[0]["status"] == 200


def test_non_http_scope_passes_through():
    inner = _InnerRecorder()
    mw = obl.OversizedBodyDrainMiddleware(inner, max_body_size=10)
    scope = {"type": "websocket", "headers": []}
    sends = []

    async def receive():
        return {"type": "websocket.receive"}

    async def send(message):
        sends.append(message)

    _run(mw(scope, receive, send))
    assert inner.called


def test_client_disconnect_mid_upload_still_413():
    inner = _InnerRecorder()
    mw = obl.OversizedBodyDrainMiddleware(inner, max_body_size=1000)
    sends = []
    seq = iter([
        {"type": "http.request", "body": b"x" * 1500, "more_body": True},
        {"type": "http.disconnect"},  # client gives up mid-upload
    ])

    async def receive():
        try:
            return next(seq)
        except StopIteration:
            return {"type": "http.disconnect"}

    async def send(message):
        sends.append(message)

    _run(mw(_http_scope({"Content-Length": "1500"}), receive, send))
    assert not inner.called
    assert sends[0]["status"] == 413


def test_pathological_declared_size_skips_drain():
    old_ceiling = obl.DRAIN_CEILING
    obl.DRAIN_CEILING = 2000
    try:
        inner = _InnerRecorder()
        mw = obl.OversizedBodyDrainMiddleware(inner, max_body_size=1000)
        received = []

        async def receive():
            received.append(True)
            return {"type": "http.request", "body": b"a" * 5000, "more_body": False}

        sends = []

        async def send(message):
            sends.append(message)

        _run(mw(_http_scope({"Content-Length": "999999"}), receive, send))
        assert received == []  # nothing drained
        assert sends[0]["status"] == 413
        assert "999,999" in sends[1]["body"].decode()
    finally:
        obl.DRAIN_CEILING = old_ceiling


# ------------------------------------------------------------------
# raise_mcp_body_limit (SDK patch)
# ------------------------------------------------------------------

def test_sdk_patch_supported():
    cls, restore = _install_fake_sdk(with_param=True)
    try:
        ok = obl.raise_mcp_body_limit(32 * 1024 * 1024)
        assert ok is True
        # instantiating the (patched) session manager injects the new limit
        inst = cls(app="x")
        assert inst.kwargs.get("max_request_body_size") == 32 * 1024 * 1024
    finally:
        restore()


def test_sdk_patch_explicit_value_not_overridden():
    cls, restore = _install_fake_sdk(with_param=True)
    try:
        obl.raise_mcp_body_limit(32 * 1024 * 1024)
        # an explicitly provided limit must win over the injected default
        inst = cls(app="x", max_request_body_size=1234)
        assert inst.kwargs["max_request_body_size"] == 1234
    finally:
        restore()


def test_sdk_patch_unsupported_is_noop():
    cls, restore = _install_fake_sdk(with_param=False)
    try:
        ok = obl.raise_mcp_body_limit(32 * 1024 * 1024)
        assert ok is False
        assert not getattr(cls.__init__, "_ocr_body_limit_patched", False)
    finally:
        restore()


def test_sdk_patch_disabled_limit_is_noop():
    cls, restore = _install_fake_sdk(with_param=True)
    try:
        assert obl.raise_mcp_body_limit(0) is False
        assert obl.raise_mcp_body_limit(-1) is False
    finally:
        restore()


def test_sdk_patch_idempotent():
    cls, restore = _install_fake_sdk(with_param=True)
    try:
        assert obl.raise_mcp_body_limit(111) is True
        assert obl.raise_mcp_body_limit(222) is True  # already patched
        inst = cls(app="x")
        assert inst.kwargs.get("max_request_body_size") == 111  # first wins
    finally:
        restore()


# ------------------------------------------------------------------
# standalone runner (the OCR conda env has no pytest)
# ------------------------------------------------------------------

if __name__ == "__main__":
    if obl is None:
        print(f"SKIP: cannot import OCR_mcp in this interpreter: {_IMPORT_ERROR!r}")
        print("Run with the OCR service interpreter, e.g. "
              "/root/conda/envs/OCR/bin/python tests/test_ocr_body_limit.py")
        sys.exit(0)
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {exc!r}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
