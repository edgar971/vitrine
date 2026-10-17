"""The dashboard's session in Chromium (spec § 7.6, security T24).

The cookie is ``SameSite=Strict`` and scoped to ``/_admin``, and agent HTML runs in an opaque
origin, so a page an agent wrote cannot ride the operator's session: its requests to the
dashboard arrive without the cookie.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Request

from mcp_vitrine.domain.models import ArtifactDraft, PublishedArtifact
from mcp_vitrine.services.container import Services
from tests.conftest import ADMIN_HASH, ADMIN_PASSWORD

pytestmark = pytest.mark.browser

COOKIE = "vitrine_admin="
PROBE = b"""<!doctype html><body><script>
  fetch("/_admin/", {credentials: "include", redirect: "manual"})
    .finally(() => { document.body.dataset.done = "1"; });
</script></body>"""


@pytest.fixture
def overrides() -> dict[str, str]:
    """The dashboard on."""
    return {"VITRINE_ADMIN_PASSWORD": ADMIN_HASH}


@pytest.fixture
def probe(services: Services) -> PublishedArtifact:
    """A page that asks the dashboard for its list, with credentials."""
    return services.artifacts.create(
        ArtifactDraft(title="Probe", files={"index.html": PROBE}), client="hermes"
    )


@pytest.fixture
def signed_in(page: Page, base: str) -> Page:
    """A browser signed in through the login form."""
    page.goto(f"{base}/_admin/login")
    page.fill("input[name=password]", ADMIN_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{base}/_admin/")
    return page


def _cookie(request: Request) -> str:
    """The ``Cookie`` header a request went out with."""
    return request.all_headers().get("cookie", "")


class DescribeTheDashboardSessionInChromium:
    class WhenTheDashboardIsBrowsed:
        def it_sends_the_session_cookie(self, signed_in: Page, base: str) -> None:
            with signed_in.expect_request(f"{base}/_admin/audit") as sent:
                signed_in.goto(f"{base}/_admin/audit")
            assert COOKIE in _cookie(sent.value)

    class WhenAnArtifactPageCallsTheDashboard:
        def it_does_not_send_the_session_cookie_from_an_artifact_page(
            self, signed_in: Page, base: str, probe: PublishedArtifact
        ) -> None:
            with signed_in.expect_request(f"{base}/_admin/") as sent:
                signed_in.goto(probe.url)
            assert COOKIE not in _cookie(sent.value)
