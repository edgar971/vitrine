"""Identifiers: artifact ids, client references and upload nonces (spec § 3, § 4.5, § 7.3)."""

from __future__ import annotations

import re
import secrets

from mcp_vitrine.domain.errors import Invalid

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
ID_LENGTH = 16
BITS_PER_CHARACTER = 5
NONCE_BYTES = 16
LID_BYTES = 8
# 8 bytes in unpadded base64url.
LID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
CLIENT_REF_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
CLIENT_REF_MAX = 128

_ID_BYTES = ID_LENGTH * BITS_PER_CHARACTER // 8
_SHIFTS = tuple(range(ID_LENGTH * BITS_PER_CHARACTER - BITS_PER_CHARACTER, -1, -BITS_PER_CHARACTER))


def new_id() -> str:
    """Draw a fresh id: 80 random bits, five per Crockford base32 character (spec § 3)."""
    number = int.from_bytes(secrets.token_bytes(_ID_BYTES), "big")
    return "".join(ALPHABET[(number >> shift) & 31] for shift in _SHIFTS)


def is_valid_id(value: str) -> bool:
    """Whether ``value`` has the id shape; check it before any filesystem access (spec § 3)."""
    return len(value) == ID_LENGTH and all(character in ALPHABET for character in value)


def new_nonce() -> str:
    """A fresh single-use upload nonce (spec § 7.3)."""
    return secrets.token_urlsafe(NONCE_BYTES)


def new_lid() -> str:
    """A fresh link id: 8 random bytes in base64url, unique among an artifact's links (§ 3)."""
    return secrets.token_urlsafe(LID_BYTES)


def is_valid_lid(value: str) -> bool:
    """Whether ``value`` has the link-id shape."""
    return LID_PATTERN.fullmatch(value) is not None


def validate_client_ref(ref: str) -> str:
    """Return the reference if it matches spec § 4.5, else raise ``Invalid``."""
    if not 0 < len(ref) <= CLIENT_REF_MAX or CLIENT_REF_PATTERN.fullmatch(ref) is None:
        raise Invalid(f"a client_ref is 1-{CLIENT_REF_MAX} characters from [A-Za-z0-9._:-]")
    return ref
