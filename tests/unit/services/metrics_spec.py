"""What the services count (spec § 11.2), and the Prometheus exporter that publishes it."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest
from prometheus_client import generate_latest

from mcp_vitrine.config import parse_rate
from mcp_vitrine.domain.clock import Clock
from mcp_vitrine.domain.errors import RateLimited, Refused
from mcp_vitrine.domain.models import ArtifactDraft, GateDecision, VersionDraft, Visibility
from mcp_vitrine.metrics import PrometheusMetrics
from mcp_vitrine.services.artifacts import ArtifactService
from mcp_vitrine.services.audit import AuditLog, DenialReason
from mcp_vitrine.services.limits import Bucket, RateLimiter
from mcp_vitrine.services.links import LinkPolicy, LinkService
from mcp_vitrine.storage.base import StoreConfig
from mcp_vitrine.storage.filesystem import FileStore

CLIENT = "hermes"
TITLE = "Quarterly secrets"
SECRET = b"0123456789abcdef0123456789abcdef"


class RecordingMetrics:
    """Remembers every count, so a spec can read what a call recorded."""

    def __init__(self) -> None:
        """Start from nothing."""
        self.counts: Counter[tuple[str, str]] = Counter()

    def version_created(self, client: str) -> None:
        """Remember a version."""
        self.counts["version", client] += 1

    def viewed(self, route: str) -> None:
        """Remember a view."""
        self.counts["view", route] += 1

    def gate(self, outcome: GateDecision) -> None:
        """Remember a verdict."""
        self.counts["gate", outcome.value] += 1

    def rejected(self, reason: str) -> None:
        """Remember a refusal."""
        self.counts["rejected", reason] += 1


@pytest.fixture
def metrics() -> RecordingMetrics:
    """The counts the services record."""
    return RecordingMetrics()


@pytest.fixture
def audit(tmp_path: Path, clock: Clock, metrics: RecordingMetrics) -> AuditLog:
    """An audit log that counts scope refusals."""
    return AuditLog(tmp_path, retention=timedelta(days=365), clock=clock, metrics=metrics)


@pytest.fixture
def service(
    store_config: StoreConfig, clock: Clock, audit: AuditLog, metrics: RecordingMetrics
) -> ArtifactService:
    """An artifact service that counts."""
    return ArtifactService(
        store=FileStore(store_config, clock),
        links=LinkService(LinkPolicy(base_url="https://vitrine.example.com"), SECRET, clock),
        audit=audit,
        clock=clock,
        metrics=metrics,
    )


@pytest.fixture
def created(service: ArtifactService) -> str:
    """One artifact, created by ``CLIENT``."""
    draft = ArtifactDraft(title=TITLE, files={"index.html": b"hi"})
    return service.create(draft, client=CLIENT).id


class DescribeCountedVersions:
    class WhenAnArtifactIsCreatedAndUpdated:
        def it_counts_both_versions_for_the_client(
            self, service: ArtifactService, created: str, metrics: RecordingMetrics
        ) -> None:
            service.update(created, VersionDraft(files={"index.html": b"v2"}), client=CLIENT)
            assert metrics.counts["version", CLIENT] == 2


class DescribeCountedGateVerdicts:
    class WhenAPublishIsDeclined:
        def it_counts_the_verdict(
            self, service: ArtifactService, created: str, metrics: RecordingMetrics
        ) -> None:
            with pytest.raises(Refused):
                service.set_visibility(
                    created, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.DECLINED
                )
            assert metrics.counts["gate", "declined"] == 1

    class WhenAnArtifactIsMadePrivate:
        def it_counts_no_verdict(
            self, service: ArtifactService, created: str, metrics: RecordingMetrics
        ) -> None:
            service.set_visibility(
                created, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.APPROVED
            )
            service.set_visibility(
                created, Visibility.PRIVATE, client=CLIENT, gate=GateDecision.NOT_REQUIRED
            )
            assert metrics.counts["gate", "not_required"] == 0


class DescribeCountedRefusals:
    class WhenABucketIsEmpty:
        def it_counts_a_rate_limited_refusal(self, metrics: RecordingMetrics) -> None:
            limiter = RateLimiter({Bucket.VIEWS: parse_rate("1/m")}, metrics=metrics)
            limiter.check(Bucket.VIEWS, "203.0.113.9")
            with pytest.raises(RateLimited):
                limiter.check(Bucket.VIEWS, "203.0.113.9")
            assert metrics.counts["rejected", "rate_limited"] == 1

    class WhenACallLacksItsScope:
        def it_counts_a_scope_refusal(self, audit: AuditLog, metrics: RecordingMetrics) -> None:
            audit.deny(client=CLIENT, reason=DenialReason.SCOPE, tool="artifact_create")
            assert metrics.counts["rejected", "scope"] == 1

    class WhenARateLimitedCallIsAudited:
        def it_is_not_counted_twice(self, audit: AuditLog, metrics: RecordingMetrics) -> None:
            audit.deny(client=CLIENT, reason=DenialReason.RATE_LIMITED, tool="artifact_create")
            assert metrics.counts["rejected", "rate_limited"] == 0


class DescribeThePrometheusExporter:
    @pytest.fixture
    def exporter(self, service: ArtifactService) -> PrometheusMetrics:
        """The exporter, reading its gauges from the service."""
        exporter = PrometheusMetrics()
        exporter.measure(service.usage)
        return exporter

    @pytest.fixture
    def scraped(self, exporter: PrometheusMetrics, created: str) -> str:
        """One scrape after a version, a view, a verdict and a refusal were counted."""
        exporter.version_created(CLIENT)
        exporter.viewed("s")
        exporter.gate(GateDecision.APPROVED)
        exporter.rejected("rate_limited")
        return generate_latest(exporter.registry).decode()

    class WhenItIsScraped:
        def it_exports_the_artifact_count(self, scraped: str) -> None:
            assert "vitrine_artifacts 1.0" in scraped

        def it_exports_the_stored_bytes(self, scraped: str) -> None:
            assert "vitrine_storage_bytes 2.0" in scraped

        def it_exports_versions_by_client(self, scraped: str) -> None:
            assert 'vitrine_versions_created_total{client="hermes"} 1.0' in scraped

        def it_exports_views_by_route(self, scraped: str) -> None:
            assert 'vitrine_views_total{route="s"} 1.0' in scraped

        def it_exports_gate_verdicts(self, scraped: str) -> None:
            assert 'vitrine_public_gate_total{outcome="approved"} 1.0' in scraped

        def it_exports_refusals_by_reason(self, scraped: str) -> None:
            assert 'vitrine_rejected_total{reason="rate_limited"} 1.0' in scraped

        def it_carries_no_artifact_id(self, scraped: str, created: str) -> None:
            assert created not in scraped

        def it_carries_no_title(self, scraped: str) -> None:
            assert TITLE not in scraped
