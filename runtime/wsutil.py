"""Minimal WebSocket (RFC 6455) frame primitives shared by the terminal
WebSocket endpoint and the parent/child tunnel data channel.

Server-side helpers: handshake response generation, frame encode/decode with
a robust full-read loop (a single ``recv()`` may return a short payload, so
all reads loop until the expected byte count is reached), and send helpers
for text / binary / close / pong.

The client side (outbound dials from a child environment) lives in
``runtime.wsclient`` and reuses the same frame layout and the accept-key
computation.

Zero third-party dependencies — only Python standard library.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Opcodes
# ---------------------------------------------------------------------------
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_accept_key(key: str) -> str:
    """Compute the Sec-WebSocket-Accept value for a client key."""
    digest = hashlib.sha1((key + _WS_MAGIC).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("utf-8")


def ws_handshake_response(key: str) -> str:
    """Build the 101 Switching Protocols response for a WebSocket handshake."""
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {ws_accept_key(key)}\r\n"
        "\r\n"
    )


# ---------------------------------------------------------------------------
# Frame encoding / decoding
# ---------------------------------------------------------------------------

def _encode_frame(data: bytes, opcode: int, mask: bool = False) -> bytes:
    """Encode one unfragmented WebSocket frame.

    ``mask`` must be True for client→server frames (RFC 6455 §5.3) and False
    for server→client frames.
    """
    length = len(data)
    header = bytearray([0x80 | (opcode & 0x0F)])  # FIN + opcode
    if mask:
        if length <= 125:
            header.append(0x80 | length)
        elif length <= 65535:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(os.urandom(4))
    else:
        if length <= 125:
            header.append(length)
        elif length <= 65535:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
    if mask:
        key = header[-4:]
        payload = bytearray(b ^ key[i % 4] for i, b in enumerate(data))
    else:
        payload = data
    return bytes(header) + bytes(payload)


def ws_send_frame(sock, data: bytes, opcode: int = OP_TEXT, mask: bool = False) -> None:
    """Send a single unfragmented frame. Raises OSError on failure."""
    sock.sendall(_encode_frame(data, opcode, mask=mask))


def ws_send_text(sock, text: str, mask: bool = False) -> None:
    ws_send_frame(sock, text.encode("utf-8", errors="replace"), OP_TEXT, mask=mask)


def ws_send_binary(sock, data: bytes, mask: bool = False) -> None:
    ws_send_frame(sock, data, OP_BINARY, mask=mask)


def ws_send_close(sock, code: int = 1000, mask: bool = False) -> None:
    ws_send_frame(sock, struct.pack("!H", code & 0xFFFF), OP_CLOSE, mask=mask)


def ws_send_pong(sock, payload: bytes = b"", mask: bool = False) -> None:
    ws_send_frame(sock, payload, OP_PONG, mask=mask)


def ws_send_ping(sock, payload: bytes = b"", mask: bool = False) -> None:
    ws_send_frame(sock, payload, OP_PING, mask=mask)


def _recv_exact(sock, n: int) -> bytes:
    """Read exactly *n* bytes (loops over short reads). Raises on EOF/short."""
    if n <= 0:
        return b""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise OSError("connection closed mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def ws_recv_frame(sock, expect_masked: bool = False) -> Optional[Tuple[int, bytes]]:
    """Receive one (unfragmented) frame.

    Returns ``(opcode, payload)`` or ``None`` when the connection is closed
    (close frame, EOF, or protocol error).  Fragmented frames (continuation
    opcodes) are not produced anywhere in this codebase and are treated as
    protocol errors (→ None).  Close frames are consumed here (a close reply
    is best-effort; the caller simply stops reading), so callers never see
    opcode 8 and can treat None uniformly as "connection over".

    ``expect_masked`` should be True when reading from a browser / WS client
    (client→server frames are always masked per RFC 6455) and False when
    reading from a server.  A mismatch is tolerated: the actual mask bit in
    the frame header is authoritative.
    """
    try:
        header = _recv_exact(sock, 2)
    except OSError:
        return None
    b1, b2 = header[0], header[1]
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    if not fin:
        # Continuation frames are unsupported; drain is pointless — bail out.
        return None
    if opcode == OP_CLOSE:
        # Consume the close frame: reply best-effort, then report closed.
        try:
            ws_send_frame(sock, b"", OP_CLOSE)
        except OSError:
            pass
        return None

    payload_len = b2 & 0x7F
    if payload_len == 126:
        payload_len = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif payload_len == 127:
        payload_len = struct.unpack("!Q", _recv_exact(sock, 8))[0]

    if masked:
        masking_key = _recv_exact(sock, 4)
    else:
        masking_key = b""

    raw = _recv_exact(sock, payload_len)
    if masked:
        payload = bytearray(b ^ masking_key[i % 4] for i, b in enumerate(raw))
    else:
        payload = raw
    return opcode, bytes(payload)
