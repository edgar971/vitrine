"""``Metrics``: what the services count, with no exporter behind it unless one is configured.

The Prometheus exporter is an optional extra (spec § 11.2), so the services record through this
seam and never import it. Labels are client names, routes, outcomes and reasons: never an
artifact id or title.
"""

from __future__ import annotations

from typing import Protocol

from mcp_vitrine.domain.models import GateDecision


class Metrics(Protocol):
    """The counters of spec § 11.2; the two gauges are read from the store when scraped."""

    def version_created(self, client: str) -> None:
        """A create or an update landed a version."""

    def viewed(self, route: str) -> None:
        """A view (``a`` or ``s``) was served."""

    def gate(self, outcome: GateDecision) -> None:
        """A publish reached the public gate and got this verdict."""

    def rejected(self, reason: str) -> None:
        """A call was refused before it did anything (``rate_limited``, ``scope``)."""


class NoMetrics:
    """The default when ``metrics_port`` is 0: counts nothing."""

    def version_created(self, client: str) -> None:
        """Count nothing."""

    def viewed(self, route: str) -> None:
        """Count nothing."""

    def gate(self, outcome: GateDecision) -> None:
        """Count nothing."""

    def rejected(self, reason: str) -> None:
        """Count nothing."""
