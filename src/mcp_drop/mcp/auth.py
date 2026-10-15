"""``HashedTokenVerifier``: bearer tokens checked against SHA-256 hashes, with scopes (ADR 0003)."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

from fastmcp.server.auth import AccessToken, TokenVerifier

from mcp_drop.config import TokenEntry


class HashedTokenVerifier(TokenVerifier):
    """Knows tokens only by their hashes, so the server's configuration holds no usable token."""

    def __init__(self, tokens: Mapping[str, TokenEntry]) -> None:
        """Keep the ``DROP_TOKENS`` entries."""
        super().__init__()
        self._entries = tuple(tokens.values())

    async def verify_token(self, token: str) -> AccessToken | None:
        """Name the client whose hash matches, with its scopes, or ``None`` for a 401."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        match: TokenEntry | None = None
        # Every entry is compared, so the time taken does not say which entry, if any, matched.
        for entry in self._entries:
            if hmac.compare_digest(entry.sha256, digest):
                match = entry
        if match is None:
            return None
        return AccessToken(
            token=token, client_id=match.name, scopes=sorted(scope.value for scope in match.scopes)
        )
