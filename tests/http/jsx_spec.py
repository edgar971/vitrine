"""JSX/TSX artifacts and the ``/_vitrine`` runtime over real HTTP (spec § 7.1, § 7.2)."""

from __future__ import annotations

import httpx2
import pytest

from mcp_vitrine.domain.models import ArtifactDraft, PublishedArtifact
from mcp_vitrine.services.container import Services
from mcp_vitrine.web.jsx import JsxBundle
from tests.http.conftest import CLIENT, signature

SOURCE = b"export default function App() { return <p>hi</p>; }\n"


@pytest.fixture
def component(services: Services) -> PublishedArtifact:
    """A private artifact whose only file is a component."""
    return services.artifacts.create(
        ArtifactDraft(title="App", files={"App.tsx": SOURCE}), client=CLIENT
    )


class DescribeAJsxArtifact:
    class WhenItIsOpened:
        def it_serves_the_shell(self, http: httpx2.Client, component: PublishedArtifact) -> None:
            assert 'id="vitrine-source"' in http.get(component.url).text

        def it_keeps_the_sandbox_csp(
            self, http: httpx2.Client, component: PublishedArtifact
        ) -> None:
            policy = http.get(component.url).headers["content-security-policy"]
            assert policy.startswith("sandbox allow-scripts")
            assert "allow-same-origin" not in policy

    class WhenRawIsAsked:
        def it_serves_the_source_as_text(
            self, http: httpx2.Client, component: PublishedArtifact
        ) -> None:
            response = http.get(f"/s/{signature(component.url)}/App.tsx?raw=1")
            assert (response.headers["content-type"], response.content) == (
                "text/plain; charset=utf-8",
                SOURCE,
            )

    class WhenTheRendererIsOff:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            return {"VITRINE_RENDERERS": "md"}

        def it_serves_the_source_as_text(
            self, http: httpx2.Client, component: PublishedArtifact
        ) -> None:
            assert http.get(component.url).content == SOURCE


class DescribeTheStaticRoute:
    def it_serves_immutable_assets_with_cors(self, http: httpx2.Client) -> None:
        response = http.get(JsxBundle.load().url("react.js"))
        assert (
            response.status_code,
            response.headers["cache-control"],
            response.headers["access-control-allow-origin"],
        ) == (200, "public, max-age=31536000, immutable", "*")

    def it_serves_scripts_as_javascript(self, http: httpx2.Client) -> None:
        response = http.get(JsxBundle.load().url("mount.js"))
        assert response.headers["content-type"] == "text/javascript; charset=utf-8"

    def it_never_sniffs(self, http: httpx2.Client) -> None:
        response = http.get(JsxBundle.load().url("mount.js"))
        assert response.headers["x-content-type-options"] == "nosniff"

    class WhenThePathEscapes:
        @pytest.mark.parametrize(
            "path",
            [
                "/_vitrine/jsx/{bundle}/../manifest.json",
                "/_vitrine/jsx/{bundle}/%2e%2e/manifest.json",
                "/_vitrine/jsx/manifest.json",
                "/_vitrine/jsx/{bundle}/../../renderers.py",
                "/_vitrine/jsx/000000000000/react.js",
                "/_vitrine/jsx/{bundle}/missing.js",
                "/_vitrine/",
            ],
        )
        def it_answers_404(self, http: httpx2.Client, path: str) -> None:
            url = path.format(bundle=JsxBundle.load().bundle)
            assert http.get(url).status_code == 404
