"""The composition root: one set of services per process, shared by the MCP and web edges."""

from __future__ import annotations

from dataclasses import dataclass

from mcp_drop.config import Settings
from mcp_drop.domain.clock import Clock, SystemClock
from mcp_drop.services.artifacts import ArtifactService
from mcp_drop.services.audit import AuditLog
from mcp_drop.services.gate import PublicGate
from mcp_drop.services.limits import RateLimiter
from mcp_drop.services.links import LinkPolicy, LinkService
from mcp_drop.services.metrics import Metrics, NoMetrics
from mcp_drop.services.tokens import TokenBook
from mcp_drop.storage.base import StoreConfig
from mcp_drop.storage.filesystem import FileStore


@dataclass(frozen=True, slots=True)
class Services:
    """Everything an edge calls; built once, passed in, never global."""

    artifacts: ArtifactService
    gate: PublicGate
    audit: AuditLog
    limits: RateLimiter
    tokens: TokenBook
    metrics: Metrics

    @classmethod
    def from_settings(
        cls, settings: Settings, *, clock: Clock | None = None, metrics: Metrics | None = None
    ) -> Services:
        """Wire the store, the signer, the audit log, the gate, the buckets and the tokens."""
        clock = clock or SystemClock()
        metrics = metrics or NoMetrics()
        audit = AuditLog(
            settings.data_dir, retention=settings.audit_retention, clock=clock, metrics=metrics
        )
        links = LinkService(
            LinkPolicy.from_settings(settings), settings.secret.get_secret_value().encode(), clock
        )
        store = FileStore(StoreConfig.from_settings(settings), clock)
        return cls(
            artifacts=ArtifactService(
                store=store, links=links, audit=audit, clock=clock, metrics=metrics
            ),
            gate=PublicGate(settings.public_mode),
            audit=audit,
            limits=RateLimiter.from_settings(settings, metrics=metrics),
            tokens=TokenBook(settings.tokens),
            metrics=metrics,
        )
