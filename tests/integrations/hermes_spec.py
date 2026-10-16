"""The Hermes plugin (design § 8): ``register()``, and a real server driven through its upload."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

import drop
from drop.client import EARLY_CLOSE, DropError, Endpoint, collect
from mcp_drop.domain.models import Visibility
from mcp_drop.services.container import Services
from tests.integrations.conftest import Recorder

PLUGIN = Path(drop.__file__).parent
# The oldest Python Hermes runs on (its requires-python); the plugin runs on Hermes's interpreter.
HERMES_PYTHON = (3, 11)


def published(**args: Any) -> dict[str, Any]:  # noqa: ANN401
    """Call the tool as Hermes's registry does, and read back its JSON result."""
    result: dict[str, Any] = json.loads(drop.publish(args, task_id="t1"))
    return result


@pytest.fixture
def registered() -> Recorder:
    """What ``register()`` handed Hermes."""
    recorder = Recorder()
    drop.register(recorder)
    return recorder


class DescribeThePluginSource:
    @pytest.mark.parametrize("module", ["__init__.py", "client.py"])
    def it_parses_on_the_oldest_python_hermes_runs_on(self, module: str) -> None:
        source = (PLUGIN / module).read_text()
        ast.parse(source, feature_version=HERMES_PYTHON)


class DescribeRegistering:
    def it_registers_the_tool_the_manifest_declares(self, registered: Recorder) -> None:
        # `hermes plugins validate` compares the two in CI; this is the same check, sooner.
        manifest = (PLUGIN / "plugin.yaml").read_text()
        declared = "".join(f"  - {name}\n" for name in registered.tools)
        assert manifest.endswith(f"provides_tools:\n{declared}")

    def it_puts_the_tool_in_the_drop_toolset(self, registered: Recorder) -> None:
        assert registered.tools[drop.TOOL]["toolset"] == "drop"

    def it_offers_no_way_to_ask_for_visibility(self, registered: Recorder) -> None:
        properties = registered.tools[drop.TOOL]["schema"]["parameters"]["properties"]
        assert not {"visibility", "public"} & set(properties)

    def it_refuses_parameters_the_schema_does_not_name(self, registered: Recorder) -> None:
        assert registered.tools[drop.TOOL]["schema"]["parameters"]["additionalProperties"] is False

    def it_registers_the_skill_from_its_file(self, registered: Recorder) -> None:
        assert registered.skills["drop"].is_file()

    def it_registers_the_share_and_artifacts_commands(self, registered: Recorder) -> None:
        assert sorted(registered.commands) == ["artifacts", "share"]

    class WhenTheServerIsNotConfigured:
        def it_does_not_offer_the_tool(
            self, registered: Recorder, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            monkeypatch.delenv("DROP_URL", raising=False)
            monkeypatch.delenv("DROP_TOKEN", raising=False)
            assert registered.tools[drop.TOOL]["check_fn"]() is False


class DescribePublishingFiles:
    class WhenAFolderIsPublished:
        @pytest.fixture
        def result(self, connected: str, site: Path) -> dict[str, Any]:
            return published(paths=["site"], title="Energy")

        def it_succeeds(self, result: dict[str, Any]) -> None:
            assert result["success"] is True

        def it_creates_version_one(self, result: dict[str, Any]) -> None:
            assert result["version"] == 1

        def it_stores_the_folder_contents_at_their_relative_paths(
            self, result: dict[str, Any], services: Services
        ) -> None:
            files = services.artifacts.get(result["id"]).files
            assert sorted(f.path for f in files) == ["img/chart.png", "index.html"]

        def it_uploads_binaries_byte_for_byte(
            self, result: dict[str, Any], services: Services, site: Path
        ) -> None:
            artifact = services.artifacts.get(result["id"]).artifact
            stored = services.artifacts.locate(artifact, "img/chart.png").read_bytes()
            assert stored == (site / "img" / "chart.png").read_bytes()

        def it_leaves_the_artifact_private(
            self, result: dict[str, Any], services: Services
        ) -> None:
            artifact = services.artifacts.get(result["id"]).artifact
            assert artifact.visibility is Visibility.PRIVATE

        def it_returns_a_link_that_opens(self, result: dict[str, Any]) -> None:
            assert httpx2.get(result["url"], follow_redirects=True).status_code == 200

        def it_records_the_token_s_client_as_the_source(
            self, result: dict[str, Any], services: Services
        ) -> None:
            assert services.artifacts.get(result["id"]).artifact.source == "hermes"

    class WhenNoTitleIsGiven:
        def it_titles_the_artifact_after_the_first_file(
            self, connected: str, site: Path, services: Services
        ) -> None:
            result = published(paths=["site/index.html"])
            assert services.artifacts.get(result["id"]).artifact.title == "index.html"

    class WhenAnIdIsGiven:
        @pytest.fixture
        def first(self, connected: str, site: Path) -> dict[str, Any]:
            return published(paths=["site"], title="Energy")

        def it_adds_a_version_behind_the_same_id(self, first: dict[str, Any]) -> None:
            result = published(paths=["site/index.html"], id=first["id"])
            assert (result["id"], result["version"]) == (first["id"], 2)

        def it_replaces_the_files_by_default(
            self, first: dict[str, Any], services: Services
        ) -> None:
            published(paths=["site/index.html"], id=first["id"])
            files = services.artifacts.get(first["id"]).files
            assert [f.path for f in files] == ["index.html"]

        def it_overlays_the_files_when_merging(
            self, first: dict[str, Any], services: Services, tmp_path: Path
        ) -> None:
            (tmp_path / "notes.md").write_text("# Notes")
            published(paths=[str(tmp_path / "notes.md")], id=first["id"], merge=True)
            files = services.artifacts.get(first["id"]).files
            assert sorted(f.path for f in files) == ["img/chart.png", "index.html", "notes.md"]

        def it_sends_tags_and_a_note(self, first: dict[str, Any], services: Services) -> None:
            published(paths=["site"], id=first["id"], tags=["energy", "q3"], note="add a chart")
            detail = services.artifacts.get(first["id"])
            assert (detail.artifact.tags, detail.artifact.versions[-1].note) == (
                ("energy", "q3"),
                "add a chart",
            )

    class WhenTheIdIsAPath:
        def it_cannot_leave_the_artifact_route(self, connected: str, site: Path) -> None:
            result = published(paths=["site"], id="../../healthz")
            assert result["success"] is False

    class WhenTheTokenIsWrong:
        @pytest.fixture
        def result(
            self, connected: str, site: Path, monkeypatch: pytest.MonkeyPatch
        ) -> dict[str, Any]:
            monkeypatch.setenv("DROP_TOKEN", "a-guess")
            return published(paths=["site/index.html"])

        def it_reports_the_server_s_refusal(self, result: dict[str, Any]) -> None:
            assert result == {
                "success": False,
                "error": "drop refused the request (401): unauthorized",
            }

    class WhenTheServerHangsUpBeforeReadingTheUpload:
        def it_says_what_drop_refuses_unread(
            self, site: Path, monkeypatch: pytest.MonkeyPatch, hang_up: str
        ) -> None:
            monkeypatch.setenv("DROP_URL", hang_up)
            monkeypatch.setenv("DROP_TOKEN", "t")
            (site / "big.bin").write_bytes(b"x" * 8_000_000)
            assert published(paths=["site/big.bin"])["error"] == EARLY_CLOSE

    class WhenTheServerRefusesTheBody:
        def it_passes_the_server_s_reason_on(self, connected: str, site: Path) -> None:
            result = published(paths=["site"], expires_in="soon")
            assert result["error"].startswith("drop refused the request (400): ")

    class WhenTheServerIsUnreachable:
        def it_says_where_it_tried(
            self, site: Path, monkeypatch: pytest.MonkeyPatch, port: int
        ) -> None:
            monkeypatch.setenv("DROP_URL", f"http://127.0.0.1:{port}")
            monkeypatch.setenv("DROP_TOKEN", "t")
            result = published(paths=["site"])
            assert result["error"].startswith(f"cannot reach drop at http://127.0.0.1:{port}")

    class WhenAPathDoesNotExist:
        def it_names_the_path(self, connected: str, site: Path) -> None:
            assert published(paths=["missing.pdf"])["error"] == (
                "missing.pdf: no such file or folder"
            )


class DescribeCollectingFiles:
    def it_keeps_a_file_s_name(self, site: Path) -> None:
        assert collect(["site/index.html"], site.parent) == [("index.html", site / "index.html")]

    def it_resolves_relative_paths_against_the_agent_s_directory(self, site: Path) -> None:
        names = [name for name, _ in collect(["index.html"], site)]
        assert names == ["index.html"]

    def it_skips_hidden_files_in_a_folder(self, site: Path) -> None:
        assert ".env" not in [name for name, _ in collect(["site"], site.parent)]

    def it_skips_symlinks_in_a_folder(self, site: Path) -> None:
        assert "linked.txt" not in [name for name, _ in collect(["site"], site.parent)]

    def it_refuses_two_files_with_one_name(self, site: Path) -> None:
        (site / "img" / "index.html").write_text("again")
        with pytest.raises(DropError, match=r"both be stored as index\.html"):
            collect(["site/index.html", "site/img/index.html"], site.parent)

    def it_refuses_a_name_with_a_quote(self, site: Path) -> None:
        (site / 'say "hi".txt').write_text("hi")
        with pytest.raises(DropError, match="quote"):
            collect([str(site / 'say "hi".txt')], site.parent)

    def it_refuses_a_folder_with_nothing_to_upload(self, tmp_path: Path) -> None:
        (tmp_path / "empty").mkdir()
        with pytest.raises(DropError, match="nothing to upload"):
            collect(["empty"], tmp_path)

    def it_refuses_more_files_than_the_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("drop.client.MAX_FILES", 2)
        for name in ("a", "b", "c"):
            (tmp_path / name).write_text(name)
        with pytest.raises(DropError, match="more than 2 files"):
            collect([str(tmp_path)], tmp_path)


class DescribeTheEndpoint:
    def it_refuses_a_url_that_is_not_http(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DROP_URL", "file:///etc/passwd")
        monkeypatch.setenv("DROP_TOKEN", "t")
        with pytest.raises(DropError, match="http or https"):
            Endpoint.from_env()

    def it_says_which_variables_are_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DROP_URL", raising=False)
        with pytest.raises(DropError, match="DROP_URL and DROP_TOKEN"):
            Endpoint.from_env()


class DescribeTheShareCommand:
    def it_answers_with_a_private_link(
        self, registered: Recorder, connected: str, site: Path
    ) -> None:
        assert registered.commands["share"]("site").startswith(f"{connected}/s/")

    def it_accepts_a_quoted_path_with_spaces(
        self, registered: Recorder, connected: str, site: Path
    ) -> None:
        (site / "Q3 report.html").write_text("<h1>Q3</h1>")
        assert "(private, version 1" in registered.commands["share"]("'site/Q3 report.html'")

    def it_explains_itself_without_arguments(self, registered: Recorder) -> None:
        assert registered.commands["share"]("").startswith("usage: /share")

    def it_reports_a_refusal_as_its_answer(
        self, registered: Recorder, connected: str, site: Path
    ) -> None:
        assert registered.commands["share"]("nope") == "nope: no such file or folder"


class DescribeTheArtifactsCommand:
    def it_lists_what_was_published_without_links(
        self, registered: Recorder, connected: str, site: Path
    ) -> None:
        result = published(paths=["site"], title="Energy")
        assert registered.commands["artifacts"]("") == f"{result['id']}  private  v1  Energy"

    def it_filters_by_the_query(self, registered: Recorder, connected: str, site: Path) -> None:
        published(paths=["site"], title="Energy")
        assert registered.commands["artifacts"]("water") == "no artifacts"

    def it_reports_a_refusal_as_its_answer(
        self, registered: Recorder, connected: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DROP_TOKEN", "a-guess")
        assert registered.commands["artifacts"]("") == (
            "drop refused the request (401): unauthorized"
        )
