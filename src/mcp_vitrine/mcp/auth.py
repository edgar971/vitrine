"""``HashedTokenVerifier``: bearer tokens checked against SHA-256 hashes, with scopes (ADR 0003)."""

from __future__ import annotations

from collections.abc import Mapping

from fastmcp.server.auth import AccessToken, TokenVerifier

from mcp_vitrine.config import TokenEntry
from mcp_vitrine.services.tokens import TokenBook


class HashedTokenVerifier(TokenVerifier):
    """Knows tokens only by their hashes, so the server's configuration holds no usable token."""

    def __init__(self, tokens: Mapping[str, TokenEntry]) -> None:
        """Keep the ``VITRINE_TOKENS`` entries."""
        super().__init__()
        self._book = TokenBook(tokens)

    async def verify_token(self, token: str) -> AccessToken | None:
        """Name the client whose hash matches, with its scopes, or ``None`` for a 401."""
        match = self._book.identify(token)
        if match is None:
            return None
        return AccessToken(
            token=token, client_id=match.name, scopes=sorted(scope.value for scope in match.scopes)
        )
