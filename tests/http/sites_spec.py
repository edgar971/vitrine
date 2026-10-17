"""Zip sites, the SPA fallback and streamed bodies over real HTTP (spec § 7.3, § 4.3)."""

from __future__ import annotations

import io
import zipfile

import httpx2
import pytest

from mcp_vitrine.config import Settings
from mcp_vitrine.domain import layout
from mcp_vitrine.domain.errors import NotFound
from mcp_vitrine.domain.models import ArtifactDraft, PublishedArtifact, UploadGrant
from mcp_vitrine.services.container import Services
from tests.http.conftest import CLIENT, signature

SITE = {
    "dist/index.html": b"<h1>app</h1>",
    "dist/assets/app.js": b"console.log(1)",
    "dist/404.html": b"<h1>gone</h1>",
    "__MACOSX/._index.html": b"junk",
}


def zipped(entries: dict[str, bytes]) -> bytes:
    """A zip archive of ``entries``."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as writer:
        for name, data in entries.items():
            writer.writestr(name, data)
    return buffer.getvalue()


def evil() -> bytes:
    """An archive with one good entry and one that climbs out of the version."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        writer.writestr("index.html", b"x")
        writer.writestr(zipfile.ZipInfo("../escape.html"), b"x")
    return buffer.getvalue()


@pytest.fixture
def grant(services: Services) -> UploadGrant:
    """An upload URL for a new site, as ``artifact_upload_url(unpack="zip")`` issues it."""
    return services.artifacts.upload_url(None, client=CLIENT, unpack=True)


@pytest.fixture
def site(services: Services) -> PublishedArtifact:
    """A private single-page app whose missing routes serve its index."""
    return services.artifacts.create(
        ArtifactDraft(
            title="App",
            files={"index.html": b"<h1>app</h1>", "app.js": b"1", "404.html": b"<h1>gone</h1>"},
            fallback="index.html",
        ),
        client=CLIENT,
    )


def spools(settings: Settings) -> list[str]:
    """The spool directories left in ``data_dir/tmp``."""
    return [path.name for path in (settings.data_dir / layout.TMP_DIR).iterdir()]


class DescribeUploadingAZipSite:
    class WhenTheArchiveIsValid:
        def it_answers_201(self, http: httpx2.Client, grant: UploadGrant) -> None:
            assert http.put(grant.put_url, content=zipped(SITE)).status_code == 201

        def it_strips_the_shared_top_level_folder(
            self, http: httpx2.Client, grant: UploadGrant, services: Services
        ) -> None:
            http.put(grant.put_url, content=zipped(SITE))
            paths = [file.path for file in services.artifacts.get(grant.id).files]
            assert paths == ["404.html", "assets/app.js", "index.html"]

        def it_reports_the_files_and_the_skipped_junk(
            self, http: httpx2.Client, grant: UploadGrant
        ) -> None:
            body = http.put(grant.put_url, content=zipped(SITE)).json()
            assert (body["files"], body["skipped"], body["warnings"]) == (3, 1, [])

        def it_serves_the_site(self, http: httpx2.Client, grant: UploadGrant) -> None:
            url = http.put(grant.put_url, content=zipped(SITE)).json()["url"]
            assert http.get(f"/s/{signature(url)}/assets/app.js").text == "console.log(1)"

        def it_leaves_no_spool_behind(
            self, http: httpx2.Client, grant: UploadGrant, settings: Settings
        ) -> None:
            http.put(grant.put_url, content=zipped(SITE))
            assert spools(settings) == []

    class WhenTheEntryUsesRootAbsoluteUrls:
        def it_warns(self, http: httpx2.Client, grant: UploadGrant) -> None:
            page = {"index.html": b'<script src="/assets/app.js"></script>'}
            assert http.put(grant.put_url, content=zipped(page)).json()["warnings"]

    class WhenOneEntryEscapes:
        def it_answers_400(self, http: httpx2.Client, grant: UploadGrant) -> None:
            assert http.put(grant.put_url, content=evil()).status_code == 400

        def it_writes_nothing(
            self, http: httpx2.Client, grant: UploadGrant, services: Services
        ) -> None:
            http.put(grant.put_url, content=evil())
            with pytest.raises(NotFound):
                services.artifacts.get(grant.id)

        def it_leaves_no_spool_behind(
            self, http: httpx2.Client, grant: UploadGrant, settings: Settings
        ) -> None:
            http.put(grant.put_url, content=evil())
            assert spools(settings) == []

    class WhenTheBodyIsNotAZip:
        def it_answers_400(self, http: httpx2.Client, grant: UploadGrant) -> None:
            assert http.put(grant.put_url, content=b"%PDF-1.7").status_code == 400

    class WhenTheUnpackFormatIsUnknown:
        def it_answers_400(self, http: httpx2.Client, grant: UploadGrant) -> None:
            url = grant.put_url.replace("unpack=zip", "unpack=tar")
            assert http.put(url, content=zipped(SITE)).status_code == 400

    class WhenTheArchiveAddsAVersion:
        def it_replaces_the_files(
            self, http: httpx2.Client, services: Services, site: PublishedArtifact
        ) -> None:
            grant = services.artifacts.upload_url(
                None, client=CLIENT, artifact_id=site.id, unpack=True
            )
            http.put(grant.put_url, content=zipped({"index.html": b"v2"}))
            assert [file.path for file in services.artifacts.get(site.id).files] == ["index.html"]

        def it_overlays_them_with_merge(
            self, http: httpx2.Client, services: Services, site: PublishedArtifact
        ) -> None:
            grant = services.artifacts.upload_url(
                None, client=CLIENT, artifact_id=site.id, unpack=True
            )
            http.put(f"{grant.put_url}&merge=true", content=zipped({"extra.html": b"x"}))
            assert len(services.artifacts.get(site.id).files) == 4


class DescribeCreatingASiteOverTheApi:
    class WhenOneZipPartIsSent:
        def it_unpacks_it(
            self, http: httpx2.Client, bearer: dict[str, str], services: Services
        ) -> None:
            response = http.post(
                "/api/artifacts",
                files=[("file", ("site.zip", zipped(SITE), "application/zip"))],
                data={"title": "App", "unpack": "zip", "fallback": "404.html"},
                headers=bearer,
            )
            detail = services.artifacts.get(response.json()["id"])
            assert (len(detail.files), detail.artifact.fallback) == (3, "404.html")

    class WhenTheClientLabelsTheZipAnotherType:
        @pytest.mark.parametrize(
            "kind", ["application/octet-stream", "application/x-zip-compressed"]
        )
        def it_unpacks_it(self, http: httpx2.Client, bearer: dict[str, str], kind: str) -> None:
            response = http.post(
                "/api/artifacts",
                files=[("file", ("site.zip", zipped(SITE), kind))],
                data={"title": "App", "unpack": "zip"},
                headers=bearer,
            )
            assert response.status_code == 201

    class WhenThePartIsNotAZip:
        def it_answers_400(self, http: httpx2.Client, bearer: dict[str, str]) -> None:
            response = http.post(
                "/api/artifacts",
                files=[("file", ("site.zip", b"<h1>not a zip</h1>", "application/zip"))],
                data={"title": "App", "unpack": "zip"},
                headers=bearer,
            )
            assert response.status_code == 400

    class WhenTwoPartsAreSent:
        def it_answers_400(self, http: httpx2.Client, bearer: dict[str, str]) -> None:
            part = ("file", ("a.zip", zipped(SITE), "application/zip"))
            response = http.post(
                "/api/artifacts",
                files=[part, ("file", ("b.zip", zipped(SITE), "application/zip"))],
                data={"title": "App", "unpack": "zip"},
                headers=bearer,
            )
            assert response.status_code == 400

    class WhenTheFallbackIsNotInTheVersion:
        def it_answers_400(self, http: httpx2.Client, bearer: dict[str, str]) -> None:
            response = http.post(
                "/api/artifacts",
                files=[("file", ("index.html", b"x"))],
                data={"title": "App", "fallback": "missing.html"},
                headers=bearer,
            )
            assert response.status_code == 400

    class WhenAFileIsStreamed:
        def it_leaves_no_spool_behind(
            self, http: httpx2.Client, bearer: dict[str, str], settings: Settings
        ) -> None:
            http.post(
                "/api/artifacts",
                files=[("file", ("index.html", b"x" * 300_000))],
                data={"title": "Big"},
                headers=bearer,
            )
            assert spools(settings) == []


class DescribeTheFallback:
    def it_serves_the_fallback_for_a_missing_route(
        self, http: httpx2.Client, site: PublishedArtifact
    ) -> None:
        response = http.get(f"/s/{signature(site.url)}/settings")
        assert (response.status_code, response.text) == (200, "<h1>app</h1>")

    def it_keeps_the_sandbox_on_the_fallback(
        self, http: httpx2.Client, site: PublishedArtifact
    ) -> None:
        response = http.get(f"/s/{signature(site.url)}/settings")
        assert "sandbox" in response.headers["content-security-policy"]

    def it_does_not_serve_the_fallback_for_a_missing_script(
        self, http: httpx2.Client, site: PublishedArtifact
    ) -> None:
        assert http.get(f"/s/{signature(site.url)}/missing.js").status_code == 404

    def it_serves_existing_files_as_they_are(
        self, http: httpx2.Client, site: PublishedArtifact
    ) -> None:
        assert http.get(f"/s/{signature(site.url)}/app.js").text == "1"

    class WhenTheRouteIsNested:
        def it_sends_relative_urls_back_to_the_site_root(
            self, http: httpx2.Client, site: PublishedArtifact
        ) -> None:
            response = http.get(f"/s/{signature(site.url)}/users/42/edit")
            assert response.text == '<base href="../../"><h1>app</h1>'

        def it_puts_the_base_inside_the_head(self, http: httpx2.Client, services: Services) -> None:
            published = services.artifacts.create(
                ArtifactDraft(
                    title="App",
                    files={"index.html": b"<html><head><title>a</title></head></html>"},
                    fallback="index.html",
                ),
                client=CLIENT,
            )
            response = http.get(f"/s/{signature(published.url)}/users/42")
            assert response.text.startswith('<html><head><base href="../">')

        def it_keeps_the_sandbox(self, http: httpx2.Client, site: PublishedArtifact) -> None:
            response = http.get(f"/s/{signature(site.url)}/users/42")
            assert "sandbox" in response.headers["content-security-policy"]

    class WhenThePageSetsItsOwnBase:
        def it_serves_the_page_as_stored(self, http: httpx2.Client, services: Services) -> None:
            page = b'<head><base href="/app/"></head>'
            published = services.artifacts.create(
                ArtifactDraft(title="App", files={"index.html": page}, fallback="index.html"),
                client=CLIENT,
            )
            assert http.get(f"/s/{signature(published.url)}/users/42").content == page

    class WhenTheRouteEndsInASlash:
        def it_serves_the_fallback(self, http: httpx2.Client, site: PublishedArtifact) -> None:
            assert http.get(f"/s/{signature(site.url)}/settings/").status_code == 200

    class WhenAPinnedVersionHadNoFallback:
        def it_answers_404_as_it_did_then(
            self, http: httpx2.Client, bearer: dict[str, str], services: Services
        ) -> None:
            published = services.artifacts.create(
                ArtifactDraft(title="App", files={"index.html": b"<h1>v1</h1>"}), client=CLIENT
            )
            http.put(
                f"/api/artifacts/{published.id}", data={"fallback": "index.html"}, headers=bearer
            )
            assert http.get(f"/s/{signature(published.url)}@1/nope").status_code == 404

        def it_serves_the_fallback_on_the_version_that_set_it(
            self, http: httpx2.Client, bearer: dict[str, str], services: Services
        ) -> None:
            published = services.artifacts.create(
                ArtifactDraft(title="App", files={"index.html": b"<h1>v1</h1>"}), client=CLIENT
            )
            http.put(
                f"/api/artifacts/{published.id}", data={"fallback": "index.html"}, headers=bearer
            )
            assert http.get(f"/s/{signature(published.url)}@2/nope").status_code == 200

    class WhenTheFallbackIsA404Page:
        def it_answers_404_with_the_page(
            self, http: httpx2.Client, bearer: dict[str, str], site: PublishedArtifact
        ) -> None:
            http.put(f"/api/artifacts/{site.id}", data={"fallback": "404.html"}, headers=bearer)
            response = http.get(f"/s/{signature(site.url)}/nope")
            assert (response.status_code, response.text) == (404, "<h1>gone</h1>")

    class WhenTheFallbackIsCleared:
        def it_answers_a_plain_404(
            self, http: httpx2.Client, bearer: dict[str, str], site: PublishedArtifact
        ) -> None:
            http.put(f"/api/artifacts/{site.id}", data={"fallback": ""}, headers=bearer)
            assert http.get(f"/s/{signature(site.url)}/nope").text == "not found"

    class WhenAnUpdateDropsTheFallbackFile:
        def it_answers_400(
            self, http: httpx2.Client, bearer: dict[str, str], site: PublishedArtifact
        ) -> None:
            response = http.put(
                f"/api/artifacts/{site.id}",
                files=[("file", ("main.html", b"x"))],
                data={"entry": "main.html"},
                headers=bearer,
            )
            assert response.status_code == 400
