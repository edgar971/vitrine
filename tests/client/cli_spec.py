"""``vitrine``: the client command line, against a real server (spec § 9)."""

from __future__ import annotations

import io
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest

from mcp_vitrine.client.cli import main
from mcp_vitrine.domain.models import ArtifactDraft, ArtifactQuery, PublishedArtifact, Visibility
from mcp_vitrine.services.container import Services
from tests.http.conftest import CLIENT, FULL_TOKEN, READ_TOKEN


@dataclass(frozen=True, slots=True)
class Run:
    """One finished command: its exit status and what it printed."""

    status: int
    out: str
    err: str


type Vitrine = Callable[..., Run]


@pytest.fixture
def vitrine(
    base: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Vitrine:
    """Run ``vitrine <args>`` in-process against the server, as the full-scope client."""
    monkeypatch.setenv("VITRINE_URL", base)
    monkeypatch.setenv("VITRINE_TOKEN", FULL_TOKEN)

    def invoke(*args: str) -> Run:
        status = main(list(args))
        captured = capsys.readouterr()
        return Run(status=status, out=captured.out, err=captured.err)

    return invoke


@pytest.fixture
def site(tmp_path: Path) -> Path:
    """A built site with a nested asset, a hidden file and a symlink out of the folder."""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<h1>site</h1>")
    (root / "assets" / "app.js").write_text("1")
    (root / ".env").write_text("SECRET=1")
    (tmp_path / "outside.txt").write_text("private")
    (root / "linked.txt").symlink_to(tmp_path / "outside.txt")
    return root


@pytest.fixture
def artifact(services: Services) -> PublishedArtifact:
    """One private artifact of one page."""
    return services.artifacts.create(
        ArtifactDraft(title="Energy", files={"index.html": b"<h1>hi</h1>"}), client=CLIENT
    )


def paths_of(services: Services, artifact_id: str) -> list[str]:
    """The file paths of an artifact's current version."""
    return [file.path for file in services.artifacts.get(artifact_id).files]


def published_id(services: Services) -> str:
    """The id of the one artifact the store holds."""
    return services.artifacts.list(ArtifactQuery()).items[0].id


class DescribePublish:
    class WhenAFolderIsGiven:
        def it_keeps_relative_paths(self, vitrine: Vitrine, site: Path, services: Services) -> None:
            vitrine("publish", str(site))
            assert paths_of(services, published_id(services)) == ["assets/app.js", "index.html"]

        def it_skips_hidden_files_and_symlinks(
            self, vitrine: Vitrine, site: Path, services: Services
        ) -> None:
            vitrine("publish", str(site))
            paths = paths_of(services, published_id(services))
            assert ".env" not in paths
            assert "linked.txt" not in paths

        def it_prints_only_the_url_on_stdout(self, vitrine: Vitrine, site: Path) -> None:
            run = vitrine("publish", str(site))
            assert run.out.strip().splitlines() == [run.out.strip()]
            assert "/s/" in run.out

        def it_says_on_stderr_that_the_link_is_private(self, vitrine: Vitrine, site: Path) -> None:
            assert "private link" in vitrine("publish", str(site)).err

        def it_leaves_the_artifact_private(
            self, vitrine: Vitrine, site: Path, services: Services
        ) -> None:
            vitrine("publish", str(site))
            artifact = services.artifacts.get(published_id(services)).artifact
            assert artifact.visibility is Visibility.PRIVATE

        def it_opens_over_the_printed_url(self, vitrine: Vitrine, site: Path, base: str) -> None:
            url = vitrine("publish", str(site)).out.strip()
            assert httpx2.get(url).text == "<h1>site</h1>"

    class WhenZipIsAsked:
        def it_sends_one_archive_that_unpacks(
            self, vitrine: Vitrine, site: Path, services: Services
        ) -> None:
            vitrine("publish", str(site), "--zip", "--fallback", "index.html")
            detail = services.artifacts.get(published_id(services))
            assert ([file.path for file in detail.files], detail.artifact.fallback) == (
                ["assets/app.js", "index.html"],
                "index.html",
            )

    class WhenAVersionIsAdded:
        def it_keeps_the_id(
            self, vitrine: Vitrine, site: Path, artifact: PublishedArtifact, services: Services
        ) -> None:
            vitrine("publish", str(site / "index.html"), "--update", artifact.id)
            assert services.artifacts.get(artifact.id).version == 2

    class WhenTagsAndAProjectAreGiven:
        def it_files_the_artifact(self, vitrine: Vitrine, site: Path, services: Services) -> None:
            vitrine("publish", str(site), "--tag", "q3", "--tag", "energy", "--project", "power")
            artifact = services.artifacts.get(published_id(services)).artifact
            assert (artifact.tags, artifact.project) == (("q3", "energy"), "power")

    class WhenJsonIsAsked:
        def it_prints_the_api_response(self, vitrine: Vitrine, site: Path) -> None:
            assert '"version": 1' in vitrine("publish", str(site), "--json").out

    class WhenThePathIsMissing:
        def it_fails_with_exit_1(self, vitrine: Vitrine, tmp_path: Path) -> None:
            assert vitrine("publish", str(tmp_path / "nope")).status == 1

    class WhenTheUploadIsTooLarge:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            return {"VITRINE_MAX_ARTIFACT": "10B"}

        def it_suggests_zip_or_a_smaller_folder(self, vitrine: Vitrine, site: Path) -> None:
            assert "--zip" in vitrine("publish", str(site)).err


class DescribeTheToken:
    class WhenPassedAsAnArgument:
        def it_refuses_with_exit_2(self, vitrine: Vitrine) -> None:
            assert vitrine("list", "--token", FULL_TOKEN).status == 2

        def it_refuses_the_equals_form_too(self, vitrine: Vitrine) -> None:
            assert vitrine("list", f"--token={FULL_TOKEN}").status == 2

    class WhenItIsInAFile:
        def it_reads_it(
            self, vitrine: Vitrine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
        ) -> None:
            monkeypatch.delenv("VITRINE_TOKEN")
            token = tmp_path / "token"
            token.write_text(f"{READ_TOKEN}\n")
            assert vitrine("list", "--token-file", str(token)).status == 0

    class WhenItIsMissing:
        def it_fails_with_exit_2(self, vitrine: Vitrine, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.delenv("VITRINE_TOKEN")
            assert vitrine("list").status == 2

    class WhenItIsWrong:
        def it_fails_with_exit_1(self, vitrine: Vitrine, monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setenv("VITRINE_TOKEN", "guess")
            run = vitrine("list")
            assert (run.status, "401" in run.err) == (1, True)


class DescribeList:
    def it_prints_one_line_per_artifact(
        self, vitrine: Vitrine, artifact: PublishedArtifact
    ) -> None:
        assert vitrine("list").out.startswith(f"{artifact.id}\tprivate\tv1\tEnergy")

    class WhenAllIsAsked:
        def it_follows_the_cursors(self, vitrine: Vitrine, services: Services) -> None:
            for number in range(3):
                services.artifacts.create(
                    ArtifactDraft(title=f"a{number}", files={"a.txt": b"x"}), client=CLIENT
                )
            assert len(vitrine("list", "--all", "--json").out.split('"id"')) == 4


class DescribeGet:
    def it_shows_the_files_and_links(
        self, vitrine: Vitrine, artifact: PublishedArtifact, services: Services
    ) -> None:
        services.artifacts.link(artifact.id, client=CLIENT, label="finance")
        out = vitrine("get", artifact.id).out
        assert "index.html" in out
        assert "finance" in out

    class WhenTheIdIsUnknown:
        def it_fails_with_exit_1(self, vitrine: Vitrine) -> None:
            assert vitrine("get", "k7m2q9x4tz3wbn6r").status == 1


class DescribeLink:
    def it_prints_a_working_link(self, vitrine: Vitrine, artifact: PublishedArtifact) -> None:
        url = vitrine("link", artifact.id, "--label", "finance").out.strip()
        assert httpx2.get(url).text == "<h1>hi</h1>"

    class WhenPasscodeIsGivenWithoutAValue:
        def it_prints_the_generated_passcode_once(
            self, vitrine: Vitrine, artifact: PublishedArtifact
        ) -> None:
            run = vitrine("link", artifact.id, "--passcode")
            assert (run.err.count("passcode: "), "passcode" in run.out) == (1, False)

        def it_locks_the_link(self, vitrine: Vitrine, artifact: PublishedArtifact) -> None:
            url = vitrine("link", artifact.id, "--passcode").out.strip()
            assert httpx2.get(url).status_code == 401

    class WhenAPasscodeValueIsGiven:
        def it_does_not_print_it(self, vitrine: Vitrine, artifact: PublishedArtifact) -> None:
            run = vitrine("link", artifact.id, "--passcode", "correct horse")
            assert "correct horse" not in run.out + run.err


class DescribeRevoke:
    class WhenOneLinkIsNamed:
        def it_keeps_the_others(
            self, vitrine: Vitrine, artifact: PublishedArtifact, services: Services
        ) -> None:
            first = services.artifacts.link(artifact.id, client=CLIENT)
            second = services.artifacts.link(artifact.id, client=CLIENT)
            vitrine("revoke", artifact.id, "--link", str(first.lid))
            assert (httpx2.get(first.url).status_code, httpx2.get(second.url).status_code) == (
                404,
                200,
            )


class DescribeTag:
    def it_edits_without_a_version(
        self, vitrine: Vitrine, artifact: PublishedArtifact, services: Services
    ) -> None:
        vitrine("tag", artifact.id, "--add", "q3", "--project", "power")
        detail = services.artifacts.get(artifact.id)
        assert (detail.artifact.tags, detail.artifact.project, detail.version) == (
            ("q3",),
            "power",
            1,
        )

    class WhenNothingIsNamed:
        def it_fails_with_exit_2(self, vitrine: Vitrine, artifact: PublishedArtifact) -> None:
            assert vitrine("tag", artifact.id).status == 2


class DescribeDelete:
    class WhenStdinIsNotATerminalAndYesIsMissing:
        def it_refuses(
            self,
            vitrine: Vitrine,
            artifact: PublishedArtifact,
            services: Services,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            monkeypatch.setattr("sys.stdin", io.StringIO(""))
            run = vitrine("delete", artifact.id)
            assert (run.status, services.artifacts.get(artifact.id).version) == (2, 1)

    class WhenYesIsGiven:
        def it_deletes(
            self, vitrine: Vitrine, artifact: PublishedArtifact, services: Services
        ) -> None:
            vitrine("delete", artifact.id, "--yes")
            assert vitrine("get", artifact.id).status == 1

    class WhenTheTerminalAnswersNo:
        def it_keeps_the_artifact(
            self,
            vitrine: Vitrine,
            artifact: PublishedArtifact,
            services: Services,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            terminal = io.StringIO("n\n")
            terminal.isatty = lambda: True  # type: ignore[method-assign]
            monkeypatch.setattr("sys.stdin", terminal)
            vitrine("delete", artifact.id)
            assert services.artifacts.get(artifact.id).version == 1


class DescribeOpen:
    def it_opens_a_fresh_link_in_the_browser(
        self, vitrine: Vitrine, artifact: PublishedArtifact, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[str] = []

        def browse(url: str) -> bool:
            opened.append(url)
            return True

        monkeypatch.setattr("webbrowser.open", browse)
        run = vitrine("open", artifact.id)
        assert opened == [run.out.strip()]


class DescribeTheClientPackage:
    def it_imports_nothing_from_the_server_layers(self) -> None:
        root = Path(__file__).parents[2]
        contracts = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["importlinter"][
            "contracts"
        ]
        client = next(c for c in contracts if c.get("source_modules") == ["mcp_vitrine.client"])
        assert {
            "mcp_vitrine.services",
            "mcp_vitrine.storage",
            "mcp_vitrine.web",
            "mcp_vitrine.mcp",
        } <= set(client["forbidden_modules"])
