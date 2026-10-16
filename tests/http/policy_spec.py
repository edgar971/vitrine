"""The response policy of spec § 7.2 on the wire: types, the sandbox, renderers, Range, HEAD."""

from __future__ import annotations

import httpx2
import pytest

from mcp_vitrine.domain.models import PublishedArtifact
from mcp_vitrine.web.responses import SANDBOX_CSP


def _get(http: httpx2.Client, published: PublishedArtifact, path: str) -> httpx2.Response:
    """GET one file of the published artifact through its signed link."""
    return http.get(f"{published.url}{path}")


class DescribeTheSecurityHeaders:
    @pytest.mark.parametrize(
        ("header", "value"),
        [
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("X-Robots-Tag", "noindex, nofollow"),
        ],
    )
    def it_sets_them_on_every_artifact_response(
        self, http: httpx2.Client, published: PublishedArtifact, header: str, value: str
    ) -> None:
        assert _get(http, published, "img/a.png").headers[header] == value


class DescribeTheSandbox:
    @pytest.mark.parametrize("path", ["index.html", "img/dot.svg", "feed.xml", "notes.md"])
    def it_sandboxes_everything_a_browser_runs_script_in(
        self, http: httpx2.Client, published: PublishedArtifact, path: str
    ) -> None:
        assert _get(http, published, path).headers["Content-Security-Policy"] == SANDBOX_CSP

    def it_never_allows_the_same_origin(
        self, http: httpx2.Client, published: PublishedArtifact
    ) -> None:
        csp = _get(http, published, "index.html").headers["Content-Security-Policy"]
        assert "allow-same-origin" not in csp


class DescribeContentTypes:
    @pytest.mark.parametrize(
        ("path", "kind"),
        [
            ("index.html", "text/html; charset=utf-8"),
            ("img/a.png", "image/png"),
            ("clip.mp4", "video/mp4"),
            ("data.csv?raw=1", "text/csv; charset=utf-8"),
        ],
    )
    def it_serves_the_allowlisted_type(
        self, http: httpx2.Client, published: PublishedArtifact, path: str, kind: str
    ) -> None:
        assert _get(http, published, path).headers["Content-Type"] == kind

    class WhenTheExtensionIsNotAllowlisted:
        def it_serves_octet_stream(self, http: httpx2.Client, published: PublishedArtifact) -> None:
            assert _get(http, published, "blob.bin").headers["Content-Type"] == (
                "application/octet-stream"
            )

        def it_forces_a_download(self, http: httpx2.Client, published: PublishedArtifact) -> None:
            disposition = _get(http, published, "blob.bin").headers["Content-Disposition"]
            assert disposition.startswith("attachment")

    class WhenADownloadIsAskedFor:
        def it_forces_one(self, http: httpx2.Client, published: PublishedArtifact) -> None:
            disposition = _get(http, published, "index.html?download=1").headers[
                "Content-Disposition"
            ]
            assert disposition == 'attachment; filename="index.html"'


class DescribeTheRenderers:
    class WhenMarkdownIsViewed:
        def it_is_rendered_as_html(self, http: httpx2.Client, published: PublishedArtifact) -> None:
            assert "<h1>Notes</h1>" in _get(http, published, "notes.md").text

        def it_escapes_raw_html(self, http: httpx2.Client, published: PublishedArtifact) -> None:
            assert "<script>" not in _get(http, published, "notes.md").text

    class WhenRawIsAskedFor:
        def it_serves_the_file_itself(
            self, http: httpx2.Client, published: PublishedArtifact
        ) -> None:
            assert _get(http, published, "notes.md?raw=1").text.startswith("# Notes")

    class WhenACsvIsViewed:
        def it_is_a_table(self, http: httpx2.Client, published: PublishedArtifact) -> None:
            assert "<th>month</th>" in _get(http, published, "data.csv").text

    class WhenJsonIsViewed:
        def it_is_indented(self, http: httpx2.Client, published: PublishedArtifact) -> None:
            assert "{\n  &quot;a&quot;: 1\n}" in _get(http, published, "data.json").text

    class WhenTheOperatorDisabledTheRenderer:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            return {"VITRINE_RENDERERS": "csv"}

        def it_serves_the_file_itself(
            self, http: httpx2.Client, published: PublishedArtifact
        ) -> None:
            assert _get(http, published, "notes.md").headers["Content-Type"] == (
                "text/markdown; charset=utf-8"
            )


class DescribeMedia:
    class WhenARangeIsRequested:
        def it_answers_206(self, http: httpx2.Client, published: PublishedArtifact) -> None:
            response = http.get(f"{published.url}clip.mp4", headers={"Range": "bytes=2-5"})
            assert (response.status_code, response.content) == (206, b"2345")

    class WhenOnlyTheHeadersAreAskedFor:
        def it_sends_no_body(self, http: httpx2.Client, published: PublishedArtifact) -> None:
            response = http.head(f"{published.url}clip.mp4")
            assert (response.status_code, response.content) == (200, b"")
