"""``TokenBook``: bearer tokens known only by their SHA-256 hashes (ADR 0003).

Both edges authenticate through it, so ``/mcp`` and ``/api`` can never disagree about who a token
belongs to or what it may do.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

from mcp_drop.config import TokenEntry


class TokenBook:
    """Knows tokens only by their hashes, so the server's configuration holds no usable token."""

    def __init__(self, tokens: Mapping[str, TokenEntry]) -> None:
        """Keep the ``DROP_TOKENS`` entries."""
        self._entries = tuple(tokens.values())

    def identify(self, token: str) -> TokenEntry | None:
        """The entry whose hash matches ``token``, or ``None``."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        match: TokenEntry | None = None
        # Every entry is compared, so the time taken does not say which entry, if any, matched.
        for entry in self._entries:
            if hmac.compare_digest(entry.sha256, digest):
                match = entry
        return match
