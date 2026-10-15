"""``TokenBook``: bearer tokens known only by their SHA-256 hashes (ADR 0003).

Both edges authenticate through it, so ``/mcp`` and ``/api`` can never disagree about who a token
belongs to or what it may do.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

from mcp_drop.config import TokenEntry
from mcp_drop.domain.models import Scope


def token_hash(token: str) -> str:
    """The SHA-256 a ``DROP_TOKENS`` entry holds for ``token`` (spec § 8.2)."""
    return hashlib.sha256(token.encode()).hexdigest()


class TokenBook:
    """Knows tokens only by their hashes, so the server's configuration holds no usable token."""

    def __init__(self, tokens: Mapping[str, TokenEntry]) -> None:
        """Keep the ``DROP_TOKENS`` entries."""
        self._entries = tuple(tokens.values())

    def identify(self, token: str) -> TokenEntry | None:
        """The entry whose hash matches ``token``, or ``None``."""
        digest = token_hash(token)
        match: TokenEntry | None = None
        # Every entry is compared, so the time taken does not say which entry, if any, matched.
        for entry in self._entries:
            if hmac.compare_digest(entry.sha256, digest):
                match = entry
        return match

    def allows(self, client: str, scope: Scope) -> bool:
        """Whether ``client`` is still configured with ``scope``.

        A capability minted for a client (an upload URL) is only as good as that client: removing
        its token, or the scope, stops what it already handed out (security T21).
        """
        return any(entry.name == client and scope in entry.scopes for entry in self._entries)
