"""Self-signed X.509 certificates, standard library only.

Zero third-party dependencies -- only Python standard library.

The usual way to hand a throwaway certificate to a development TLS server is
the ``cryptography`` package (or shelling out to ``openssl``), and neither is
part of the standard library.  The fallback certificate that
``runtime.server._build_ssl_context`` serves when ``DATA_DIR/certs`` holds no
usable pair is therefore built here: a minimal ASN.1/DER writer plus RSA key
generation and RSASSA-PKCS1-v1_5 signing over SHA-256, on top of
``secrets`` / ``math`` / ``hashlib`` / ``base64`` / ``ipaddress``.

Scope is deliberately narrow -- one RSA key pair and one self-signed
certificate for a single name -- and the material is handed straight to
OpenSSL via ``ssl.SSLContext.load_cert_chain``, which parses it strictly: a
malformed encoding fails loudly at startup rather than producing a certificate
that only some clients accept.

The generated certificate is emphatically *not* trusted.  Its only job is to
keep an https endpoint reachable (with the usual browser warning) instead of
silently downgrading to http, so that real per-domain certificates dropped into
``DATA_DIR/certs`` have a chance to take over.
"""

import base64
import datetime
import hashlib
import ipaddress
import math
import secrets

# Miller-Rabin rounds applied to a candidate that survived trial division. Only
# the final (actually prime) candidate pays the full price -- a composite fails
# the first round with probability 3/4 -- so a generous count is nearly free:
# the chance of a pseudoprime sneaking through is 4**-16 per candidate.
_MR_ROUNDS = 16
# Trial division below this bound is folded into a single GCD against the
# product of those primes: one big GCD per candidate beats hundreds of
# divisions, and it is what keeps key generation at a fraction of a second.
_TRIAL_PRIME_LIMIT = 1000

_OID_RSA_ENCRYPTION = "1.2.840.113549.1.1.1"
_OID_SHA256_RSA = "1.2.840.113549.1.1.11"
_OID_SHA256 = "2.16.840.1.101.3.4.2.1"
_OID_COMMON_NAME = "2.5.4.3"
_OID_BASIC_CONSTRAINTS = "2.5.29.19"
_OID_SUBJECT_ALT_NAME = "2.5.29.17"

# Characters PrintableString may carry; anything else falls back to UTF8String.
_PRINTABLE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "'()+,-./:=? "
)

# String tags X.501 names may use; the value bytes follow the tag.
_STRING_TAGS = frozenset({0x0C, 0x13, 0x16, 0x17})  # UTF8/Printable/IA5/Teletex

# [trial_prime_product, primorial] -- computed once on first use.
_trial_product: list = []


# ---------------------------------------------------------------------------
# DER (Distinguished Encoding Rules) primitives
# ---------------------------------------------------------------------------


def _der_length(length: int) -> bytes:
    """Definite-length encoding (DER forbids the indefinite form)."""
    if length < 0x80:
        return bytes([length])
    body = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _der(tag: int, payload: bytes) -> bytes:
    return bytes([tag]) + _der_length(len(payload)) + payload


def _der_seq(*parts: bytes) -> bytes:
    return _der(0x30, b"".join(parts))


def _der_set(*parts: bytes) -> bytes:
    return _der(0x31, b"".join(parts))


def _der_int(value: int) -> bytes:
    if value == 0:
        return _der(0x02, b"\x00")
    body = value.to_bytes((value.bit_length() + 7) // 8, "big")
    if body[0] & 0x80:  # keep the two's-complement sign bit positive
        body = b"\x00" + body
    return _der(0x02, body)


def _der_bool(value: bool) -> bytes:
    return _der(0x01, b"\xff" if value else b"\x00")


def _der_null() -> bytes:
    return _der(0x05, b"")


def _der_octet_string(payload: bytes) -> bytes:
    return _der(0x04, payload)


def _der_bitstring(payload: bytes) -> bytes:
    # The leading 0x00 counts the unused bits in the last content byte.
    return _der(0x03, b"\x00" + payload)


def _oid_value(dotted: str) -> bytes:
    """Value bytes of a dotted OID (first two arcs merged, rest base-128)."""
    arcs = [int(arc) for arc in dotted.split(".")]
    body = bytearray()
    for arc in [arcs[0] * 40 + arcs[1]] + arcs[2:]:
        chunk = bytearray([arc & 0x7F])
        arc >>= 7
        while arc:
            chunk.append((arc & 0x7F) | 0x80)
            arc >>= 7
        body.extend(reversed(chunk))
    return bytes(body)


def _der_oid(dotted: str) -> bytes:
    return _der(0x06, _oid_value(dotted))


def _der_text(value: str) -> bytes:
    """X.501 string: PrintableString when it fits, UTF8String otherwise."""
    if set(value) <= _PRINTABLE_CHARS:
        return _der(0x13, value.encode("ascii"))
    return _der(0x0C, value.encode("utf-8"))


def _der_time(when: datetime.datetime) -> bytes:
    """UTCTime below 2050 (RFC 5280 requires it there), GeneralizedTime above."""
    if when.year < 2050:
        return _der(0x17, when.strftime("%y%m%d%H%M%SZ").encode("ascii"))
    return _der(0x18, when.strftime("%Y%m%d%H%M%SZ").encode("ascii"))


def _der_name(common_name: str) -> bytes:
    """A one-RDN Name: SEQUENCE OF SET OF SEQUENCE { OID, string }."""
    return _der_seq(
        _der_set(_der_seq(_der_oid(_OID_COMMON_NAME), _der_text(common_name)))
    )


def _der_algorithm_identifier() -> bytes:
    """sha256WithRSAEncryption, with the NULL parameters RFC 5280 mandates."""
    return _der_seq(_der_oid(_OID_SHA256_RSA), _der_null())


def _san_entry(name: str) -> bytes:
    """One subjectAltName GeneralName: iPAddress or dNSName.

    A client checks an IP URL against ``iPAddress`` entries only -- a
    ``dNSName`` holding ``"127.0.0.1"`` is ignored -- so a fallback certificate
    meant for an address has to carry the packed bytes if a client that decides
    to trust it is to get past hostname checking at all.
    """
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return _der(0x82, name.encode("ascii"))  # dNSName
    return _der(0x87, address.packed)  # iPAddress: 4 bytes, 16 for v6


# ---------------------------------------------------------------------------
# RSA key generation and signing (plain integers -- no external primitives)
# ---------------------------------------------------------------------------


def _trial_primes_product() -> int:
    """The product of every prime below ``_TRIAL_PRIME_LIMIT`` (cached)."""
    if _trial_product:
        return _trial_product[0]
    primes: list = []
    candidate = 2
    while candidate < _TRIAL_PRIME_LIMIT:
        if all(candidate % p for p in primes if p * p <= candidate):
            primes.append(candidate)
        candidate += 1
    product = 1
    for prime in primes:
        product *= prime
    _trial_product.append(product)
    return product


def _is_probable_prime(candidate: int, rounds: int = _MR_ROUNDS) -> bool:
    """Miller-Rabin, preceded by one trial-division GCD."""
    factor = math.gcd(candidate, _trial_primes_product())
    if factor != 1:
        # Either it shares a small factor, or it *is* one of the small primes.
        return candidate == factor
    # Write candidate-1 as 2**r * d with d odd.
    d = candidate - 1
    r = 0
    while not d & 1:
        d >>= 1
        r += 1
    for _ in range(rounds):
        a = secrets.randbelow(candidate - 3) + 2
        x = pow(a, d, candidate)
        if x == 1 or x == candidate - 1:
            continue
        for _ in range(r - 1):
            x = pow(x, 2, candidate)
            if x == candidate - 1:
                break
        else:
            return False
    return True


def _generate_prime(bits: int) -> int:
    """A random ``bits``-bit prime (top bit set, so the length is exact)."""
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(candidate):
            return candidate


def _modinv(value: int, modulus: int) -> int:
    """Modular inverse by the extended Euclidean algorithm (no 3.8+ ``pow``)."""
    old_r, r = value, modulus
    old_s, s = 1, 0
    while r:
        quotient = old_r // r
        old_r, r = r, old_r - quotient * r
        old_s, s = s, old_s - quotient * s
    if old_r != 1:
        raise ValueError("no modular inverse")
    return old_s % modulus


def _generate_rsa_key(key_size: int) -> dict:
    """A fresh RSA key pair, with CRT exponents so signing stays fast."""
    half = key_size // 2
    p = _generate_prime(half)
    q = _generate_prime(key_size - half)
    while q == p:
        q = _generate_prime(key_size - half)
    n = p * q
    e = 65537
    # Carmichael's lambda(n) yields a smaller -- and therefore faster -- d.
    p1, q1 = p - 1, q - 1
    gcd = p1
    other = q1
    while other:
        gcd, other = other, gcd % other
    lambda_n = p1 // gcd * q1
    d = _modinv(e, lambda_n)
    return {
        "n": n,
        "e": e,
        "d": d,
        "p": p,
        "q": q,
        "dp": d % p1,
        "dq": d % q1,
        "qinv": _modinv(q, p),
    }


def _sign_pkcs1_sha256(key: dict, message: bytes) -> bytes:
    """RSASSA-PKCS1-v1_5 signature over SHA-256(*message*)."""
    digest_info = _der_seq(
        _der_seq(_der_oid(_OID_SHA256), _der_null()),
        _der_octet_string(hashlib.sha256(message).digest()),
    )
    block = (key["n"].bit_length() + 7) // 8
    padding = block - len(digest_info) - 3
    if padding < 8:
        raise ValueError("RSA key too small to sign with SHA-256")
    encoded = b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), key["d"], key["n"])
    return signature.to_bytes(block, "big")


# ---------------------------------------------------------------------------
# PEM envelope
# ---------------------------------------------------------------------------


def _to_pem(label: str, der: bytes) -> bytes:
    # encodebytes() folds at 76 characters and ends with a newline.
    folded = base64.encodebytes(der).decode("ascii")
    return ("-----BEGIN %s-----\n%s-----END %s-----\n" % (label, folded, label)).encode(
        "ascii"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_self_signed_cert(
    common_name: str,
    key_size: int = 2048,
    valid_days: int = 3650,
) -> tuple:
    """Build an in-memory RSA key pair and a self-signed certificate for it.

    Returns ``(cert_pem, key_pem)``: a ``CERTIFICATE`` PEM and an unencrypted
    PKCS#1 ``RSA PRIVATE KEY`` PEM, both directly loadable by
    ``ssl.SSLContext.load_cert_chain``.

    *common_name* may be a DNS name or an IP literal; the subjectAltName entry
    is a dNSName and an iPAddress respectively.  Those two v3 extensions are all
    a self-signed serving certificate needs: subjectAltName (what modern clients
    actually match against) and basicConstraints with cA TRUE, so a user who
    decides to trust this fallback certificate can import it as an anchor.
    """
    common_name = (common_name or "").strip().rstrip(".").lower()
    if not common_name or len(common_name) > 255:
        raise ValueError("a certificate name of 1-255 characters is required")
    try:
        common_name.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("the certificate name must be ASCII: %r" % (common_name,))

    key = _generate_rsa_key(key_size)
    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = now - datetime.timedelta(days=1)
    not_after = now + datetime.timedelta(days=valid_days)

    subject = _der_name(common_name)
    subject_key_info = _der_seq(
        _der_seq(_der_oid(_OID_RSA_ENCRYPTION), _der_null()),
        _der_bitstring(_der_seq(_der_int(key["n"]), _der_int(key["e"]))),
    )
    extensions = _der_seq(
        _der_seq(
            _der_oid(_OID_BASIC_CONSTRAINTS),
            _der_bool(True),
            _der_octet_string(_der_seq(_der_bool(True))),
        ),
        _der_seq(
            _der_oid(_OID_SUBJECT_ALT_NAME),
            _der_octet_string(_der_seq(_san_entry(common_name))),
        ),
    )

    tbs_certificate = _der_seq(
        _der(0xA0, _der_int(2)),  # version v3
        _der_int(secrets.randbits(62) | 1),  # serialNumber, positive < 2**63
        _der_algorithm_identifier(),
        subject,  # issuer == subject: self-signed
        _der_seq(_der_time(not_before), _der_time(not_after)),
        subject,
        subject_key_info,
        _der(0xA3, extensions),
    )
    signature = _sign_pkcs1_sha256(key, tbs_certificate)
    certificate = _der_seq(
        tbs_certificate, _der_algorithm_identifier(), _der_bitstring(signature)
    )

    pkcs1_private_key = _der_seq(
        _der_int(0),
        _der_int(key["n"]),
        _der_int(key["e"]),
        _der_int(key["d"]),
        _der_int(key["p"]),
        _der_int(key["q"]),
        _der_int(key["dp"]),
        _der_int(key["dq"]),
        _der_int(key["qinv"]),
    )
    return (
        _to_pem("CERTIFICATE", certificate),
        _to_pem("RSA PRIVATE KEY", pkcs1_private_key),
    )


def common_name_of(cert_der: bytes) -> str:
    """The commonName of an X.509 certificate (DER), or "" when there is none.

    ``ssl`` will not report the peer certificate when the handshake skipped
    verification (``CERT_NONE`` -- the only mode a self-signed fallback can be
    probed in), so a caller that wants to know *which* certificate a server
    picked has to read it out of the DER itself.
    """
    common_name_oid = _oid_value(_OID_COMMON_NAME)
    for tag, body in _der_walk(cert_der):
        if tag != 0x30:  # candidate AttributeTypeAndValue SEQUENCE
            continue
        parts = _der_children(body)
        if len(parts) != 2 or parts[0] != (0x06, common_name_oid):
            continue
        string_tag, value = parts[1]
        if string_tag in _STRING_TAGS:
            return value.decode("utf-8", "replace")
    return ""


def _der_element(data: bytes, offset: int) -> tuple:
    """``(tag, value_bytes, offset_of_next_element)`` at *offset*."""
    tag = data[offset]
    offset += 1
    first = data[offset]
    offset += 1
    if first & 0x80:
        count = first & 0x7F
        if count == 0 or offset + count > len(data):
            raise ValueError("malformed DER length")
        length = int.from_bytes(data[offset:offset + count], "big")
        offset += count
    else:
        length = first
    if offset + length > len(data):
        raise ValueError("truncated DER element")
    return tag, data[offset:offset + length], offset + length


def _der_children(value: bytes) -> list:
    """``(tag, value_bytes)`` of the elements directly inside one constructed value."""
    elements: list = []
    offset = 0
    while offset < len(value):
        tag, body, offset = _der_element(value, offset)
        elements.append((tag, body))
    return elements


def _der_walk(data: bytes):
    """Yield ``(tag, value_bytes)`` for every element of a DER tree, depth first."""
    stack = [data]
    while stack:
        for tag, body in _der_children(stack.pop()):
            yield tag, body
            if tag & 0x20:  # constructed: its value is made of elements too
                stack.append(body)
