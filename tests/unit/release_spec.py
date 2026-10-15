"""``scripts/release.py``: one command moves every copy of the version (CONTRIBUTING.md)."""

from __future__ import annotations

import datetime as dt
import tomllib
from pathlib import Path

import pytest
import release

ROOT = Path(__file__).parents[2]
TODAY = dt.date(2026, 10, 16)
CHANGELOG = """# Changelog

Intro.

## [Unreleased]

### Added

- The first thing.
"""
ROLLED = """# Changelog

Intro.

## [Unreleased]

## [0.1.0] - 2026-10-16

### Added

- The first thing.
"""
LATER = """# Changelog

## [Unreleased]

## [0.1.0] - 2026-10-16

- Old.
"""


def _version() -> str:
    """The version pyproject.toml declares."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version: str = project["project"]["version"]
    return version


class DescribeTheVersionCopies:
    @pytest.mark.parametrize("path", release.IMAGE_COPIES)
    def it_tags_each_example_image_with_the_version(self, path: Path) -> None:
        assert f"{release.IMAGE}{_version()}" in (ROOT / path).read_text(encoding="utf-8")

    @pytest.mark.parametrize("path", release.PLUGIN_COPIES)
    def it_gives_the_plugin_the_base_version(self, path: Path) -> None:
        base = release.parse(_version())["base"]
        assert f"\nversion: {base}\n" in (ROOT / path).read_text(encoding="utf-8")


class DescribeAVersion:
    @pytest.mark.parametrize("version", ["0.1.0", "0.2.0rc1", "1.0.0a2", "1.0.0b3"])
    def it_accepts_a_final_or_pre_release(self, version: str) -> None:
        assert release.parse(version)

    @pytest.mark.parametrize("version", ["v0.1.0", "0.1", "0.1.0-rc.1", "0.1.0.dev1", "0.1.0rc"])
    def it_refuses_anything_a_tag_may_not_name(self, version: str) -> None:
        with pytest.raises(release.ReleaseError):
            release.parse(version)


class DescribeBumping:
    def it_sets_the_project_version(self) -> None:
        text = '[project]\nname = "mcp-drop"\nversion = "0.1.0"\n'
        assert 'version = "0.2.0rc1"' in release.bump_pyproject(text, "0.2.0rc1")

    def it_retags_the_image(self) -> None:
        text = "    image: ghcr.io/edgar971/drop:0.1.0\n"
        assert release.bump_image(text, "0.2.0rc1") == "    image: ghcr.io/edgar971/drop:0.2.0rc1\n"

    def it_gives_the_plugin_the_base_version_of_a_pre_release(self) -> None:
        assert release.bump_plugin("name: drop\nversion: 0.1.0\n", "0.2.0rc1").endswith(
            "version: 0.2.0\n"
        )

    def it_refuses_a_file_without_the_line(self) -> None:
        with pytest.raises(release.ReleaseError):
            release.bump_pyproject("[project]\n", "0.2.0")


class DescribeRollingTheChangelog:
    class WhenItIsTheFirstRelease:
        def it_dates_the_hand_written_notes_under_the_version(self) -> None:
            assert release.roll_changelog(CHANGELOG, "0.1.0", section=None, today=TODAY) == ROLLED

    class WhenTheNotesComeFromTheCommits:
        def it_puts_the_rendered_section_under_an_empty_unreleased(self) -> None:
            section = "## [0.2.0] - 2026-11-01\n\n### Fixed\n\n- A fix.\n"
            rolled = release.roll_changelog(LATER, "0.2.0", section=section, today=TODAY)
            assert rolled == (
                "# Changelog\n\n## [Unreleased]\n\n## [0.2.0] - 2026-11-01\n\n### Fixed\n\n"
                "- A fix.\n\n## [0.1.0] - 2026-10-16\n\n- Old.\n"
            )

        def it_refuses_hand_written_unreleased_notes(self) -> None:
            with pytest.raises(release.ReleaseError, match="hand-written"):
                release.roll_changelog(CHANGELOG, "0.2.0", section="## [0.2.0]\n", today=TODAY)

    class WhenTheVersionIsAlreadyThere:
        def it_refuses(self) -> None:
            with pytest.raises(release.ReleaseError, match="already"):
                release.roll_changelog(ROLLED, "0.1.0", section=None, today=TODAY)

    class WhenNothingIsUnreleased:
        def it_refuses(self) -> None:
            with pytest.raises(release.ReleaseError, match="empty"):
                release.roll_changelog(LATER, "0.2.0", section=None, today=TODAY)
