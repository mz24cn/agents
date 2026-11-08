"""Tests for the standard-library self-signed certificate generator.

The https fallback certificate used to be produced by the third-party
``cryptography`` package, which put a hole in the project's zero-third-party
dependency promise (**Validates: Requirements 7.1**).  It is now built by
``runtime/selfsigned.py`` on top of ``secrets``/``math``/``hashlib`` alone.

These tests are what make that swap trustworthy: OpenSSL itself has to parse,
verify and *use* the generated material in a real handshake, so a broken DER
encoding, a bad signature or a key/certificate mismatch fails here instead of
surfacing as an unexplained TLS error in the field.
"""

from __future__ import annotations

import base64
import datetime
import socket
import ssl
import threading

import pytest

from runtime.selfsigned import (
    _der_int,
    _der_length,
    _der_oid,
    _is_probable_prime,
    _oid_value,
    _san_entry,
    common_name_of,
    generate_self_signed_cert,
)


# ---------------------------------------------------------------------------
# Fixtures: one key pair / TLS server shared by the handshake tests
# (RSA-2048 key generation is the slow part -- about half a second each).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cert_pair() -> tuple:
    return generate_self_signed_cert("test.example")


@pytest.fixture(scope="module")
def tls_serve(tmp_path_factory):
    """A factory that serves a certificate pair over TLS and yields the port.

    Key generation is the slow part (~0.5 s per RSA-2048 pair), so the handshake
    tests share one listener per certificate rather than one per test.
    """
    started: list = []

    def serve(cert_pair: tuple, host: str = "127.0.0.1") -> int:
        cert_dir = tmp_path_factory.mktemp("tls")
        cert_path = cert_dir / "cert.pem"
        key_path = cert_dir / "key.pem"
        cert_path.write_bytes(cert_pair[0])
        key_path.write_bytes(cert_pair[1])

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        # Fails loudly if OpenSSL cannot parse the generated DER/PEM.
        ctx.load_cert_chain(str(cert_path), str(key_path))

        listener = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
        listener.bind((host, 0))
        listener.listen(8)
        listener.settimeout(0.2)
        stop = threading.Event()

        def serve_connections() -> None:
            while not stop.is_set():
                try:
                    raw, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                try:
                    with ctx.wrap_socket(raw, server_side=True) as tls:
                        tls.sendall(b"pong")
                except Exception:
                    pass  # the wrong-hostname probes below abort on purpose

        thread = threading.Thread(target=serve_connections, daemon=True)
        thread.start()
        started.append((listener, stop, thread))
        return listener.getsockname()[1]

    yield serve
    for listener, stop, thread in started:
        stop.set()
        listener.close()
        thread.join(timeout=2)


@pytest.fixture(scope="module")
def tls_server(cert_pair, tls_serve) -> int:
    """A TLS server serving the generated DNS-name certificate."""
    return tls_serve(cert_pair)


@pytest.fixture(scope="module")
def ip_cert_pair() -> tuple:
    """A certificate for an IP address, which is what https://<ip> needs."""
    return generate_self_signed_cert("127.0.0.1")


@pytest.fixture(scope="module")
def ip_tls_server(ip_cert_pair, tls_serve) -> int:
    return tls_serve(ip_cert_pair)


def _connect(port: int, server_hostname: str, trust_pem: str | None = None):
    """Client handshake; *trust_pem* enables full verification."""
    if trust_pem is None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cadata=trust_pem)
    return ctx.wrap_socket(
        socket.create_connection(("127.0.0.1", port), timeout=10),
        server_hostname=server_hostname,
    )


# ---------------------------------------------------------------------------
# DER primitives: known-answer vectors taken from RFC 5280 / X.509 examples
# ---------------------------------------------------------------------------


def test_oid_encoding_matches_known_vectors():
    assert _oid_value("2.5.29.17") == bytes.fromhex("551d11")  # subjectAltName
    assert _der_oid("1.2.840.113549.1.1.11") == bytes.fromhex(
        "06092a864886f70d01010b"
    )  # sha256WithRSAEncryption
    # A two-digit arc above 127 needs the base-128 continuation bit.
    assert _oid_value("1.2.840.113549") == bytes.fromhex("2a864886f70d")


def test_integer_encoding_keeps_the_sign_bit_positive():
    assert _der_int(0) == b"\x02\x01\x00"
    assert _der_int(127) == b"\x02\x01\x7F"
    assert _der_int(128) == b"\x02\x02\x00\x80"  # pad byte, not a negative number
    assert _der_int(255) == b"\x02\x02\x00\xFF"


def test_lengths_use_the_definite_form():
    assert _der_length(0) == b"\x00"
    assert _der_length(0x7F) == b"\x7F"
    assert _der_length(0x80) == b"\x81\x80"
    assert _der_length(300) == b"\x82\x01\x2C"


def test_prime_test_separates_primes_from_composites():
    assert _is_probable_prime(104729)  # prime
    assert _is_probable_prime((1 << 61) - 1)  # Mersenne prime
    assert not _is_probable_prime(104730)
    assert not _is_probable_prime((1 << 61) - 2)


# ---------------------------------------------------------------------------
# The generated material
# ---------------------------------------------------------------------------


def test_pem_envelope_is_wellformed_and_decodes(cert_pair):
    cert_pem, key_pem = cert_pair
    assert cert_pem.startswith(b"-----BEGIN CERTIFICATE-----")
    assert cert_pem.rstrip().endswith(b"-----END CERTIFICATE-----")
    assert key_pem.startswith(b"-----BEGIN RSA PRIVATE KEY-----")
    assert key_pem.rstrip().endswith(b"-----END RSA PRIVATE KEY-----")
    body = cert_pem.decode("ascii").split("-----")[2]
    der = base64.b64decode(body)
    assert der[0] == 0x30  # the outer Certificate SEQUENCE
    assert len(der) > 500


def test_generated_certificate_carries_the_name(cert_pair):
    cert_pem, _ = cert_pair
    der = base64.b64decode(cert_pem.decode("ascii").split("-----")[2])
    assert common_name_of(der) == "test.example"
    assert common_name_of(b"\x30\x00") == ""  # an empty SEQUENCE carries no name


def test_handshake_verified_against_the_generated_certificate(cert_pair, tls_server):
    """The certificate must stand on its own as the only trust anchor.

    This is the end-to-end check that the encoding is right: OpenSSL validates
    the self-signature, the SPKI against the private key, the validity window
    and the subjectAltName, and enforces hostname matching.
    """
    with _connect(tls_server, "test.example", trust_pem=cert_pair[0].decode()) as sock:
        assert sock.recv(4) == b"pong"
        peer = sock.getpeercert()
    assert peer["subjectAltName"] == (("DNS", "test.example"),)
    assert "test.example" in str(peer["subject"])
    # Validity window: one day back, ten years forward by default.
    not_after = datetime.datetime.strptime(peer["notAfter"], "%b %d %H:%M:%S %Y %Z")
    today = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    assert not_after > today + datetime.timedelta(days=3600)


def test_handshake_rejects_a_different_hostname(cert_pair, tls_server):
    """The subjectAltName really is what clients match against."""
    with pytest.raises(ssl.SSLCertVerificationError):
        with _connect(tls_server, "other.example", trust_pem=cert_pair[0].decode()):
            pass


# ---------------------------------------------------------------------------
# IP-address names: iPAddress, not a dNSName holding digits
# ---------------------------------------------------------------------------


def test_san_entry_matches_the_known_encodings():
    """GeneralName choices from RFC 5280 §4.2.1.6 ([2]=dNSName, [7]=iPAddress)."""
    assert _san_entry("test.example") == b"\x82\x0c" + b"test.example"
    assert _san_entry("127.0.0.1") == b"\x87\x04\x7f\x00\x00\x01"
    assert _san_entry("::1") == b"\x87\x10" + bytes(15) + b"\x01"


def test_ip_address_certificate_verifies_over_an_ip_url(ip_cert_pair, ip_tls_server):
    """``https://127.0.0.1`` has to work with verification on, not only skipped.

    Encoding the address as a dNSName -- which is what "put the name in the SAN"
    naively means -- makes OpenSSL report *IP address mismatch* even when that
    very certificate is the client's trust anchor, so the fallback would be
    unusable for anyone who chose to trust it.
    """
    with _connect(ip_tls_server, "127.0.0.1", trust_pem=ip_cert_pair[0].decode()) as sock:
        assert sock.recv(4) == b"pong"
        peer = sock.getpeercert()
        der = sock.getpeercert(binary_form=True)
    assert peer["subjectAltName"] == (("IP Address", "127.0.0.1"),)
    assert common_name_of(der) == "127.0.0.1"
    assert b"\x82\x09" + b"127.0.0.1" not in der  # never spelled as a dNSName


def test_dns_certificate_is_not_accepted_for_an_ip_url(cert_pair, tls_server):
    """Guards the assertion above: IP matching really is enforced, not skipped."""
    with pytest.raises(ssl.SSLCertVerificationError):
        with _connect(tls_server, "127.0.0.1", trust_pem=cert_pair[0].decode()):
            pass


def test_unverified_client_can_read_the_certificate_back(tls_server):
    """CERT_NONE probing (the only way to reach a self-signed cert) still works."""
    with _connect(tls_server, "test.example") as sock:
        assert sock.recv(4) == b"pong"
        der = sock.getpeercert(binary_form=True)
    assert common_name_of(der) == "test.example"


def test_every_call_produces_a_fresh_key_pair():
    cert_a, key_a = generate_self_signed_cert("rotate.example", key_size=1024)
    cert_b, key_b = generate_self_signed_cert("rotate.example", key_size=1024)
    assert cert_a != cert_b
    assert key_a != key_b


def test_names_that_cannot_be_certified_are_rejected():
    for bad in ("", "   ", "x" * 256, "日本名前.example"):
        with pytest.raises(ValueError):
            generate_self_signed_cert(bad)
