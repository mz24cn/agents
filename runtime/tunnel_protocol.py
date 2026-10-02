"""Parent↔child tunnel protocol — shared constants and frame helpers.

The tunnel data channel is a single WebSocket connection dialed *by the
child* toward the parent (``GET /v1/tunnel/ws?token=...``).  Three frame
families travel over it, all JSON text frames except HTTP bodies, which are
binary frames:

Control frames (text, JSON):
    C→P  {"op":"hello",      "tunnel_id":"<16hex>", "snapshot":{...}}
    P→C  {"op":"welcome","env_id":"tunnel:<16hex>"}
         {"op":"reject","reason":"not_registered"}
         {"op":"replaced"}            (an older connection of the same id)
         {"op":"deregister"}          (the parent removed this environment)
    P→C  {"op":"ping"}   →   C→P {"op":"pong"}

HTTP frames (text header + binary body, request/response symmetric):
    header {"op":"req",  "id":"<uuid>","method":"...","path":"...","headers":{...}}
    header {"op":"resp", "id":"<uuid>","status":200,"headers":{...}}
    followed by binary body frames; a **zero-length binary frame** terminates
    the body (streaming — no body length is pre-declared).

Stream frames (text; payload base64 — terminal traffic is small):
    P→C  {"op":"stream-open",  "sid":"<uuid>","path":"...","headers":{...}}
    C→P  {"op":"stream-ready", "sid":"..."} | {"op":"stream-error","sid":"...","status":404}
    both ways {"op":"stream-data","sid":"...","data":"<base64>"}
    P→C  {"op":"stream-close", "sid":"..."}

Zero third-party dependencies — only Python standard library.
"""

from __future__ import annotations

import json
import secrets
from typing import Iterator

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

ENV_ID_PREFIX = "tunnel:"
_TUNNEL_ID_LEN = 16  # 16 hex chars (64 bits)


def new_tunnel_id() -> str:
    """Generate a fresh child identity (16 hex chars, 64 bits of entropy)."""
    return secrets.token_hex(8)


def tunnel_env_id(tunnel_id: str) -> str:
    """Build the remote-env record id for a tunnel identity."""
    return f"{ENV_ID_PREFIX}{tunnel_id}"


def tunnel_id_from_env_id(env_id: str) -> str | None:
    """Extract the tunnel id from a record id, or None for non-tunnel ids."""
    if isinstance(env_id, str) and env_id.startswith(ENV_ID_PREFIX):
        tail = env_id[len(ENV_ID_PREFIX):]
        if len(tail) == _TUNNEL_ID_LEN:
            return tail
    return None


def is_tunnel_env_id(env_id: str) -> bool:
    return tunnel_id_from_env_id(env_id) is not None


# ---------------------------------------------------------------------------
# Timing / sizing
# ---------------------------------------------------------------------------

CHUNK_SIZE = 256 * 1024            # one WS binary frame per body chunk
INFLIGHT_MAX = 16                  # concurrent in-flight HTTP calls per tunnel
DEFAULT_CALL_TIMEOUT = 600.0       # long pushes / updates over the tunnel
PING_INTERVAL = 30.0               # parent liveness ping cadence
STALE_AFTER = 90.0                 # parent force-closes after this silence
RECONNECT_MIN = 1.0                # child backoff bounds (seconds)
RECONNECT_MAX = 30.0
SMALL_BODY_INLINE = 8 * 1024 * 1024  # child: keep bodies ≤ 8MB in memory, else temp file

# ---------------------------------------------------------------------------
# Frame opcodes
# ---------------------------------------------------------------------------

OP_HELLO = "hello"
OP_WELCOME = "welcome"
OP_REJECT = "reject"
OP_REPLACED = "replaced"
OP_DEREGISTER = "deregister"
OP_PING = "ping"
OP_PONG = "pong"

OP_REQ = "req"
OP_RESP = "resp"
# Per-chunk body framing (see CAP_RESP_CHUNK_ID).  A child that knows its
# parent routes bodies by per-chunk headers may send, instead of one
# uninterrupted header + bare-binary + empty-terminator sequence:
#     header {"op":"resp", ...}
#     header {"op":"resp-chunk","id":rid,"eof":false}  -> binary <data>
#     ...
#     header {"op":"resp-chunk","id":rid,"eof":true}   (no following binary)
# so that several response sequences can interleave on one tunnel (a long
# streaming response no longer blocks every other response on the lock).
OP_RESP_CHUNK = "resp-chunk"
# The same idea in the parent -> child direction (see CAP_REQ_CHUNK_ID, which
# the CHILD advertises): a request whose header carries ``"body": "chunked"``
# is followed by one such pair per body chunk instead of bare binary frames
# under a single active-body cursor:
#     header {"op":"req", ..., "body":"chunked"}
#     header {"op":"req-chunk","id":rid,"eof":false}   -> binary <data>
#     ...
#     header {"op":"req-chunk","id":rid,"eof":true}    (no following binary)
# A child that understands it routes each binary frame to the rid of the
# chunk header that preceded it, so the parent no longer has to hold the
# connection-wide send lock across a whole (possibly multi-minute) body: an
# upload can no longer stall every other request/response on the tunnel.
OP_REQ_CHUNK = "req-chunk"
REQ_BODY_CHUNKED = "chunked"

# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

# parent -> child caps (welcome frame)
CAP_RESP_CHUNK_ID = "resp-chunk-id"
# child -> parent caps (hello frame): the child routes routed request bodies
# (see OP_REQ_CHUNK).  The parent only uses that framing when the child says
# it can take it; old children keep receiving one atomic sequence.
CAP_REQ_CHUNK_ID = "req-chunk-id"

OP_STREAM_OPEN = "stream-open"
OP_STREAM_READY = "stream-ready"
OP_STREAM_ERROR = "stream-error"
OP_STREAM_DATA = "stream-data"
OP_STREAM_CLOSE = "stream-close"

# reject reasons
REJECT_NOT_REGISTERED = "not_registered"

# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------

def encode_frame(obj: dict) -> bytes:
    """Encode a JSON control/header frame (UTF-8 text frame payload)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_frame(data: bytes) -> dict:
    """Decode a JSON control/header frame. Raises ValueError on bad JSON."""
    obj = json.loads(data.decode("utf-8", errors="replace"))
    if not isinstance(obj, dict):
        raise ValueError("tunnel frame must be a JSON object")
    return obj


def iter_chunks(body: bytes, size: int = CHUNK_SIZE) -> Iterator[bytes]:
    """Split *body* into ≤ *size* chunks (yields nothing for empty bodies —
    the empty-body terminator is a separate zero-length frame)."""
    for offset in range(0, len(body), size):
        yield body[offset:offset + size]
