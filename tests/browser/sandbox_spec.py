"""Agent-written HTML in Chromium (security T7): it runs, but in an opaque origin.

The script runs; ``document.cookie`` and ``localStorage`` throw even with a cookie set for
vitrine's host; ``window.origin`` is ``"null"``; and a relative image still loads under a signed
path.
"""

from __future__ import annotations

import base64
import json

import pytest
from playwright.sync_api import BrowserContext, Page

from mcp_vitrine.domain.models import ArtifactDraft, PublishedArtifact
from mcp_vitrine.services.container import Services

pytestmark = pytest.mark.browser

PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
PROBE = b"""<!doctype html><body><img src="img/pixel.png"
  onload="document.body.dataset.image='loaded'" onerror="document.body.dataset.image='failed'">
<script>
  const result = {ran: true, origin: String(window.origin)};
  try { result.cookie = document.cookie; } catch (error) { result.cookie = "threw"; }
  try { result.storage = localStorage.length; } catch (error) { result.storage = "threw"; }
  document.body.dataset.result = JSON.stringify(result);
</script></body>"""


@pytest.fixture
def probe(services: Services) -> PublishedArtifact:
    """A page that reports what its script can reach, plus an image beside it."""
    return services.artifacts.create(
        ArtifactDraft(title="Probe", files={"index.html": PROBE, "img/pixel.png": PIXEL}),
        client="hermes",
    )


@pytest.fixture
def opened(context: BrowserContext, page: Page, base: str, probe: PublishedArtifact) -> Page:
    """The probe, opened with a cookie already set for vitrine's host."""
    context.add_cookies([{"name": "session", "value": "secret", "url": base}])
    page.goto(probe.url)
    page.wait_for_function("document.body.dataset.result && document.body.dataset.image")
    return page


def _result(page: Page) -> dict[str, object]:
    """What the probe's script reported."""
    reported: dict[str, object] = json.loads(page.evaluate("document.body.dataset.result"))
    return reported


class DescribeTheSandboxInChromium:
    class WhenAgentHtmlIsViewed:
        def it_runs_the_script(self, opened: Page) -> None:
            assert _result(opened)["ran"] is True

        def it_runs_in_an_opaque_origin(self, opened: Page) -> None:
            assert _result(opened)["origin"] == "null"

        def it_cannot_read_vitrine_s_cookies(self, opened: Page) -> None:
            assert _result(opened)["cookie"] == "threw"

        def it_cannot_reach_storage(self, opened: Page) -> None:
            assert _result(opened)["storage"] == "threw"

        def it_still_loads_a_relative_image(self, opened: Page) -> None:
            assert opened.evaluate("document.body.dataset.image") == "loaded"
