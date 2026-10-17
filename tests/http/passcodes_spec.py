"""Recorded links over real HTTP: passcodes, the unlock flow and per-link revoke (spec § 7.1).

Security T22: a forwarded or unfurled passcode link shows vitrine's form, never the content, and
revoking one link ends it and its unlocked URLs without touching the others.
"""

from __future__ import annotations

import httpx2
import pytest

from mcp_vitrine.domain.models import ArtifactLink, PublishedArtifact
from mcp_vitrine.services.container import Services
from tests.http.conftest import CLIENT, signature

CONTENT = "<h1>hi</h1>"


@pytest.fixture
def locked(services: Services, published: PublishedArtifact) -> ArtifactLink:
    """A recorded link to the published artifact, with a generated passcode."""
    return services.artifacts.link(published.id, client=CLIENT, passcode=True)


@pytest.fixture
def plain(services: Services, published: PublishedArtifact) -> ArtifactLink:
    """A recorded link without a passcode."""
    return services.artifacts.link(published.id, client=CLIENT, label="sent to finance")


def _unlock(
    http: httpx2.Client,
    link: ArtifactLink,
    passcode: str,
    *,
    path: str = "",
    headers: dict[str, str] | None = None,
) -> httpx2.Response:
    """Post a passcode to a link's unlock route."""
    return http.post(
        f"/s/{signature(link.url)}/unlock",
        data={"passcode": passcode, "path": path},
        headers=headers,
    )


def _passcode(link: ArtifactLink) -> str:
    """The passcode a link was minted with."""
    assert link.passcode is not None
    return link.passcode


@pytest.fixture
def unlocked(http: httpx2.Client, locked: ArtifactLink) -> str:
    """The unlocked URL a right passcode leads to."""
    return _unlock(http, locked, _passcode(locked)).headers["Location"]


class DescribeAPasscodeLink:
    class WhenOpened:
        def it_shows_the_form_instead_of_the_content(
            self, http: httpx2.Client, locked: ArtifactLink
        ) -> None:
            response = http.get(locked.url)
            assert (response.status_code, CONTENT in response.text) == (401, False)

        def it_names_nothing_of_the_artifact(
            self, http: httpx2.Client, locked: ArtifactLink
        ) -> None:
            assert "Energy" not in http.get(locked.url).text

        def it_locks_every_file_not_only_the_entry(
            self, http: httpx2.Client, locked: ArtifactLink
        ) -> None:
            assert http.get(f"{locked.url}img/a.png").status_code == 401

        def it_carries_the_form_policy(self, http: httpx2.Client, locked: ArtifactLink) -> None:
            policy = http.get(locked.url).headers["Content-Security-Policy"]
            assert policy == (
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                "frame-ancestors 'none'"
            )

        def it_is_never_stored(self, http: httpx2.Client, locked: ArtifactLink) -> None:
            assert http.get(locked.url).headers["Cache-Control"] == "private, no-store"

        def it_sends_no_referrer_off_site(self, http: httpx2.Client, locked: ArtifactLink) -> None:
            assert http.get(locked.url).headers["Referrer-Policy"] == "same-origin"

    class WhenTheRightPasscodeIsSent:
        def it_redirects_with_see_other(self, http: httpx2.Client, locked: ArtifactLink) -> None:
            assert _unlock(http, locked, _passcode(locked)).status_code == 303

        def it_serves_the_content_at_the_unlocked_url(
            self, http: httpx2.Client, unlocked: str
        ) -> None:
            assert http.get(unlocked).text.startswith(CONTENT)

        def it_serves_the_relative_assets_there_too(
            self, http: httpx2.Client, unlocked: str
        ) -> None:
            assert http.get(f"{unlocked}img/a.png").status_code == 200

        def it_lands_on_the_path_that_was_asked_for(
            self, http: httpx2.Client, locked: ArtifactLink
        ) -> None:
            response = _unlock(http, locked, _passcode(locked), path="notes.md")
            assert response.headers["Location"].endswith("/notes.md")

        def it_ignores_case_and_hyphens(self, http: httpx2.Client, locked: ArtifactLink) -> None:
            typed = _passcode(locked).replace("-", "").upper()
            assert _unlock(http, locked, typed).status_code == 303

        def it_never_redirects_off_site(self, http: httpx2.Client, locked: ArtifactLink) -> None:
            response = _unlock(http, locked, _passcode(locked), path="//evil.example/x")
            assert response.headers["Location"].startswith("/s/")

    class WhenTheWrongPasscodeIsSent:
        def it_never_serves_content(self, http: httpx2.Client, locked: ArtifactLink) -> None:
            response = _unlock(http, locked, "wrong-passcode")
            assert (response.status_code, CONTENT in response.text) == (401, False)

        def it_says_only_that_it_did_not_work(
            self, http: httpx2.Client, locked: ArtifactLink
        ) -> None:
            assert "That passcode did not work." in _unlock(http, locked, "wrong-passcode").text

    class WhenTheLinkIsRevoked:
        def it_ends_unlocked_urls(
            self,
            http: httpx2.Client,
            unlocked: str,
            locked: ArtifactLink,
            services: Services,
            published: PublishedArtifact,
        ) -> None:
            services.artifacts.revoke_links(published.id, client=CLIENT, lid=_lid(locked))
            assert http.get(unlocked).status_code == 404

    class WhenEveryLinkIsRevoked:
        def it_ends_unlocked_urls(
            self,
            http: httpx2.Client,
            unlocked: str,
            services: Services,
            published: PublishedArtifact,
        ) -> None:
            services.artifacts.revoke_links(published.id, client=CLIENT)
            assert http.get(unlocked).status_code == 404

    class WhenAnUnlockedSigIsUsedAsAViewSig:
        def it_answers_404(self, http: httpx2.Client, unlocked: str) -> None:
            usig = signature(unlocked)
            response = http.post(f"/s/{usig}/unlock", data={"passcode": "anything"})
            assert response.status_code == 404

    class WhenTheBucketIsEmpty:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            return {"VITRINE_RATE_UNLOCK": "1/m"}

        @pytest.fixture
        def exhausted(self, http: httpx2.Client, locked: ArtifactLink) -> ArtifactLink:
            """The link, once this address has spent its only wrong passcode."""
            _unlock(http, locked, "wrong-passcode")
            return locked

        def it_answers_429(self, http: httpx2.Client, exhausted: ArtifactLink) -> None:
            assert _unlock(http, exhausted, "wrong-again").status_code == 429

        def it_says_when_to_retry(self, http: httpx2.Client, exhausted: ArtifactLink) -> None:
            assert int(_unlock(http, exhausted, "wrong-again").headers["Retry-After"]) > 0

        def it_does_not_check_the_passcode(
            self, http: httpx2.Client, exhausted: ArtifactLink
        ) -> None:
            assert _unlock(http, exhausted, _passcode(exhausted)).status_code == 429

        def it_leaves_other_links_alone(
            self,
            http: httpx2.Client,
            exhausted: ArtifactLink,
            services: Services,
            published: PublishedArtifact,
        ) -> None:
            other = services.artifacts.link(published.id, client=CLIENT, passcode=True)
            assert _unlock(http, other, _passcode(other)).status_code == 303

    class WhenRightPasscodesKeepComing:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            return {"VITRINE_RATE_UNLOCK": "1/m"}

        def it_spends_nothing(self, http: httpx2.Client, locked: ArtifactLink) -> None:
            _unlock(http, locked, _passcode(locked))
            assert _unlock(http, locked, _passcode(locked)).status_code == 303

    class WhenTheOriginIsForeign:
        def it_answers_403(self, http: httpx2.Client, locked: ArtifactLink) -> None:
            response = _unlock(
                http, locked, _passcode(locked), headers={"Origin": "https://evil.example"}
            )
            assert response.status_code == 403

        def it_refuses_the_opaque_origin_of_artifact_pages(
            self, http: httpx2.Client, locked: ArtifactLink
        ) -> None:
            response = _unlock(http, locked, _passcode(locked), headers={"Origin": "null"})
            assert response.status_code == 403

    class WhenTheBodyIsTooLarge:
        def it_answers_413(self, http: httpx2.Client, locked: ArtifactLink) -> None:
            assert _unlock(http, locked, "x" * 2048).status_code == 413


class DescribeALinkWithoutAPasscode:
    class WhenOpened:
        def it_serves_the_content(self, http: httpx2.Client, plain: ArtifactLink) -> None:
            assert http.get(plain.url).text.startswith(CONTENT)

    class WhenAnUnlockIsPosted:
        def it_redirects_to_the_link_itself(self, http: httpx2.Client, plain: ArtifactLink) -> None:
            response = _unlock(http, plain, "anything")
            assert response.headers["Location"] == f"/s/{signature(plain.url)}/"


class DescribeTwoLinks:
    @pytest.fixture
    def other(self, services: Services, published: PublishedArtifact) -> ArtifactLink:
        """A second recorded link to the same artifact."""
        return services.artifacts.link(published.id, client=CLIENT)

    @pytest.fixture
    def revoked(
        self, services: Services, published: PublishedArtifact, plain: ArtifactLink
    ) -> ArtifactLink:
        """The plain link, once it alone was revoked."""
        services.artifacts.revoke_links(published.id, client=CLIENT, lid=_lid(plain))
        return plain

    class WhenOneIsRevoked:
        def it_keeps_the_other_working(
            self, http: httpx2.Client, other: ArtifactLink, revoked: ArtifactLink
        ) -> None:
            assert http.get(other.url).status_code == 200

        def it_stops_the_revoked_one(self, http: httpx2.Client, revoked: ArtifactLink) -> None:
            assert http.get(revoked.url).status_code == 404

        def it_keeps_the_link_a_write_returned_working(
            self, http: httpx2.Client, published: PublishedArtifact, revoked: ArtifactLink
        ) -> None:
            assert http.get(published.url).status_code == 200


class DescribeAnOldLink:
    class WhenItHasNoLid:
        def it_still_verifies(self, http: httpx2.Client, published: PublishedArtifact) -> None:
            assert http.get(published.url).status_code == 200


def _lid(link: ArtifactLink) -> str:
    """The id a recorded link carries."""
    assert link.lid is not None
    return link.lid
