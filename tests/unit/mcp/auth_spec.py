"""``HashedTokenVerifier``: tokens known only by their hashes (ADR 0003, T2, T12)."""

from __future__ import annotations

import hashlib

import pytest

from mcp_vitrine.config import TokenEntry
from mcp_vitrine.domain.models import Scope
from mcp_vitrine.mcp.auth import HashedTokenVerifier

TOKEN = "s3cret-token-for-hermes"


def _entry(name: str, token: str, scopes: frozenset[Scope]) -> TokenEntry:
    """A ``VITRINE_TOKENS`` entry for ``token``."""
    return TokenEntry(name=name, sha256=hashlib.sha256(token.encode()).hexdigest(), scopes=scopes)


@pytest.fixture
def verifier() -> HashedTokenVerifier:
    """Two clients: one with every scope, one that only reads."""
    return HashedTokenVerifier(
        {
            "hermes": _entry("hermes", TOKEN, frozenset(Scope)),
            "viewer": _entry("viewer", "viewer-token", frozenset({Scope.READ})),
        }
    )


class DescribeHashedTokenVerifier:
    class WhenTheTokenIsKnown:
        async def it_names_the_client(self, verifier: HashedTokenVerifier) -> None:
            token = await verifier.verify_token(TOKEN)
            assert token is not None
            assert token.client_id == "hermes"

        async def it_carries_the_scopes(self, verifier: HashedTokenVerifier) -> None:
            token = await verifier.verify_token("viewer-token")
            assert token is not None
            assert token.scopes == ["vitrine:read"]

    class WhenTheTokenIsUnknown:
        async def it_refuses_it(self, verifier: HashedTokenVerifier) -> None:
            assert await verifier.verify_token("guess") is None

    class WhenTheCallerSendsTheHashItself:
        async def it_refuses_it(self, verifier: HashedTokenVerifier) -> None:
            digest = hashlib.sha256(TOKEN.encode()).hexdigest()
            assert await verifier.verify_token(digest) is None
