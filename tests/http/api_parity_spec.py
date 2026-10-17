"""``/api/artifacts/{id}``: get, delete, metadata, links and revoke over HTTP (spec § 7.1)."""

from __future__ import annotations

from collections.abc import Callable

import httpx2
import pytest

from mcp_vitrine.domain.models import (
    TAG_LIMIT,
    ArtifactDraft,
    GateDecision,
    PublishedArtifact,
    VersionDraft,
    Visibility,
)
from mcp_vitrine.services.audit import AuditEvent, AuditEventName, DenialReason
from mcp_vitrine.services.container import Services
from tests.http.conftest import CLIENT, READ_TOKEN, signature

UNKNOWN = "k7m2q9x4tz3wbn6r"


@pytest.fixture
def reader() -> dict[str, str]:
    """The header of the client that may only read."""
    return {"Authorization": f"Bearer {READ_TOKEN}"}


@pytest.fixture
def events(services: Services) -> Callable[[], tuple[AuditEvent, ...]]:
    """The audit log as it stands, newest first."""
    return services.audit.query


@pytest.fixture
def artifact(services: Services) -> PublishedArtifact:
    """One private artifact of one page."""
    return services.artifacts.create(
        ArtifactDraft(title="Energy", files={"index.html": b"<h1>hi</h1>"}, tags=("q3",)),
        client=CLIENT,
    )


class DescribeGettingOverTheApi:
    class WhenTheTokenReads:
        def it_returns_the_metadata(
            self, http: httpx2.Client, reader: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            body = http.get(f"/api/artifacts/{artifact.id}", headers=reader).json()
            assert (body["artifact"]["title"], body["version"]) == ("Energy", 1)

        def it_lists_the_files(
            self, http: httpx2.Client, reader: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            body = http.get(f"/api/artifacts/{artifact.id}", headers=reader).json()
            assert [file["path"] for file in body["files"]] == ["index.html"]

        def it_returns_no_url(
            self, http: httpx2.Client, reader: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            response = http.get(f"/api/artifacts/{artifact.id}", headers=reader)
            assert "/s/" not in response.text

    class WhenALinkHasAPasscode:
        def it_never_returns_the_hash(
            self,
            http: httpx2.Client,
            reader: dict[str, str],
            services: Services,
            artifact: PublishedArtifact,
        ) -> None:
            services.artifacts.link(artifact.id, client=CLIENT, passcode=True)
            assert "hash" not in http.get(f"/api/artifacts/{artifact.id}", headers=reader).text

    class WhenAVersionIsPinned:
        def it_returns_that_version(
            self,
            http: httpx2.Client,
            reader: dict[str, str],
            services: Services,
            artifact: PublishedArtifact,
        ) -> None:
            services.artifacts.update(
                artifact.id,
                VersionDraft(files={"index.html": b"two"}),
                client=CLIENT,
            )
            body = http.get(f"/api/artifacts/{artifact.id}?version=1", headers=reader).json()
            assert body["version"] == 1

    @pytest.mark.parametrize("version", ["0", "x", "1" * 20])
    def it_refuses_a_version_that_is_not_one(
        self,
        http: httpx2.Client,
        reader: dict[str, str],
        artifact: PublishedArtifact,
        version: str,
    ) -> None:
        response = http.get(f"/api/artifacts/{artifact.id}?version={version}", headers=reader)
        assert response.status_code == 400

    class WhenTheIdIsUnknown:
        def it_answers_404(self, http: httpx2.Client, reader: dict[str, str]) -> None:
            assert http.get(f"/api/artifacts/{UNKNOWN}", headers=reader).status_code == 404

    class WhenNoTokenIsSent:
        def it_answers_401(self, http: httpx2.Client, artifact: PublishedArtifact) -> None:
            assert http.get(f"/api/artifacts/{artifact.id}").status_code == 401


class DescribeDeletingOverTheApi:
    class WhenTheTokenWrites:
        def it_deletes_the_artifact(
            self,
            http: httpx2.Client,
            bearer: dict[str, str],
            artifact: PublishedArtifact,
        ) -> None:
            response = http.delete(f"/api/artifacts/{artifact.id}", headers=bearer)
            assert response.json() == {"deleted": artifact.id}

        def it_answers_404_afterwards(
            self,
            http: httpx2.Client,
            bearer: dict[str, str],
            artifact: PublishedArtifact,
        ) -> None:
            http.delete(f"/api/artifacts/{artifact.id}", headers=bearer)
            assert http.get(f"/api/artifacts/{artifact.id}", headers=bearer).status_code == 404

        def it_audits_the_deletion(
            self,
            http: httpx2.Client,
            bearer: dict[str, str],
            artifact: PublishedArtifact,
            events: Callable[[], tuple[AuditEvent, ...]],
        ) -> None:
            http.delete(f"/api/artifacts/{artifact.id}", headers=bearer)
            assert (events()[0].event, events()[0].client) == (AuditEventName.DELETED, CLIENT)

    class WhenTheTokenHasOnlyRead:
        def it_answers_403_and_audits_access_denied(
            self,
            http: httpx2.Client,
            reader: dict[str, str],
            artifact: PublishedArtifact,
            events: Callable[[], tuple[AuditEvent, ...]],
        ) -> None:
            assert http.delete(f"/api/artifacts/{artifact.id}", headers=reader).status_code == 403
            event = events()[0]
            assert (event.event, event.reason, event.route) == (
                AuditEventName.ACCESS_DENIED,
                DenialReason.SCOPE,
                "api_delete",
            )

        def it_keeps_the_artifact(
            self,
            http: httpx2.Client,
            reader: dict[str, str],
            services: Services,
            artifact: PublishedArtifact,
        ) -> None:
            http.delete(f"/api/artifacts/{artifact.id}", headers=reader)
            assert services.artifacts.get(artifact.id).version == 1

    class WhenTheIdIsUnknown:
        def it_answers_404(self, http: httpx2.Client, bearer: dict[str, str]) -> None:
            assert http.delete(f"/api/artifacts/{UNKNOWN}", headers=bearer).status_code == 404


class DescribePatchingOverTheApi:
    class WhenTagsAreAdded:
        def it_keeps_the_others(
            self,
            http: httpx2.Client,
            bearer: dict[str, str],
            artifact: PublishedArtifact,
        ) -> None:
            response = http.patch(
                f"/api/artifacts/{artifact.id}", json={"add_tags": ["energy"]}, headers=bearer
            )
            assert response.json()["tags"] == ["q3", "energy"]

        def it_writes_no_version(
            self,
            http: httpx2.Client,
            bearer: dict[str, str],
            services: Services,
            artifact: PublishedArtifact,
        ) -> None:
            http.patch(f"/api/artifacts/{artifact.id}", json={"title": "Power"}, headers=bearer)
            detail = services.artifacts.get(artifact.id)
            assert (detail.artifact.title, detail.version) == ("Power", 1)

    class WhenTheProjectIsEmpty:
        def it_clears_it(
            self,
            http: httpx2.Client,
            bearer: dict[str, str],
            services: Services,
            artifact: PublishedArtifact,
        ) -> None:
            url = f"/api/artifacts/{artifact.id}"
            http.patch(url, json={"project": "q3-report"}, headers=bearer)
            assert http.patch(url, json={"project": ""}, headers=bearer).json()["project"] is None

    class WhenAVisibilityIsSent:
        def it_refuses_the_request(
            self,
            http: httpx2.Client,
            bearer: dict[str, str],
            services: Services,
            artifact: PublishedArtifact,
        ) -> None:
            response = http.patch(
                f"/api/artifacts/{artifact.id}",
                json={"title": "x", "visibility": "public"},
                headers=bearer,
            )
            assert response.status_code == 400
            assert services.artifacts.get(artifact.id).artifact.visibility is Visibility.PRIVATE

    class WhenNothingIsNamed:
        def it_answers_400(
            self, http: httpx2.Client, bearer: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            assert http.patch(f"/api/artifacts/{artifact.id}", headers=bearer).status_code == 400

    class WhenTagsAreReplacedAndAdded:
        def it_answers_400(
            self, http: httpx2.Client, bearer: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            body = {"tags": ["a"], "add_tags": ["b"]}
            response = http.patch(f"/api/artifacts/{artifact.id}", json=body, headers=bearer)
            assert response.status_code == 400

    class WhenTheBodyIsNotJson:
        def it_answers_415(
            self, http: httpx2.Client, bearer: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            response = http.patch(
                f"/api/artifacts/{artifact.id}", data={"title": "x"}, headers=bearer
            )
            assert response.status_code == 415

    class WhenTheJsonIsMalformed:
        def it_answers_400(
            self, http: httpx2.Client, bearer: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            response = http.patch(
                f"/api/artifacts/{artifact.id}",
                content=b"{",
                headers=bearer | {"Content-Type": "application/json"},
            )
            assert response.status_code == 400

    class WhenTheJsonIsNotAnObject:
        def it_answers_400(
            self, http: httpx2.Client, bearer: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            response = http.patch(f"/api/artifacts/{artifact.id}", json=["x"], headers=bearer)
            assert response.status_code == 400

    class WhenATagListIsLongerThanTheToolAllows:
        def it_answers_400(
            self, http: httpx2.Client, bearer: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            body = {"remove_tags": [f"t{n}" for n in range(TAG_LIMIT + 1)]}
            response = http.patch(f"/api/artifacts/{artifact.id}", json=body, headers=bearer)
            assert response.status_code == 400

    class WhenTheBodyIsOver16KiB:
        def it_answers_413(
            self, http: httpx2.Client, bearer: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            body = {"title": "x" * (17 * 1024)}
            response = http.patch(f"/api/artifacts/{artifact.id}", json=body, headers=bearer)
            assert response.status_code == 413

    class WhenTheTokenHasOnlyRead:
        def it_answers_403(
            self, http: httpx2.Client, reader: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            response = http.patch(
                f"/api/artifacts/{artifact.id}", json={"title": "x"}, headers=reader
            )
            assert response.status_code == 403


class DescribeMintingALinkOverTheApi:
    class WhenTheArtifactIsPrivate:
        def it_returns_a_recorded_private_link(
            self, http: httpx2.Client, bearer: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            body = http.post(f"/api/artifacts/{artifact.id}/links", headers=bearer).json()
            assert "/s/" in body["url"]
            assert body["lid"]

        def it_opens_the_artifact(
            self, http: httpx2.Client, bearer: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            url = http.post(f"/api/artifacts/{artifact.id}/links", headers=bearer).json()["url"]
            assert http.get(f"/s/{signature(url)}/").text == "<h1>hi</h1>"

        def it_audits_link_issued(
            self,
            http: httpx2.Client,
            bearer: dict[str, str],
            artifact: PublishedArtifact,
            events: Callable[[], tuple[AuditEvent, ...]],
        ) -> None:
            http.post(
                f"/api/artifacts/{artifact.id}/links", json={"label": "finance"}, headers=bearer
            )
            assert events()[0].event is AuditEventName.LINK_ISSUED

    class WhenAPasscodeIsAsked:
        def it_returns_it_once(
            self,
            http: httpx2.Client,
            bearer: dict[str, str],
            artifact: PublishedArtifact,
        ) -> None:
            url = f"/api/artifacts/{artifact.id}/links"
            minted = http.post(url, json={"passcode": True}, headers=bearer).json()
            assert len(minted["passcode"]) == len("xxxxx-xxxxx")
            detail = http.get(f"/api/artifacts/{artifact.id}", headers=bearer).text
            assert minted["passcode"] not in detail

        def it_locks_the_link(
            self, http: httpx2.Client, bearer: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            url = f"/api/artifacts/{artifact.id}/links"
            minted = http.post(url, json={"passcode": True}, headers=bearer).json()
            assert http.get(f"/s/{signature(minted['url'])}/").status_code == 401

    class WhenAPasscodeIsSupplied:
        def it_does_not_return_it(
            self, http: httpx2.Client, bearer: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            url = f"/api/artifacts/{artifact.id}/links"
            minted = http.post(url, json={"passcode": "correct horse"}, headers=bearer).json()
            assert minted["passcode"] is None

    class WhenTheSuppliedPasscodeIsTooShort:
        def it_answers_400_without_echoing_it(
            self, http: httpx2.Client, bearer: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            url = f"/api/artifacts/{artifact.id}/links"
            response = http.post(url, json={"passcode": "s3cr"}, headers=bearer)
            assert response.status_code == 400
            assert "s3cr" not in response.text

    class WhenTheArtifactIsPublic:
        def it_returns_the_public_url(
            self,
            http: httpx2.Client,
            bearer: dict[str, str],
            services: Services,
            artifact: PublishedArtifact,
        ) -> None:
            services.artifacts.set_visibility(
                artifact.id, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.APPROVED
            )
            body = http.post(f"/api/artifacts/{artifact.id}/links", headers=bearer).json()
            assert f"/a/{artifact.id}/" in body["url"]

    class WhenTheIdIsPrivateAndUnknown:
        def it_answers_the_same_404(
            self, http: httpx2.Client, bearer: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            missing = http.post(f"/api/artifacts/{UNKNOWN}/links", headers=bearer)
            malformed = http.post("/api/artifacts/not-an-id/links", headers=bearer)
            assert (missing.status_code, missing.json()) == (
                malformed.status_code,
                malformed.json(),
            )
            assert missing.status_code == 404

    class WhenTheTokenHasOnlyRead:
        def it_answers_403_and_audits_access_denied(
            self,
            http: httpx2.Client,
            reader: dict[str, str],
            artifact: PublishedArtifact,
            events: Callable[[], tuple[AuditEvent, ...]],
        ) -> None:
            response = http.post(f"/api/artifacts/{artifact.id}/links", headers=reader)
            assert response.status_code == 403
            assert events()[0].route == "api_link"

    class WhenAnUnknownFieldIsSent:
        def it_answers_400(
            self, http: httpx2.Client, bearer: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            url = f"/api/artifacts/{artifact.id}/links"
            assert http.post(url, json={"public": True}, headers=bearer).status_code == 400

    class WhenTheClientIsOverItsLinkRate:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            return {"VITRINE_RATE_LINKS": "1/m"}

        def it_answers_429_with_retry_after(
            self, http: httpx2.Client, bearer: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            url = f"/api/artifacts/{artifact.id}/links"
            http.post(url, headers=bearer)
            response = http.post(url, headers=bearer)
            assert response.status_code == 429
            assert "Retry-After" in response.headers


class DescribeRevokingOneLinkOverTheApi:
    def it_keeps_the_other_links_working(
        self,
        http: httpx2.Client,
        bearer: dict[str, str],
        services: Services,
        artifact: PublishedArtifact,
    ) -> None:
        first = services.artifacts.link(artifact.id, client=CLIENT)
        second = services.artifacts.link(artifact.id, client=CLIENT)
        response = http.post(
            f"/api/artifacts/{artifact.id}/revoke", json={"link": first.lid}, headers=bearer
        )
        assert response.json() == {"id": artifact.id, "link_epoch": 0, "link": first.lid}
        assert http.get(f"/s/{signature(first.url)}/").status_code == 404
        assert http.get(f"/s/{signature(second.url)}/").status_code == 200

    class WhenNoLinkIsNamed:
        def it_revokes_every_link(
            self,
            http: httpx2.Client,
            bearer: dict[str, str],
            services: Services,
            artifact: PublishedArtifact,
        ) -> None:
            link = services.artifacts.link(artifact.id, client=CLIENT)
            body = http.post(f"/api/artifacts/{artifact.id}/revoke", headers=bearer).json()
            assert body["link_epoch"] == 1
            assert http.get(f"/s/{signature(link.url)}/").status_code == 404

    class WhenTheLidIsMalformed:
        def it_answers_400(
            self, http: httpx2.Client, bearer: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            url = f"/api/artifacts/{artifact.id}/revoke"
            assert http.post(url, json={"link": "../x"}, headers=bearer).status_code == 400

    class WhenTheTokenHasOnlyRead:
        def it_answers_403(
            self, http: httpx2.Client, reader: dict[str, str], artifact: PublishedArtifact
        ) -> None:
            url = f"/api/artifacts/{artifact.id}/revoke"
            assert http.post(url, headers=reader).status_code == 403
