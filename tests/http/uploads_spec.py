"""``PUT /u/{sig}``: single-use signed uploads over real HTTP (spec § 7.3, security T5)."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx2
import pytest

from mcp_drop.domain.errors import NotFound
from mcp_drop.domain.models import PublishedArtifact, UploadGrant, Visibility
from mcp_drop.services.audit import AuditEvent, AuditEventName
from mcp_drop.services.container import Services
from tests.http.conftest import CLIENT


@pytest.fixture
def grant(services: Services) -> UploadGrant:
    """An upload URL for a new artifact, as ``artifact_upload_url`` issues it."""
    return services.artifacts.upload_url("report.pdf", client=CLIENT)


@pytest.fixture
def events(services: Services) -> Callable[[], tuple[AuditEvent, ...]]:
    """The audit log as it stands, newest first."""
    return services.audit.query


def _chunks(total: int) -> Iterator[bytes]:
    """A body with no ``Content-Length``, sent chunked."""
    sent = 0
    while sent < total:
        yield b"x" * 1024
        sent += 1024


class DescribeTheUploadRoute:
    class WhenTheUrlIsValid:
        def it_answers_201(self, http: httpx2.Client, grant: UploadGrant) -> None:
            assert http.put(grant.put_url, content=b"%PDF-1.7").status_code == 201

        def it_creates_the_pre_allocated_artifact(
            self, http: httpx2.Client, grant: UploadGrant
        ) -> None:
            assert http.put(grant.put_url, content=b"%PDF-1.7").json()["id"] == grant.id

        def it_is_private(
            self, http: httpx2.Client, grant: UploadGrant, services: Services
        ) -> None:
            http.put(grant.put_url, content=b"%PDF-1.7")
            assert services.artifacts.get(grant.id).artifact.visibility is Visibility.PRIVATE

        def it_returns_a_signed_link(self, http: httpx2.Client, grant: UploadGrant) -> None:
            assert "/s/" in http.put(grant.put_url, content=b"%PDF-1.7").json()["url"]

        def it_audits_the_creation_for_the_client_that_asked(
            self,
            http: httpx2.Client,
            grant: UploadGrant,
            events: Callable[[], tuple[AuditEvent, ...]],
        ) -> None:
            http.put(grant.put_url, content=b"%PDF-1.7")
            assert (events()[0].event, events()[0].client) == (AuditEventName.CREATED, CLIENT)

    class WhenTheUrlIsReplayed:
        def it_answers_409(self, http: httpx2.Client, grant: UploadGrant) -> None:
            http.put(grant.put_url, content=b"%PDF-1.7")
            assert http.put(grant.put_url, content=b"%PDF-1.7").status_code == 409

    class WhenTheUrlNamesAnExistingArtifact:
        def it_adds_a_version(
            self, http: httpx2.Client, services: Services, published: PublishedArtifact
        ) -> None:
            grant = services.artifacts.upload_url(
                "chart.png", client=CLIENT, artifact_id=published.id
            )
            response = http.put(grant.put_url, content=b"PNG")
            assert (response.status_code, response.json()["version"]) == (200, 2)

    class WhenTheSignatureIsTamperedWith:
        def it_answers_404(self, http: httpx2.Client, grant: UploadGrant) -> None:
            assert http.put(grant.put_url.replace("/u/", "/u/x"), content=b"1").status_code == 404

    class WhenTheFilenameTraverses:
        def it_answers_400(self, http: httpx2.Client, grant: UploadGrant) -> None:
            url = grant.put_url.replace("filename=report.pdf", "filename=../report.pdf")
            assert http.put(url, content=b"1").status_code == 400

    class WhenTheTitleIsTooLong:
        def it_answers_400(self, http: httpx2.Client, grant: UploadGrant) -> None:
            assert http.put(f"{grant.put_url}&title={'t' * 201}", content=b"1").status_code == 400

    class WhenATitleIsGiven:
        def it_titles_the_artifact(
            self, http: httpx2.Client, grant: UploadGrant, services: Services
        ) -> None:
            http.put(f"{grant.put_url}&title=Q3", content=b"1")
            assert services.artifacts.get(grant.id).artifact.title == "Q3"

    class WhenNoFilenameIsGiven:
        def it_answers_400(self, http: httpx2.Client, grant: UploadGrant) -> None:
            assert http.put(grant.put_url.split("?")[0], content=b"1").status_code == 400

    class WhenTheBodyIsTooLarge:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            return {"DROP_MAX_UPLOAD": "64KB"}

        def it_answers_413_from_the_declared_length(
            self, http: httpx2.Client, grant: UploadGrant
        ) -> None:
            assert http.put(grant.put_url, content=b"x" * (65 * 1024)).status_code == 413

        def it_answers_413_while_a_chunked_body_streams(
            self, http: httpx2.Client, grant: UploadGrant
        ) -> None:
            assert http.put(grant.put_url, content=_chunks(256 * 1024)).status_code == 413

        def it_creates_nothing(
            self, http: httpx2.Client, grant: UploadGrant, services: Services
        ) -> None:
            http.put(grant.put_url, content=_chunks(256 * 1024))
            with pytest.raises(NotFound):
                services.artifacts.get(grant.id)

    class WhenTheClientIsOverItsWriteRate:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            return {"DROP_RATE_WRITES": "1/m"}

        @pytest.fixture
        def second(self, http: httpx2.Client, services: Services, grant: UploadGrant) -> str:
            """A second upload URL, once the first PUT has spent the only token."""
            http.put(grant.put_url, content=b"1")
            return services.artifacts.upload_url("b.pdf", client=CLIENT).put_url

        def it_answers_429_with_retry_after(self, http: httpx2.Client, second: str) -> None:
            response = http.put(second, content=b"1")
            assert (response.status_code, "Retry-After" in response.headers) == (429, True)

        def it_audits_the_refusal_by_route(
            self,
            http: httpx2.Client,
            second: str,
            events: Callable[[], tuple[AuditEvent, ...]],
        ) -> None:
            http.put(second, content=b"1")
            assert (events()[0].event, events()[0].route) == (
                AuditEventName.ACCESS_DENIED,
                "upload",
            )
