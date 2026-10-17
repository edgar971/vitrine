"""The JSX/TSX renderer in Chromium with every request off vitrine blocked (spec § 7.2).

A component mounts in the sandbox's opaque origin from vitrine's vendored runtime alone, so it
renders on an offline machine and no viewer's browser trusts a third party.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page, Route

from mcp_vitrine.domain.models import ArtifactDraft
from mcp_vitrine.services.container import Services

pytestmark = pytest.mark.browser

APP = b"""import { useState } from "react";
import { BarChart, Bar } from "recharts";

type Props = { start?: number };

export default function App({ start = 2 }: Props) {
  const [count] = useState<number>(start);
  return (
    <main>
      <p id="count" className="text-red-600">{count}</p>
      <p id="origin">{String(window.origin)}</p>
      <BarChart width={100} height={50} data={[{ v: 1 }]}><Bar dataKey="v" /></BarChart>
    </main>
  );
}
"""
UNKNOWN_PACKAGE = b"""import confetti from "canvas-confetti";
export default function App() { return <p>{String(confetti)}</p>; }
"""
THROWS = b"export default function App() { throw new Error('boom'); }\n"

Open = Callable[[bytes, str], Page]


def message_of(page: Page) -> str:
    """The message the page shows instead of the component, once it shows one."""
    element = page.wait_for_selector(".vitrine-message")
    return "" if element is None else element.text_content() or ""


@pytest.fixture
def offline(page: Page, base: str) -> list[str]:
    """Abort every request whose host is not vitrine's; the list collects what was blocked."""
    host = urlsplit(base).netloc
    blocked: list[str] = []

    def guard(route: Route) -> None:
        if urlsplit(route.request.url).netloc in {host, ""} or route.request.url.startswith(
            ("blob:", "data:")
        ):
            route.continue_()
            return
        blocked.append(route.request.url)
        route.abort()

    page.route("**/*", guard)
    return blocked


@pytest.fixture
def open_component(page: Page, services: Services, offline: list[str]) -> Open:
    """Publish one component as the only file of an artifact, and open its link."""

    def show(source: bytes, name: str) -> Page:
        published = services.artifacts.create(
            ArtifactDraft(title="Component", files={name: source}), client="hermes"
        )
        page.goto(published.url)
        return page

    return show


class DescribeTheJsxRendererInChromium:
    class WhenATsxComponentIsOpened:
        def it_mounts_the_component_in_an_opaque_origin(self, open_component: Open) -> None:
            page = open_component(APP, "App.tsx")
            page.wait_for_selector("#count")
            assert (page.text_content("#count"), page.text_content("#origin")) == ("2", "null")

        def it_applies_tailwind_classes(self, open_component: Open) -> None:
            page = open_component(APP, "App.tsx")
            page.wait_for_selector("#count")
            page.wait_for_function(
                "getComputedStyle(document.querySelector('#count')).color !== 'rgb(0, 0, 0)'"
            )
            assert page.evaluate("getComputedStyle(document.querySelector('#count')).color") != (
                "rgb(0, 0, 0)"
            )

        def it_renders_a_vendored_package(self, open_component: Open) -> None:
            page = open_component(APP, "App.tsx")
            assert page.wait_for_selector("svg.recharts-surface") is not None

        def it_requests_nothing_outside_vitrine(
            self, open_component: Open, offline: list[str]
        ) -> None:
            page = open_component(APP, "App.tsx")
            page.wait_for_selector("svg.recharts-surface")
            assert offline == []

    class WhenAPackageIsNotVendored:
        def it_shows_a_message_for_an_unknown_package(self, open_component: Open) -> None:
            page = open_component(UNKNOWN_PACKAGE, "App.jsx")
            assert "package not available offline: canvas-confetti" in message_of(page)

    class WhenTheComponentThrows:
        def it_shows_the_error_instead_of_a_blank_page(self, open_component: Open) -> None:
            page = open_component(THROWS, "App.jsx")
            assert "boom" in message_of(page)

    class WhenTheSourceDoesNotCompile:
        def it_shows_the_syntax_error(self, open_component: Open) -> None:
            page = open_component(b"export default function App() { return <p>; }", "App.jsx")
            assert "does not compile" in message_of(page)

    class WhenThereIsNoDefaultExport:
        def it_says_so(self, open_component: Open) -> None:
            page = open_component(b"export const App = () => <p>x</p>;", "App.jsx")
            assert "no default export" in message_of(page)
