"""Link passcodes: generated or supplied, stored as an scrypt hash only (spec § 3, ADR 0006)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from mcp_vitrine.domain.errors import Invalid
from mcp_vitrine.domain.ids import ALPHABET
from mcp_vitrine.domain.models import Passcode

# Ten Crockford base32 characters: 50 bits, which the per-link buckets make unguessable online.
GENERATED_LENGTH = 10
GROUP = 5
SUPPLIED_MIN = 8
SUPPLIED_MAX = 128
SALT_BYTES = 16
HASH_BYTES = 32
# The standard library's scrypt; n=2**15, r=8 needs 32 MiB, over its 32 MiB default ceiling.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
MAX_MEMORY = 64 * 1024 * 1024


def generate() -> str:
    """A new passcode, shown as ``xxxxx-xxxxx``."""
    raw = "".join(secrets.choice(ALPHABET) for _ in range(GENERATED_LENGTH))
    return f"{raw[:GROUP]}-{raw[GROUP:]}"


def check_supplied(passcode: str) -> None:
    """Refuse a supplied passcode outside 8-128 characters, without ever repeating it."""
    if not SUPPLIED_MIN <= len(passcode) <= SUPPLIED_MAX:
        raise Invalid(f"a passcode is {SUPPLIED_MIN} to {SUPPLIED_MAX} characters")


def digest(passcode: str) -> Passcode:
    """The scrypt record of a passcode, under a fresh salt. Slow on purpose: run it off the loop."""
    salt = secrets.token_bytes(SALT_BYTES)
    return Passcode(
        hash=_encode(_scrypt(passcode, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)),
        salt=_encode(salt),
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
    )


def matches(passcode: str, record: Passcode) -> bool:
    """Whether ``passcode`` is the one ``record`` was made from, compared in constant time."""
    salt = _decode(record.salt)
    expected = _decode(record.hash)
    if len(passcode) > SUPPLIED_MAX:
        return False
    supplied = _scrypt(passcode, salt, record.n, record.r, record.p)
    return hmac.compare_digest(supplied, expected)


def _normal(passcode: str) -> bytes:
    """Case and hyphens do not count, so ``ABCDE-fghjk`` and ``abcdefghjk`` are one passcode."""
    return passcode.replace("-", "").casefold().encode()


def _scrypt(passcode: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    """``hashlib.scrypt`` with the parameters the record names."""
    return hashlib.scrypt(
        _normal(passcode), salt=salt, n=n, r=r, p=p, maxmem=MAX_MEMORY, dklen=HASH_BYTES
    )


def _encode(value: bytes) -> str:
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(text: str) -> bytes:
    """Undo :func:`_encode`."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
