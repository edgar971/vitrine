"""The composition root: one set of services per process, shared by the MCP and web edges."""

from __future__ import annotations

from dataclasses import dataclass

from mcp_vitrine.config import Settings
from mcp_vitrine.domain.clock import Clock, SystemClock
from mcp_vitrine.services.artifacts import ArtifactService
from mcp_vitrine.services.audit import AuditLog
from mcp_vitrine.services.gate import PublicGate
from mcp_vitrine.services.limits import RateLimiter
from mcp_vitrine.services.links import LinkPolicy, LinkService
from mcp_vitrine.services.metrics import Metrics, NoMetrics
from mcp_vitrine.services.tokens import TokenBook
from mcp_vitrine.storage.base import StoreConfig
from mcp_vitrine.storage.filesystem import FileStore


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
