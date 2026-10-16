"""``/api/artifacts``: the bearer-authenticated multipart API (spec § 7.1, § 4.5, § 10)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from mcp_vitrine.domain.models import PublishedArtifact, Visibility
from mcp_vitrine.services.audit import AuditEvent, AuditEventName, DenialReason
from mcp_vitrine.services.container import Services
from tests.http.conftest import READ_TOKEN

INDEX = ("file", ("index.html", b"<h1>hi</h1>"))
CHART = ("file", ("img/chart.png", b"PNG"))

Create = Callable[..., httpx2.Response]


@pytest.fixture
def create(http: httpx2.Client, bearer: dict[str, str]) -> Create:
    """POST a multipart create as the full-scope client."""

    def post(files: list[Any] | None = None, **fields: Any) -> httpx2.Response:  # noqa: ANN401
        return http.post(
            "/api/artifacts",
            files=[INDEX] if files is None else files,
            data={"title": "Energy"} | fields,
            headers=bearer,
        )

    return post


@pytest.fixture
def reader() -> dict[str, str]:
    """The header of the client that may only read."""
    return {"Authorization": f"Bearer {READ_TOKEN}"}


@pytest.fixture
def events(services: Services) -> Callable[[], tuple[AuditEvent, ...]]:
    """The audit log as it stands, newest first."""
    return services.audit.query


class DescribeCreatingOverTheApi:
    class WhenNoTokenIsSent:
        def it_answers_401(self, http: httpx2.Client) -> None:
            assert http.post("/api/artifacts", files=[INDEX]).status_code == 401

    class WhenTheTokenIsWrong:
        def it_answers_401(self, http: httpx2.Client) -> None:
            response = http.post(
                "/api/artifacts", files=[INDEX], headers={"Authorization": "Bearer guess"}
            )
            assert response.status_code == 401

    class WhenTheTokenCannotWrite:
        def it_answers_403(self, http: httpx2.Client, reader: dict[str, str]) -> None:
            response = http.post(
                "/api/artifacts", files=[INDEX], data={"title": "x"}, headers=reader
            )
            assert response.status_code == 403

        def it_audits_the_refusal_by_route(
            self,
            http: httpx2.Client,
            reader: dict[str, str],
            events: Callable[[], tuple[AuditEvent, ...]],
        ) -> None:
            http.post("/api/artifacts", files=[INDEX], data={"title": "x"}, headers=reader)
            assert (events()[0].reason, events()[0].route) == (DenialReason.SCOPE, "api_create")

    class WhenTheBodyIsValid:
        def it_answers_201(self, create: Create) -> None:
            assert create().status_code == 201

        def it_creates_a_private_artifact(self, create: Create, services: Services) -> None:
            artifact_id = create().json()["id"]
            assert services.artifacts.get(artifact_id).artifact.visibility is Visibility.PRIVATE

        def it_returns_a_signed_link(self, create: Create) -> None:
            assert "/s/" in create().json()["url"]

        def it_keeps_each_part_s_relative_path(self, create: Create, services: Services) -> None:
            artifact_id = create(files=[INDEX, CHART]).json()["id"]
            paths = [file.path for file in services.artifacts.get(artifact_id).files]
            assert paths == ["img/chart.png", "index.html"]

        def it_reads_repeated_and_comma_separated_tags(
            self, create: Create, services: Services
        ) -> None:
            artifact_id = create(tags=["energy,monthly", "q3"]).json()["id"]
            assert services.artifacts.get(artifact_id).artifact.tags == ("energy", "monthly", "q3")

        def it_audits_the_creation(
            self, create: Create, events: Callable[[], tuple[AuditEvent, ...]]
        ) -> None:
            create()
            assert events()[0].event is AuditEventName.CREATED

    class WhenAVisibilityIsSent:
        def it_refuses_the_request(self, create: Create) -> None:
            assert create(visibility="public").status_code == 400

    class WhenTheTitleIsMissing:
        def it_answers_400(self, http: httpx2.Client, bearer: dict[str, str]) -> None:
            assert http.post("/api/artifacts", files=[INDEX], headers=bearer).status_code == 400

    class WhenThereAreNoFiles:
        def it_answers_400(self, create: Create) -> None:
            assert create(files=[]).status_code == 400

    class WhenAPartIsNamedTwice:
        def it_answers_400(self, create: Create) -> None:
            assert create(files=[INDEX, INDEX]).status_code == 400

    class WhenAFilenameTraverses:
        def it_answers_400(self, create: Create) -> None:
            assert create(files=[("file", ("../x.html", b"x"))]).status_code == 400

    class WhenTheBodyIsNotMultipart:
        def it_answers_400(self, http: httpx2.Client, bearer: dict[str, str]) -> None:
            response = http.post(
                "/api/artifacts",
                content=b"--x\r\nbroken",
                headers=bearer | {"Content-Type": "multipart/form-data; boundary=x"},
            )
            assert response.status_code == 400

    class WhenThereAreTooManyFiles:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            return {"VITRINE_MAX_FILES": "1"}

        def it_answers_413(self, create: Create) -> None:
            assert create(files=[INDEX, CHART]).status_code == 413

    class WhenTheStoreIsFull:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            return {"VITRINE_QUOTA": "16", "VITRINE_MAX_ARTIFACT": "16"}

        def it_answers_507(self, create: Create) -> None:
            create()
            assert create(files=[("file", ("index.html", b"<h1>again</h1>"))]).status_code == 507

    class WhenTheClientRefIsRepeated:
        def it_returns_the_same_artifact_with_200(self, create: Create) -> None:
            first = create(client_ref="weekly-energy").json()
            again = create(client_ref="weekly-energy")
            assert (again.status_code, again.json()["id"]) == (200, first["id"])

    class WhenTheClientRefIsReusedForOtherContent:
        def it_answers_409(self, create: Create) -> None:
            create(client_ref="weekly-energy")
            assert create(client_ref="weekly-energy", title="Other").status_code == 409

    class WhenTheDurationIsMalformed:
        def it_answers_400(self, create: Create) -> None:
            assert create(expires_in="soon").status_code == 400

    class WhenTheDurationIsZero:
        def it_answers_400(self, create: Create) -> None:
            assert create(expires_in="0m").status_code == 400

    class WhenTheDurationIsTooLong:
        def it_answers_400(self, create: Create) -> None:
            assert create(expires_in="99999999999d").status_code == 400

    class WhenTheDurationHasThousandsOfDigits:
        def it_answers_400(self, create: Create) -> None:
            assert create(expires_in="9" * 5000).status_code == 400

    class WhenTheClientIsOverItsWriteRate:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            return {"VITRINE_RATE_WRITES": "1/m"}

        def it_answers_429_with_retry_after(self, create: Create) -> None:
            create()
            response = create()
            assert (response.status_code, "Retry-After" in response.headers) == (429, True)


class DescribeUpdatingOverTheApi:
    class WhenFilesReplaceTheVersion:
        def it_adds_a_version(
            self, http: httpx2.Client, bearer: dict[str, str], published: PublishedArtifact
        ) -> None:
            response = http.put(f"/api/artifacts/{published.id}", files=[INDEX], headers=bearer)
            assert (response.status_code, response.json()["version"]) == (200, 2)

    class WhenFilesMerge:
        def it_keeps_the_files_already_there(
            self,
            http: httpx2.Client,
            bearer: dict[str, str],
            services: Services,
            published: PublishedArtifact,
        ) -> None:
            http.put(
                f"/api/artifacts/{published.id}",
                files=[CHART],
                data={"merge": "true"},
                headers=bearer,
            )
            paths = {file.path for file in services.artifacts.get(published.id).files}
            assert {"img/chart.png", "index.html", "notes.md"} <= paths

    class WhenTheIdIsUnknown:
        def it_answers_404(self, http: httpx2.Client, bearer: dict[str, str]) -> None:
            response = http.put("/api/artifacts/k7m2q9x4tz3wbn6r", files=[INDEX], headers=bearer)
            assert response.status_code == 404

    class WhenTheIdIsMalformed:
        def it_answers_404(self, http: httpx2.Client, bearer: dict[str, str]) -> None:
            assert http.put("/api/artifacts/nope", files=[INDEX], headers=bearer).status_code == 404

    class WhenNoTokenIsSent:
        def it_answers_401(self, http: httpx2.Client, published: PublishedArtifact) -> None:
            assert http.put(f"/api/artifacts/{published.id}", files=[INDEX]).status_code == 401


class DescribeListingOverTheApi:
    class WhenAReaderLists:
        def it_sees_the_artifacts(
            self, http: httpx2.Client, reader: dict[str, str], published: PublishedArtifact
        ) -> None:
            items = http.get("/api/artifacts", headers=reader).json()["items"]
            assert [item["id"] for item in items] == [published.id]

        def it_gets_no_links(
            self, http: httpx2.Client, reader: dict[str, str], published: PublishedArtifact
        ) -> None:
            assert "url" not in http.get("/api/artifacts", headers=reader).json()["items"][0]

    class WhenAFilterMatchesNothing:
        def it_returns_an_empty_page(
            self, http: httpx2.Client, reader: dict[str, str], published: PublishedArtifact
        ) -> None:
            response = http.get("/api/artifacts", params={"tag": "none"}, headers=reader)
            assert response.json()["items"] == []

    class WhenTheLimitIsOutOfRange:
        def it_answers_400(self, http: httpx2.Client, reader: dict[str, str]) -> None:
            response = http.get("/api/artifacts", params={"limit": "1000"}, headers=reader)
            assert response.status_code == 400

    class WhenNoTokenIsSent:
        def it_answers_401(self, http: httpx2.Client) -> None:
            assert http.get("/api/artifacts").status_code == 401
