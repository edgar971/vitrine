"""The one path rule, and the entry rule that goes with it (spec § 4.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_drop.domain.errors import Invalid
from mcp_drop.domain.paths import INDEX_ENTRY, choose_entry, resolve_path, validate_path

UNSAFE_PATHS = [
    "",
    "/etc/passwd",
    "../escape",
    "a/../../escape",
    "..",
    ".",
    "a/./b",
    "a//b",
    ".hidden",
    "a/.hidden/file",
    "a\\b",
    "a\x00b",
]


class DescribeThePathRule:
    class WhenThePathIsSafe:
        def it_accepts_a_plain_file(self) -> None:
            assert validate_path("index.html") == "index.html"

        def it_accepts_nested_directories(self) -> None:
            assert validate_path("assets/css/site.css") == "assets/css/site.css"

    class WhenThePathIsUnsafe:
        @pytest.mark.parametrize("path", UNSAFE_PATHS)
        def it_refuses_it(self, path: str) -> None:
            with pytest.raises(Invalid):
                validate_path(path)

    class WhenThePathIsLongerThanTheLimit:
        def it_refuses_it(self) -> None:
            with pytest.raises(Invalid):
                validate_path("a" * 256)


class DescribePathResolution:
    class WhenTheFileIsInsideTheVersion:
        def it_resolves_to_it(self, tmp_path: Path) -> None:
            (tmp_path / "assets").mkdir()
            (tmp_path / "assets" / "app.js").write_text("x")
            assert resolve_path(tmp_path, "assets/app.js") == tmp_path / "assets" / "app.js"

    class WhenThePathIsUnsafe:
        def it_refuses_it_before_touching_the_filesystem(self, tmp_path: Path) -> None:
            with pytest.raises(Invalid):
                resolve_path(tmp_path, "../outside.txt")

    class WhenAFileIsASymlink:
        def it_refuses_it(self, tmp_path: Path) -> None:
            (tmp_path / "link.txt").symlink_to("/etc/hosts")
            with pytest.raises(Invalid):
                resolve_path(tmp_path, "link.txt")

    class WhenADirectoryInThePathIsASymlink:
        def it_refuses_to_follow_it_out_of_the_version(self, tmp_path: Path) -> None:
            outside = tmp_path / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("secret")
            version = tmp_path / "v1"
            version.mkdir()
            (version / "escape").symlink_to(outside, target_is_directory=True)
            with pytest.raises(Invalid):
                resolve_path(version, "escape/secret.txt")


class DescribeEntryChoice:
    class WhenTheCallerNamesAnEntry:
        def it_uses_it(self) -> None:
            assert choose_entry(files=("a.html", "b.html"), requested="b.html") == "b.html"

    class WhenTheNamedEntryIsNotInTheVersion:
        def it_refuses_the_version(self) -> None:
            with pytest.raises(Invalid):
                choose_entry(files=("a.html",), requested="missing.html")

    class WhenTheNamedEntryIsUnsafe:
        def it_refuses_the_version(self) -> None:
            with pytest.raises(Invalid):
                choose_entry(files=("../a.html",), requested="../a.html")

    class WhenTheCallerNamesNothing:
        def it_keeps_the_previous_entry(self) -> None:
            assert choose_entry(files=("report.html",), previous="report.html") == "report.html"

        def it_prefers_index_html(self) -> None:
            assert choose_entry(files=("a.html", INDEX_ENTRY), previous="gone.html") == INDEX_ENTRY

        def it_falls_back_to_the_only_file(self) -> None:
            assert choose_entry(files=("only.txt",)) == "only.txt"

        def it_refuses_a_version_it_cannot_choose_for(self) -> None:
            with pytest.raises(Invalid):
                choose_entry(files=("a.html", "b.html"))
