"""Typed domain errors, mapped once per edge (``docs/design.md`` § 3)."""

from __future__ import annotations

NOT_FOUND = "not found"
EXPIRED = "expired"


class VitrineError(Exception):
    """A failure a caller maps to exactly one status or tool error (spec § 10)."""


class NotFound(VitrineError):
    """Unknown, malformed or invisible; one message covers all three (spec § 3, § 10)."""

    def __init__(self) -> None:
        """Use the single indistinguishable message."""
        super().__init__(NOT_FOUND)


class Expired(VitrineError):
    """The artifact is past ``expires_at``, so every view answers 410 (spec § 4.3)."""

    def __init__(self) -> None:
        """Use the one expiry message."""
        super().__init__(EXPIRED)


class Invalid(VitrineError):
    """The request is malformed: an unsafe path, a bad entry, an empty version."""


class TooLarge(VitrineError):
    """A size or file-count limit would be exceeded (spec § 8.2)."""


class OverQuota(VitrineError):
    """The write would push the store past ``quota`` (spec § 8.2)."""


class Conflict(VitrineError):
    """A ``client_ref`` is bound to a different request, or a nonce was reused (spec § 4.5)."""


class Refused(VitrineError):
    """The public gate did not approve, so the artifact stays private (spec § 6.1)."""


class RateLimited(VitrineError):
    """A token bucket is empty; the caller may retry after ``retry_after`` seconds (spec § 7.5)."""

    def __init__(self, retry_after: int) -> None:
        """Carry the wait, so the MCP and HTTP edges report the same number."""
        super().__init__(f"rate limited: retry after {retry_after}s")
        self.retry_after = retry_after


class ConfigError(VitrineError):
    """The configuration is invalid, so the process must not start (spec § 8.1)."""


class SchemaMismatch(VitrineError):
    """``meta.json`` carries a ``schema_version`` this build cannot read (spec § 4.2)."""


class AuditUnavailable(VitrineError):
    """The audit log cannot be written, so the change does not happen (spec § 11.3)."""


class StorageUnavailable(VitrineError):
    """The store cannot complete the operation, so nothing changed on disk."""
