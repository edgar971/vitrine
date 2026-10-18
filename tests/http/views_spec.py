"""``/s/{sig}`` and ``/a/{id}`` over real HTTP: links, versions, traversal and headers (§ 7.1-7.2).

The view checks (security T4-T7, T10): headers, the indistinguishable 404, links that were
tampered, expired, revoked or of the wrong kind, versions and path traversal.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import httpx2
import pytest

from mcp_vitrine.domain.models import (
    ArtifactDraft,
    GateDecision,
    PublishedArtifact,
    VersionDraft,
    Visibility,
)
from mcp_vitrine.services.container import Services
from tests.conftest import FrozenClock
from tests.http.conftest import CLIENT, signature


@pytest.fixture
def public(services: Services, published: PublishedArtifact, base: str) -> str:
    """The URL of the published artifact once the gate has made it public."""
    services.artifacts.set_visibility(
        published.id, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.APPROVED
    )
    return f"{base}/a/{published.id}/"


@pytest.fixture
def unknown(http: httpx2.Client) -> httpx2.Response:
    """What an id nobody published answers, for comparing every other 404 with."""
    return http.get("/a/k7m2q9x4tz3wbn6r/")


class DescribeThePrivateView:
    class WhenTheLinkIsValid:
        def it_serves_the_entry(self, http: httpx2.Client, published: PublishedArtifact) -> None:
            assert http.get(published.url).text.startswith("<h1>hi</h1>")

        def it_serves_a_relative_asset(
            self, http: httpx2.Client, published: PublishedArtifact
        ) -> None:
            assert http.get(f"{published.url}img/a.png").status_code == 200

        def it_is_never_stored(self, http: httpx2.Client, published: PublishedArtifact) -> None:
            assert http.get(published.url).headers["Cache-Control"] == "private, no-store"

    class WhenTheMacIsTamperedWith:
        def it_answers_like_an_unknown_artifact(
            self, http: httpx2.Client, published: PublishedArtifact, unknown: httpx2.Response
        ) -> None:
            body, mac = signature(published.url).split(".")
            response = http.get(f"/s/{body}.{mac[::-1]}/")
            assert (response.status_code, response.text) == (404, unknown.text)

    class WhenTheLinkHasExpired:
        def it_answers_404(
            self,
            http: httpx2.Client,
            published: PublishedArtifact,
            advance: Callable[[timedelta], None],
        ) -> None:
            advance(timedelta(days=8))
            assert http.get(published.url).status_code == 404

    class WhenAnUploadSignatureIsUsedToView:
        def it_answers_404(self, http: httpx2.Client, services: Services) -> None:
            grant = services.artifacts.upload_url("a.pdf", client=CLIENT)
            assert http.get(f"/s/{signature(grant.put_url, '/u/')}/").status_code == 404

    class WhenTheLinksWereRevoked:
        def it_answers_404(
            self, http: httpx2.Client, services: Services, published: PublishedArtifact
        ) -> None:
            services.artifacts.revoke_links(published.id, client=CLIENT)
            assert http.get(published.url).status_code == 404

    class WhenTheArtifactWasDeleted:
        def it_answers_404(
            self, http: httpx2.Client, services: Services, published: PublishedArtifact
        ) -> None:
            services.artifacts.delete(published.id, client=CLIENT)
            assert http.get(published.url).status_code == 404

    class WhenTheArtifactHasExpired:
        def it_answers_410(
            self,
            http: httpx2.Client,
            services: Services,
            clock: FrozenClock,
            advance: Callable[[timedelta], None],
        ) -> None:
            expiring = services.artifacts.create(
                ArtifactDraft(
                    title="Soon",
                    files={"index.html": b"x"},
                    expires_at=clock.now() + timedelta(minutes=1),
                ),
                client=CLIENT,
            )
            advance(timedelta(minutes=2))
            assert http.get(expiring.url).status_code == 410

    class WhenTheLinkLacksItsTrailingSlash:
        def it_redirects_so_relative_assets_resolve(
            self, http: httpx2.Client, published: PublishedArtifact
        ) -> None:
            response = http.get(published.url.rstrip("/"))
            assert response.headers["Location"].endswith(f"{signature(published.url)}/")

        def it_redirects_relatively_whatever_the_scheme_seen(
            self, http: httpx2.Client, published: PublishedArtifact
        ) -> None:
            response = http.get(published.url.rstrip("/"))
            assert response.headers["Location"] == f"./{signature(published.url)}/"

        def it_keeps_the_redirect_out_of_caches(
            self, http: httpx2.Client, published: PublishedArtifact
        ) -> None:
            response = http.get(published.url.rstrip("/"))
            assert response.headers["Cache-Control"] == "private, no-store"


class DescribeAResponseStarletteBuilds:
    @pytest.mark.parametrize(
        ("method", "suffix", "headers"),
        [
            ("GET", "", {"Range": "bytes=999999-"}),
            ("GET", "", {"Range": "pages=1-2"}),
            ("POST", "index.html", {}),
        ],
        ids=["unsatisfiable range", "malformed range", "method not allowed"],
    )
    def it_carries_the_view_headers(
        self,
        http: httpx2.Client,
        published: PublishedArtifact,
        method: str,
        suffix: str,
        headers: dict[str, str],
    ) -> None:
        response = http.request(method, published.url + suffix, headers=headers)
        assert {
            name: response.headers.get(name)
            for name in ("X-Content-Type-Options", "Referrer-Policy", "Cache-Control")
        } == {
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "private, no-store",
        }


class DescribeVersionPinning:
    class WhenAnOlderVersionIsPinned:
        def it_serves_that_version(
            self, http: httpx2.Client, services: Services, published: PublishedArtifact
        ) -> None:
            services.artifacts.update(
                published.id,
                VersionDraft(files={"index.html": b"<h1>two</h1>"}, merge=True),
                client=CLIENT,
            )
            pinned = published.url.replace(
                signature(published.url), f"{signature(published.url)}@1"
            )
            assert http.get(pinned).text.startswith("<h1>hi</h1>")

    class WhenTheArtifactIsUpdated:
        def it_serves_the_new_version_under_the_same_link(
            self, http: httpx2.Client, services: Services, published: PublishedArtifact
        ) -> None:
            services.artifacts.update(
                published.id,
                VersionDraft(files={"index.html": b"<h1>two</h1>"}, merge=True),
                client=CLIENT,
            )
            assert http.get(published.url).text == "<h1>two</h1>"

    class WhenThePinnedVersionHadAnotherEntry:
        def it_opens_that_version_s_own_entry(
            self, http: httpx2.Client, services: Services
        ) -> None:
            report = services.artifacts.create(
                ArtifactDraft(title="Report", files={"report.txt": b"v1"}), client=CLIENT
            )
            services.artifacts.update(
                report.id, VersionDraft(files={"index.html": b"<p>v2</p>"}), client=CLIENT
            )
            pinned = report.url.replace(signature(report.url), f"{signature(report.url)}@1")
            assert http.get(pinned).text == "v1"

    @pytest.mark.parametrize("pin", ["@2", "@0", "@x", "@", "@-1", "@" + "9" * 5000])
    def it_answers_404_for_a_pin_that_names_no_kept_version(
        self, http: httpx2.Client, published: PublishedArtifact, pin: str
    ) -> None:
        pinned = published.url.replace(signature(published.url), f"{signature(published.url)}{pin}")
        assert http.get(pinned).status_code == 404


class DescribeTraversal:
    @pytest.mark.parametrize(
        "path",
        [
            "%2e%2e/meta.json",
            "..%2fmeta.json",
            "%2e%2e%2f%2e%2e%2fartifacts",
            "img/%2e%2e/%2e%2e/meta.json",
            ".hidden",
            "img/",
        ],
    )
    def it_answers_like_an_unknown_artifact(
        self,
        http: httpx2.Client,
        published: PublishedArtifact,
        unknown: httpx2.Response,
        path: str,
    ) -> None:
        response = http.get(f"/s/{signature(published.url)}/{path}")
        assert (response.status_code, response.text) == (404, unknown.text)


class DescribeThePublicView:
    class WhenTheArtifactIsPublic:
        def it_serves_it_without_credentials(self, http: httpx2.Client, public: str) -> None:
            assert http.get(public).status_code == 200

        def it_may_be_cached_briefly(self, http: httpx2.Client, public: str) -> None:
            assert http.get(public).headers["Cache-Control"] == "public, max-age=60"

    class WhenTheArtifactIsPrivate:
        def it_answers_like_an_unknown_artifact(
            self, http: httpx2.Client, published: PublishedArtifact, unknown: httpx2.Response
        ) -> None:
            response = http.get(f"/a/{published.id}/")
            assert (response.status_code, response.text) == (404, unknown.text)

    class WhenAVersionKeptFromBeforeItWentPublicIsPinned:
        @pytest.fixture
        def approved(self, services: Services, published: PublishedArtifact) -> str:
            """The id, made public at version 2 with version 1 still kept."""
            services.artifacts.update(
                published.id, VersionDraft(files={"index.html": b"<h1>two</h1>"}), client=CLIENT
            )
            services.artifacts.set_visibility(
                published.id,
                Visibility.PUBLIC,
                client=CLIENT,
                gate=GateDecision.APPROVED,
                seen_version=2,
            )
            return published.id

        def it_answers_like_an_unknown_artifact(
            self, http: httpx2.Client, approved: str, unknown: httpx2.Response
        ) -> None:
            response = http.get(f"/a/{approved}@1/")
            assert (response.status_code, response.text) == (404, unknown.text)

        def it_serves_the_approved_version_pinned(self, http: httpx2.Client, approved: str) -> None:
            assert http.get(f"/a/{approved}@2/").text.startswith("<h1>two</h1>")

    class WhenTheIdIsMalformed:
        def it_answers_404(self, http: httpx2.Client) -> None:
            assert http.get("/a/not-an-id/").status_code == 404

    class WhenAPrivateArtifactHasExpired:
        def it_still_answers_404_not_410(
            self,
            http: httpx2.Client,
            services: Services,
            clock: FrozenClock,
            advance: Callable[[timedelta], None],
        ) -> None:
            expiring = services.artifacts.create(
                ArtifactDraft(
                    title="Soon",
                    files={"index.html": b"x"},
                    expires_at=clock.now() + timedelta(minutes=1),
                ),
                client=CLIENT,
            )
            advance(timedelta(minutes=2))
            assert http.get(f"/a/{expiring.id}/").status_code == 404

    class WhenItIsMadePrivateAgain:
        def it_answers_404(
            self,
            http: httpx2.Client,
            services: Services,
            published: PublishedArtifact,
            public: str,
        ) -> None:
            services.artifacts.set_visibility(
                published.id, Visibility.PRIVATE, client=CLIENT, gate=GateDecision.NOT_REQUIRED
            )
            assert http.get(public).status_code == 404


class DescribeANestedEntry:
    class WhenTheLinkOpensTheArtifactRoot:
        def it_redirects_to_the_entry(self, http: httpx2.Client, services: Services) -> None:
            nested = services.artifacts.create(
                ArtifactDraft(
                    title="Site",
                    files={"site/index.html": b"<p>x</p>", "site/app.js": b"1"},
                    entry="site/index.html",
                ),
                client=CLIENT,
            )
            assert http.get(nested.url).headers["Location"] == "./site/index.html"
