"""The per-IP view bucket (spec § 7.5, security T16), and whose address counts."""

from __future__ import annotations

import httpx2
import pytest

from mcp_drop.domain.models import PublishedArtifact
from mcp_drop.services.audit import AuditEvent
from mcp_drop.services.container import Services


class DescribeTheViewBucket:
    @pytest.fixture
    def overrides(self) -> dict[str, str]:
        return {"DROP_RATE_VIEWS": "1/m"}

    @pytest.fixture
    def spent(self, http: httpx2.Client, published: PublishedArtifact) -> PublishedArtifact:
        """The artifact, once this address has spent its only view."""
        http.get(published.url)
        return published

    class WhenAnAddressIsOverItsRate:
        def it_answers_429(self, http: httpx2.Client, spent: PublishedArtifact) -> None:
            assert http.get(spent.url).status_code == 429

        def it_says_when_to_retry(self, http: httpx2.Client, spent: PublishedArtifact) -> None:
            assert int(http.get(spent.url).headers["Retry-After"]) > 0

        def it_does_not_audit_anonymous_viewers(
            self, http: httpx2.Client, spent: PublishedArtifact, services: Services
        ) -> None:
            before: tuple[AuditEvent, ...] = services.audit.query()
            http.get(spent.url)
            assert services.audit.query() == before

    class WhenATrustedProxyForwardsAnotherAddress:
        def it_counts_that_address_apart(
            self, http: httpx2.Client, spent: PublishedArtifact
        ) -> None:
            response = http.get(spent.url, headers={"X-Forwarded-For": "203.0.113.9"})
            assert response.status_code == 200

    class WhenAnUntrustedPeerForwardsAnotherAddress:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            return {"DROP_RATE_VIEWS": "1/m", "DROP_FORWARDED_ALLOW_IPS": "192.0.2.1"}

        def it_ignores_the_header(self, http: httpx2.Client, spent: PublishedArtifact) -> None:
            response = http.get(spent.url, headers={"X-Forwarded-For": "203.0.113.9"})
            assert response.status_code == 429

    class WhenTheLimitIsOff:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            return {"DROP_RATE_VIEWS": "off"}

        def it_never_answers_429(self, http: httpx2.Client, spent: PublishedArtifact) -> None:
            assert http.get(spent.url).status_code == 200
