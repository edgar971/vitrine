"""The Prometheus exporter behind ``metrics_port`` (spec § 11.2); needs the ``metrics`` extra.

It lives beside ``app.py``, above the services, because it is the one module that imports
``prometheus_client``: the services count through ``services.metrics.Metrics`` and never know.
Importing it without the extra raises ``ImportError``, which ``serve`` turns into a startup error.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from prometheus_client import CollectorRegistry, Counter, start_http_server
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from mcp_vitrine.domain.models import GateDecision

type Usage = Callable[[], tuple[int, int]]


class PrometheusMetrics:
    """The counters of spec § 11.2, in a registry of their own so nothing else is exported."""

    def __init__(self) -> None:
        """Register the counters; the gauges join once ``measure`` names their source."""
        self.registry = CollectorRegistry()
        self._versions = Counter(
            "vitrine_versions_created",
            "Versions written by creates and updates",
            ["client"],
            registry=self.registry,
        )
        self._views = Counter(
            "vitrine_views", "Views served", ["route", "kind"], registry=self.registry
        )
        self._unlock = Counter(
            "vitrine_unlock", "Passcodes sent to unlock a link", ["outcome"], registry=self.registry
        )
        self._gate = Counter(
            "vitrine_public_gate", "Public-gate verdicts", ["outcome"], registry=self.registry
        )
        self._rejected = Counter(
            "vitrine_rejected",
            "Calls refused before doing anything",
            ["reason"],
            registry=self.registry,
        )

    def measure(self, usage: Usage) -> None:
        """Export ``vitrine_artifacts`` and ``vitrine_storage_bytes`` on each scrape."""
        self.registry.register(_Gauges(usage))

    def version_created(self, client: str) -> None:
        """Count one version."""
        self._versions.labels(client=client).inc()

    def viewed(self, route: str, kind: str) -> None:
        """Count one view."""
        self._views.labels(route=route, kind=kind).inc()

    def unlocked(self, outcome: str) -> None:
        """Count one unlock attempt."""
        self._unlock.labels(outcome=outcome).inc()

    def gate(self, outcome: GateDecision) -> None:
        """Count one verdict."""
        self._gate.labels(outcome=outcome.value).inc()

    def rejected(self, reason: str) -> None:
        """Count one refusal."""
        self._rejected.labels(reason=reason).inc()

    def serve(self, *, host: str, port: int) -> None:
        """Answer ``GET /metrics`` on a listener of its own, in a daemon thread."""
        start_http_server(port, addr=host, registry=self.registry)


class _Gauges(Collector):
    """Read the store only when scraped, so the gauges cost nothing in between."""

    def __init__(self, usage: Usage) -> None:
        """Keep the source of the two numbers."""
        self._usage = usage

    def collect(self) -> Iterator[Metric]:
        """``vitrine_artifacts`` and ``vitrine_storage_bytes``, as of now."""
        artifacts, stored = self._usage()
        yield GaugeMetricFamily("vitrine_artifacts", "Artifacts on disk", value=artifacts)
        yield GaugeMetricFamily(
            "vitrine_storage_bytes", "Bytes the versions hold, counted against quota", value=stored
        )

    def describe(self) -> Iterator[Metric]:
        """Nothing up front: describing would read the store when the collector registers."""
        return iter(())
