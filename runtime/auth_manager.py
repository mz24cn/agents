"""Lightweight service-level authorization for Agent Service.

Zero third-party dependencies.  Authentication is disabled when the auth
configuration file is missing or contains no password/api-key hashes.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Optional


COOKIE_NAME = "agent_service_session"
DEFAULT_COOKIE_TTL_SECONDS = 7 * 24 * 60 * 60
SETUP_TOKEN_TTL_SECONDS = 60 * 60
SETUP_TOKEN_PREFIX = "st_"
SETUP_TOKEN_EXP_BYTES = 4
SETUP_TOKEN_NONCE_BYTES = 8
SETUP_TOKEN_SIG_BYTES = 16
SETUP_TOKEN_CONTEXT = b"setup-v2."

COOKIE_TTL_OPTIONS = [
    60 * 60,                  # 1 hour
    6 * 60 * 60,              # 6 hours
    12 * 60 * 60,             # 12 hours
    24 * 60 * 60,             # 1 day
    7 * 24 * 60 * 60,         # 7 days
    30 * 24 * 60 * 60,        # 30 days
    90 * 24 * 60 * 60,        # 90 days
    180 * 24 * 60 * 60,       # 180 days
    365 * 24 * 60 * 60,       # 1 year
]


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def _utc_iso(ts: int) -> str:
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")


class AuthManager:
    """Small auth manager backed by ``auth_token.json``.

    Missing file means auth disabled.  Corrupt JSON means fail-closed.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._enabled = False
        self._fail_closed = False
        self._password_hash = ""
        self._api_key_hash = ""
        self._session_secret = ""
        self._setup_secret = ""
        self._cookie_ttl_seconds = DEFAULT_COOKIE_TTL_SECONDS
        self._updated_at = ""
        self.load()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            if not os.path.exists(self.path):
                self._reset_disabled()
                return
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                self._reset_disabled()
                self._enabled = True
                self._fail_closed = True
                return

            password_hash = str(data.get("password_hash") or "")
            api_key_hash = str(data.get("api_key_hash") or "")
            if not password_hash or not api_key_hash:
                self._reset_disabled()
                return

            self._enabled = True
            self._fail_closed = False
            self._password_hash = password_hash
            self._api_key_hash = api_key_hash
            self._session_secret = str(data.get("session_secret") or secrets.token_urlsafe(32))
            self._setup_secret = str(data.get("setup_secret") or secrets.token_urlsafe(32))
            ttl = int(data.get("cookie_ttl_seconds") or DEFAULT_COOKIE_TTL_SECONDS)
            self._cookie_ttl_seconds = ttl if ttl in COOKIE_TTL_OPTIONS else DEFAULT_COOKIE_TTL_SECONDS
            self._updated_at = str(data.get("updated_at") or "")

    def _reset_disabled(self) -> None:
        self._enabled = False
        self._fail_closed = False
        self._password_hash = ""
        self._api_key_hash = ""
        self._session_secret = ""
        self._setup_secret = ""
        self._cookie_ttl_seconds = DEFAULT_COOKIE_TTL_SECONDS
        self._updated_at = ""

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def is_fail_closed(self) -> bool:
        with self._lock:
            return self._fail_closed

    def cookie_ttl_seconds(self) -> int:
        with self._lock:
            return self._cookie_ttl_seconds

    def status(self, include_setup_token: bool = False) -> dict:
        with self._lock:
            data = {
                "has_password": bool(self._enabled and not self._fail_closed),
                "auth_enabled": bool(self._enabled),
                "fail_closed": bool(self._fail_closed),
                "cookie_ttl_seconds": self._cookie_ttl_seconds,
                "cookie_ttl_options": list(COOKIE_TTL_OPTIONS),
            }
            if include_setup_token and self._enabled and not self._fail_closed:
                token, expires_at = self.create_setup_token()
                data["setup_token"] = token
                data["setup_token_expires_at"] = expires_at
            else:
                data["setup_token"] = ""
                data["setup_token_expires_at"] = ""
            return data

    # ------------------------------------------------------------------
    # Password/API key
    # ------------------------------------------------------------------

    @staticmethod
    def validate_password(password: str) -> None:
        if not isinstance(password, str) or not (6 <= len(password) <= 20):
            raise ValueError("Password must be 6-20 characters")
        if any(ord(ch) < 32 for ch in password):
            raise ValueError("Password must not contain control characters")

    def verify_password(self, password: str) -> bool:
        with self._lock:
            if not self._enabled or self._fail_closed:
                return False
            return self._verify_password_hash(password, self._password_hash)

    def verify_api_key(self, api_key: str) -> bool:
        with self._lock:
            if not self._enabled or self._fail_closed:
                return False
            return self._verify_secret_hash(api_key, self._api_key_hash)

    def update_config(self, password: Optional[str] = None, cookie_ttl_seconds: Optional[int] = None) -> dict:
        """Update password and/or cookie TTL.

        Returns a dict that includes ``api_key`` only when a new password was
        set and therefore a new API key was generated.
        """
        with self._lock:
            if cookie_ttl_seconds is not None:
                if int(cookie_ttl_seconds) not in COOKIE_TTL_OPTIONS:
                    raise ValueError("Invalid cookie TTL")
                self._cookie_ttl_seconds = int(cookie_ttl_seconds)

            api_key = ""
            if password is not None and password != "":
                self.validate_password(password)
                api_key = "as_" + secrets.token_urlsafe(32)
                self._password_hash = self._hash_password(password)
                self._api_key_hash = self._hash_secret(api_key)
                # Rotate secrets so old cookies and old setup tokens expire.
                self._session_secret = secrets.token_urlsafe(32)
                self._setup_secret = secrets.token_urlsafe(32)
                self._enabled = True
                self._fail_closed = False

            if not self._enabled:
                # Keep first-run no-auth mode completely silent unless a password
                # is actually provided.
                return self.status(include_setup_token=False)

            self._updated_at = _dt.datetime.now(tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")
            self._save_locked()
            result = self.status(include_setup_token=True)
            if api_key:
                result["api_key"] = api_key
            return result

    def disable_auth(self) -> dict:
        """Disable authorization by removing the auth configuration file."""
        with self._lock:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RuntimeError(f"Failed to remove auth config: {exc}") from exc
            self._reset_disabled()
            return self.status(include_setup_token=False)

    def _save_locked(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        data = {
            "version": 1,
            "password_hash": self._password_hash,
            "api_key_hash": self._api_key_hash,
            "session_secret": self._session_secret,
            "setup_secret": self._setup_secret,
            "cookie_ttl_seconds": self._cookie_ttl_seconds,
            "updated_at": self._updated_at,
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _hash_password(password: str) -> str:
        iterations = 260_000
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return f"pbkdf2_sha256${iterations}${_b64e(salt)}${_b64e(digest)}"

    @staticmethod
    def _verify_password_hash(password: str, encoded: str) -> bool:
        try:
            scheme, iterations_s, salt_s, digest_s = encoded.split("$", 3)
            if scheme != "pbkdf2_sha256":
                return False
            salt = _b64d(salt_s)
            expected = _b64d(digest_s)
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations_s))
            return hmac.compare_digest(actual, expected)
        except Exception:
            return False

    @staticmethod
    def _hash_secret(secret: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.sha256(salt + secret.encode("utf-8")).digest()
        return f"sha256${_b64e(salt)}${_b64e(digest)}"

    @staticmethod
    def _verify_secret_hash(secret: str, encoded: str) -> bool:
        try:
            scheme, salt_s, digest_s = encoded.split("$", 2)
            if scheme != "sha256":
                return False
            salt = _b64d(salt_s)
            expected = _b64d(digest_s)
            actual = hashlib.sha256(salt + secret.encode("utf-8")).digest()
            return hmac.compare_digest(actual, expected)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Session cookie
    # ------------------------------------------------------------------

    def create_session_token(self) -> tuple[str, int]:
        with self._lock:
            now = int(time.time())
            exp = now + self._cookie_ttl_seconds
            payload = {"v": 1, "iat": now, "exp": exp}
            token = self._sign_payload(payload, self._session_secret)
            return token, self._cookie_ttl_seconds

    def verify_session_token(self, token: str) -> bool:
        with self._lock:
            if not self._enabled or self._fail_closed:
                return False
            payload = self._verify_signed_payload(token, self._session_secret)
            if not payload:
                return False
            return int(payload.get("exp") or 0) >= int(time.time())

    # ------------------------------------------------------------------
    # Setup token
    # ------------------------------------------------------------------

    def create_setup_token(self) -> tuple[str, str]:
        """Create a short-lived setup token for ``/v1/setup``.

        The first implementation used a self-contained JSON payload plus a full
        HMAC-SHA256 signature, which was safe but visually noisy in one-line
        install commands.  Setup tokens are only consumed by this same server, so
        use a compact binary format instead:

            st_ + base64url(exp(4B) || nonce(8B) || hmac(...)[0:16])

        This keeps 128-bit MAC strength and a random nonce while making the URL
        much shorter.
        """
        with self._lock:
            exp = int(time.time()) + SETUP_TOKEN_TTL_SECONDS
            token = self._create_compact_setup_token(exp, self._setup_secret)
            return token, _utc_iso(exp)

    def verify_setup_token(self, token: str) -> bool:
        with self._lock:
            if not self._enabled or self._fail_closed:
                return False
            if self._verify_compact_setup_token(token, self._setup_secret):
                return True

            # Backward compatibility for setup links generated by older builds.
            payload = self._verify_signed_payload(token, self._setup_secret)
            if not payload:
                return False
            if payload.get("scope") != "setup":
                return False
            return int(payload.get("exp") or 0) >= int(time.time())

    @staticmethod
    def _create_compact_setup_token(exp: int, secret: str) -> str:
        exp_b = int(exp).to_bytes(SETUP_TOKEN_EXP_BYTES, "big", signed=False)
        nonce = secrets.token_bytes(SETUP_TOKEN_NONCE_BYTES)
        body = exp_b + nonce
        sig = hmac.new(secret.encode("utf-8"), SETUP_TOKEN_CONTEXT + body, hashlib.sha256).digest()[:SETUP_TOKEN_SIG_BYTES]
        return SETUP_TOKEN_PREFIX + _b64e(body + sig)

    @staticmethod
    def _verify_compact_setup_token(token: str, secret: str) -> bool:
        try:
            if not token.startswith(SETUP_TOKEN_PREFIX):
                return False
            raw = _b64d(token[len(SETUP_TOKEN_PREFIX):])
            expected_len = SETUP_TOKEN_EXP_BYTES + SETUP_TOKEN_NONCE_BYTES + SETUP_TOKEN_SIG_BYTES
            if len(raw) != expected_len:
                return False
            body = raw[:SETUP_TOKEN_EXP_BYTES + SETUP_TOKEN_NONCE_BYTES]
            actual = raw[SETUP_TOKEN_EXP_BYTES + SETUP_TOKEN_NONCE_BYTES:]
            expected = hmac.new(secret.encode("utf-8"), SETUP_TOKEN_CONTEXT + body, hashlib.sha256).digest()[:SETUP_TOKEN_SIG_BYTES]
            if not hmac.compare_digest(actual, expected):
                return False
            exp = int.from_bytes(body[:SETUP_TOKEN_EXP_BYTES], "big", signed=False)
            return exp >= int(time.time())
        except Exception:
            return False

    @staticmethod
    def _sign_payload(payload: dict, secret: str) -> str:
        payload_b = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_s = _b64e(payload_b)
        sig = hmac.new(secret.encode("utf-8"), payload_s.encode("ascii"), hashlib.sha256).digest()
        return f"{payload_s}.{_b64e(sig)}"

    @staticmethod
    def _verify_signed_payload(token: str, secret: str) -> Optional[dict]:
        try:
            payload_s, sig_s = token.split(".", 1)
            expected = hmac.new(secret.encode("utf-8"), payload_s.encode("ascii"), hashlib.sha256).digest()
            actual = _b64d(sig_s)
            if not hmac.compare_digest(actual, expected):
                return None
            return json.loads(_b64d(payload_s).decode("utf-8"))
        except Exception:
            return None
