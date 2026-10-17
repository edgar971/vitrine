"""View counts over real HTTP (spec § 11.4, security T23) and the public window (spec § 4.3)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import httpx2
import pytest

from mcp_vitrine.config import Settings
from mcp_vitrine.domain import layout
from mcp_vitrine.domain.models import (
    ArtifactLink,
    GateDecision,
    PublishedArtifact,
    Views,
    Visibility,
)
from mcp_vitrine.services.container import Services
from tests.conftest import FrozenClock
from tests.http.conftest import CLIENT

SLACK = "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)"
BROWSER = "Mozilla/5.0 (X11; Linux x86_64) vitrine-spec-browser"


def _views(services: Services, published: PublishedArtifact) -> Views:
    """The artifact's counts, as ``artifact_get`` reports them."""
    views = services.artifacts.get(published.id).views
    assert views is not None
    return views


def _on_disk(settings: Settings) -> str:
    """Everything the view log wrote."""
    return "".join(
        path.read_text(encoding="utf-8")
        for path in sorted((settings.data_dir / layout.VIEWS_DIR).glob("*.jsonl"))
    )


class DescribeViewCounting:
    class WhenAPageAndItsAssetsLoad:
        def it_counts_one_view(
            self, http: httpx2.Client, published: PublishedArtifact, services: Services
        ) -> None:
            http.get(published.url)
            http.get(f"{published.url}img/a.png")
            http.get(f"{published.url}img/dot.svg")
            assert _views(services, published).total == 1

    class WhenARenderedPageIsOpened:
        def it_counts_it(
            self, http: httpx2.Client, published: PublishedArtifact, services: Services
        ) -> None:
            http.get(f"{published.url}notes.md")
            assert _views(services, published).total == 1

    class WhenOnePersonReloads:
        def it_counts_one_visitor(
            self, http: httpx2.Client, published: PublishedArtifact, services: Services
        ) -> None:
            http.get(published.url, headers={"User-Agent": BROWSER})
            http.get(published.url, headers={"User-Agent": BROWSER})
            assert (_views(services, published).total, _views(services, published).visitors) == (
                2,
                1,
            )

    class WhenAnUnfurlerFetches:
        def it_counts_a_bot_not_a_view(
            self, http: httpx2.Client, published: PublishedArtifact, services: Services
        ) -> None:
            http.get(published.url, headers={"User-Agent": SLACK})
            assert (_views(services, published).total, _views(services, published).bots) == (0, 1)

    class WhenTheRequestIsNotSomeoneOpeningIt:
        def it_ignores_head(
            self, http: httpx2.Client, published: PublishedArtifact, services: Services
        ) -> None:
            http.head(published.url)
            assert _views(services, published).total == 0

        def it_ignores_a_range_request(
            self, http: httpx2.Client, published: PublishedArtifact, services: Services
        ) -> None:
            http.get(published.url, headers={"Range": "bytes=0-3"})
            assert _views(services, published).total == 0

        def it_ignores_a_prefetch(
            self, http: httpx2.Client, published: PublishedArtifact, services: Services
        ) -> None:
            http.get(published.url, headers={"Sec-Purpose": "prefetch"})
            assert _views(services, published).total == 0

    class WhenTheBucketIsEmpty:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            return {"VITRINE_RATE_VIEWS": "1/m"}

        def it_does_not_count(
            self, http: httpx2.Client, published: PublishedArtifact, services: Services
        ) -> None:
            http.get(published.url)
            http.get(published.url)
            assert _views(services, published).total == 1

    class WhenALinkHasALid:
        @pytest.fixture
        def labelled(self, services: Services, published: PublishedArtifact) -> ArtifactLink:
            """A recorded link with a label."""
            return services.artifacts.link(published.id, client=CLIENT, label="sent to finance")

        def it_attributes_the_view_to_it(
            self,
            http: httpx2.Client,
            labelled: ArtifactLink,
            published: PublishedArtifact,
            services: Services,
        ) -> None:
            http.get(labelled.url)
            link = _views(services, published).links[0]
            assert (link.lid, link.label, link.views) == (labelled.lid, "sent to finance", 1)

        def it_counts_a_public_view_under_no_link(
            self, http: httpx2.Client, published: PublishedArtifact, services: Services
        ) -> None:
            http.get(published.url)
            assert _views(services, published).links == ()

    class WhenAViewIsCounted:
        def it_never_writes_an_ip_or_user_agent(
            self,
            http: httpx2.Client,
            published: PublishedArtifact,
            services: Services,
            settings: Settings,
        ) -> None:
            http.get(published.url, headers={"User-Agent": BROWSER})
            services.views.flush()
            written = _on_disk(settings)
            assert (written != "", "127.0.0.1" in written, "vitrine-spec-browser" in written) == (
                True,
                False,
                False,
            )

    class WhenViewLogIsOff:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            return {"VITRINE_VIEW_LOG": "off"}

        def it_writes_nothing(
            self,
            http: httpx2.Client,
            published: PublishedArtifact,
            services: Services,
            settings: Settings,
        ) -> None:
            http.get(published.url)
            services.views.flush()
            assert _on_disk(settings) == ""

        def it_reports_no_views(
            self, http: httpx2.Client, published: PublishedArtifact, services: Services
        ) -> None:
            http.get(published.url)
            assert services.artifacts.get(published.id).views is None


class DescribeScheduledUnpublish:
    @pytest.fixture
    def windowed(self, services: Services, published: PublishedArtifact, clock: FrozenClock) -> str:
        """The published artifact, made public for one day."""
        services.artifacts.set_visibility(
            published.id,
            Visibility.PUBLIC,
            client=CLIENT,
            gate=GateDecision.APPROVED,
            public_until=clock.now() + timedelta(days=1),
        )
        return f"/a/{published.id}/"

    class WhenTheWindowIsOpen:
        def it_serves_the_public_url(self, http: httpx2.Client, windowed: str) -> None:
            assert http.get(windowed).status_code == 200

    class WhenPublicUntilHasPassed:
        def it_answers_404_on_the_public_url_before_the_sweep(
            self, http: httpx2.Client, windowed: str, advance: Callable[[timedelta], None]
        ) -> None:
            advance(timedelta(days=1))
            assert http.get(windowed).status_code == 404

        def it_answers_like_an_unknown_artifact(
            self, http: httpx2.Client, windowed: str, advance: Callable[[timedelta], None]
        ) -> None:
            advance(timedelta(days=1))
            unknown = http.get("/a/k7m2q9x4tz3wbn6r/")
            assert http.get(windowed).text == unknown.text
