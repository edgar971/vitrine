"""The dashboard's login: the operator password and the session it earns (spec § 7.6, ADR 0008).

The password is known only by its scrypt hash, like a link's passcode. A session is stateless: an
issue time and a nonce under an HMAC whose key comes from ``secret`` and the password hash, so
changing either ends every session, and there is nothing to store or sweep.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from mcp_vitrine.domain.clock import Clock
from mcp_vitrine.domain.errors import Invalid

# The ``client`` of every change made from the dashboard. The ``@`` keeps it apart from every
# client: no ``VITRINE_TOKENS`` name can start with one.
ADMIN = "@admin"
SCHEME = "scrypt"
PASSWORD_MIN = 12
PASSWORD_MAX = 1024
SALT_BYTES = 16
HASH_BYTES = 32
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
MAX_MEMORY = 64 * 1024 * 1024
NONCE_BYTES = 16
SESSION_TTL = timedelta(hours=12)
_SESSION_LABEL = b"vitrine/admin-session/v1"
_CSRF_LABEL = b"csrf"
# Longer than any value ``issue`` makes; a longer cookie is not worth hashing.
SESSION_MAX = 160


def hash_password(password: str) -> str:
    """The ``VITRINE_ADMIN_PASSWORD`` value for ``password``. Slow on purpose."""
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise Invalid(f"the password is {PASSWORD_MIN} to {PASSWORD_MAX} characters")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return "$".join(
        (SCHEME, str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P), _encode(salt), _encode(digest))
    )


@dataclass(frozen=True, slots=True)
class Session:
    """A verified session: when it was issued, and the token its forms must carry."""

    issued_at: datetime
    csrf: str


class AdminLogin:
    """Checks the operator password and signs and verifies the sessions it earns."""

    def __init__(self, password_hash: str, secret: bytes, clock: Clock) -> None:
        """Hold the hash, a session key bound to it and to ``secret``, and the clock."""
        self._hash = password_hash
        self._key = hmac.new(secret, _SESSION_LABEL + password_hash.encode(), "sha256").digest()
        self._clock = clock

    def verify(self, password: str) -> bool:
        """Whether ``password`` is the operator's, compared in constant time."""
        scheme, n, r, p, salt, expected = self._hash.split("$")
        if scheme != SCHEME or len(password) > PASSWORD_MAX:
            return False
        supplied = _scrypt(password, _decode(salt), int(n), int(r), int(p))
        return hmac.compare_digest(supplied, _decode(expected))

    def issue(self) -> str:
        """A new session cookie value: ``v1.<issued_at>.<nonce>.<mac>``."""
        body = (
            f"v1.{int(self._clock.now().timestamp())}.{_encode(secrets.token_bytes(NONCE_BYTES))}"
        )
        return f"{body}.{self._mac(body.encode())}"

    def session(self, value: str | None) -> Session | None:
        """The session a cookie value proves, or ``None``: forged, malformed or expired."""
        if not value or len(value) > SESSION_MAX:
            return None
        body, _, mac = value.rpartition(".")
        # Bytes: a cookie is attacker-chosen, and compare_digest refuses non-ASCII text.
        if not hmac.compare_digest(self._mac(body.encode()).encode(), mac.encode()):
            return None
        parts = body.split(".")
        if len(parts) != len(("v1", "issued_at", "nonce")) or not parts[1].isdigit():
            return None
        issued_at = datetime.fromtimestamp(int(parts[1]), tz=self._clock.now().tzinfo)
        if not issued_at <= self._clock.now() < issued_at + SESSION_TTL:
            return None
        return Session(issued_at=issued_at, csrf=self._mac(_CSRF_LABEL + parts[2].encode()))

    def _mac(self, data: bytes) -> str:
        """An HMAC-SHA256 under the session key, base64url without padding."""
        return _encode(hmac.new(self._key, data, "sha256").digest())


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    """``hashlib.scrypt`` with the parameters the hash names."""
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, maxmem=MAX_MEMORY, dklen=HASH_BYTES
    )


def _encode(value: bytes) -> str:
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(text: str) -> bytes:
    """Undo :func:`_encode`."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
