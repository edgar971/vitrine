"""No HTTP route can put new content on a public artifact (security T19, T21)."""

from __future__ import annotations

import httpx2
import pytest

from mcp_drop.domain.models import GateDecision, PublishedArtifact, Visibility
from mcp_drop.services.container import Services
from tests.http.conftest import CLIENT

INDEX = ("file", ("index.html", b"<h1>new</h1>"))


@pytest.fixture
def public(services: Services, published: PublishedArtifact) -> PublishedArtifact:
    """The published artifact, made public through the gate."""
    services.artifacts.set_visibility(
        published.id, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.APPROVED
    )
    return published


def _version(services: Services, artifact_id: str) -> int:
    """The artifact's current version on disk."""
    return services.artifacts.get(artifact_id).artifact.version


class DescribeUpdatingAPublicArtifactOverTheApi:
    def it_answers_403(
        self, http: httpx2.Client, bearer: dict[str, str], public: PublishedArtifact
    ) -> None:
        response = http.put(f"/api/artifacts/{public.id}", files=[INDEX], headers=bearer)
        assert response.status_code == 403

    def it_writes_nothing(
        self,
        http: httpx2.Client,
        bearer: dict[str, str],
        public: PublishedArtifact,
        services: Services,
    ) -> None:
        http.put(f"/api/artifacts/{public.id}", files=[INDEX], headers=bearer)
        assert _version(services, public.id) == 1


class DescribeUploadingToAPublicArtifact:
    def it_answers_403(
        self, http: httpx2.Client, services: Services, public: PublishedArtifact
    ) -> None:
        grant = services.artifacts.upload_url("chart.png", client=CLIENT, artifact_id=public.id)
        assert http.put(grant.put_url, content=b"PNG").status_code == 403

    def it_writes_nothing(
        self, http: httpx2.Client, services: Services, public: PublishedArtifact
    ) -> None:
        grant = services.artifacts.upload_url("chart.png", client=CLIENT, artifact_id=public.id)
        http.put(grant.put_url, content=b"PNG")
        assert _version(services, public.id) == 1


class DescribeAnUploadUrlOfARevokedClient:
    class WhenTheClientsTokenWasRemoved:
        def it_answers_404(self, http: httpx2.Client, services: Services) -> None:
            grant = services.artifacts.upload_url("report.pdf", client="retired")
            assert http.put(grant.put_url, content=b"%PDF").status_code == 404

    class WhenTheClientMayNoLongerWrite:
        def it_answers_404(self, http: httpx2.Client, services: Services) -> None:
            grant = services.artifacts.upload_url("report.pdf", client="viewer")
            assert http.put(grant.put_url, content=b"%PDF").status_code == 404
