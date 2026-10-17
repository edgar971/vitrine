"""Zip archives unpacked into a version: every control of spec § 7.3 (security T6, T9)."""

from __future__ import annotations

import io
import random
import stat
import struct
import zipfile
from pathlib import Path

import pytest

from mcp_vitrine.domain.errors import Invalid, TooLarge
from mcp_vitrine.domain.models import Spooled
from mcp_vitrine.domain.paths import validate_path
from mcp_vitrine.services.archives import ArchiveLimits, unpack, warnings

LIMITS = ArchiveLimits(max_files=20, max_bytes=4 * 1024 * 1024, max_ratio=200)
FUZZ_SEGMENTS = ("a", "b.html", "..", ".", "", "/", "\\", "__MACOSX", ".git", "c d", "é")


def archive(path: Path, entries: dict[str, bytes], *, method: int = zipfile.ZIP_DEFLATED) -> Path:
    """A zip of ``entries``, written as the zip tool writes one."""
    with zipfile.ZipFile(path, "w", compression=method) as writer:
        for name, data in entries.items():
            writer.writestr(name, data)
    return path


def raw_entry(path: Path, name: str, data: bytes, *, attributes: int = 0, flags: int = 0) -> Path:
    """A one-entry zip whose name and header bits are set by hand, as an attacker would."""
    info = zipfile.ZipInfo(name)
    info.external_attr = attributes
    with zipfile.ZipFile(path, "w") as writer:
        writer.writestr(info, data)
    if flags:
        # zipfile writes its own flag bits, so the general-purpose flags are patched in after.
        raw = bytearray(path.read_bytes())
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            at = raw.index(signature) + offset
            raw[at : at + 2] = struct.pack("<H", struct.unpack("<H", raw[at : at + 2])[0] | flags)
        path.write_bytes(bytes(raw))
    return path


def unpacked_paths(source: Path, into: Path) -> list[str]:
    """The version paths an archive unpacks to."""
    return sorted(unpack(source, into, LIMITS).files)


@pytest.fixture
def into(tmp_path: Path) -> Path:
    """Where the inflated files land."""
    return tmp_path / "out"


class DescribeUnpackingAnArchive:
    class WhenTheArchiveIsASite:
        def it_keeps_relative_paths(self, tmp_path: Path, into: Path) -> None:
            source = archive(tmp_path / "a.zip", {"index.html": b"<h1>", "assets/app.js": b"1"})
            assert unpacked_paths(source, into) == ["assets/app.js", "index.html"]

        def it_inflates_the_bytes(self, tmp_path: Path, into: Path) -> None:
            source = archive(tmp_path / "a.zip", {"index.html": b"<h1>hi</h1>"})
            content = unpack(source, into, LIMITS).files["index.html"]
            assert isinstance(content, Spooled)
            assert content.path.read_bytes() == b"<h1>hi</h1>"

    def it_strips_the_shared_top_level_folder(self, tmp_path: Path, into: Path) -> None:
        source = archive(tmp_path / "a.zip", {"dist/index.html": b"x", "dist/a/b.js": b"y"})
        assert unpacked_paths(source, into) == ["a/b.js", "index.html"]

    def it_keeps_a_folder_that_not_every_entry_shares(self, tmp_path: Path, into: Path) -> None:
        source = archive(tmp_path / "a.zip", {"dist/a.js": b"x", "index.html": b"y"})
        assert unpacked_paths(source, into) == ["dist/a.js", "index.html"]

    def it_skips_macosx_junk_and_counts_it(self, tmp_path: Path, into: Path) -> None:
        entries = {"index.html": b"x", "__MACOSX/._index.html": b"j", ".DS_Store": b"j"}
        result = unpack(archive(tmp_path / "a.zip", entries), into, LIMITS)
        assert (sorted(result.files), result.skipped) == (["index.html"], 2)

    def it_stores_a_nested_archive_as_a_file(self, tmp_path: Path, into: Path) -> None:
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as writer:
            writer.writestr("deep.html", b"x")
        source = archive(tmp_path / "a.zip", {"index.html": b"x", "data.zip": inner.getvalue()})
        assert unpacked_paths(source, into) == ["data.zip", "index.html"]

    def it_accepts_a_stored_entry(self, tmp_path: Path, into: Path) -> None:
        source = archive(tmp_path / "a.zip", {"index.html": b"x"}, method=zipfile.ZIP_STORED)
        assert unpacked_paths(source, into) == ["index.html"]

    def it_refuses_a_dot_dot_entry(self, tmp_path: Path, into: Path) -> None:
        with pytest.raises(Invalid):
            unpack(raw_entry(tmp_path / "a.zip", "../evil.html", b"x"), into, LIMITS)

    def it_refuses_an_absolute_entry(self, tmp_path: Path, into: Path) -> None:
        with pytest.raises(Invalid):
            unpack(raw_entry(tmp_path / "a.zip", "/etc/evil.html", b"x"), into, LIMITS)

    def it_refuses_a_backslash_entry(self, tmp_path: Path, into: Path) -> None:
        with pytest.raises(Invalid):
            unpack(raw_entry(tmp_path / "a.zip", "..\\evil.html", b"x"), into, LIMITS)

    def it_refuses_a_symlink_entry(self, tmp_path: Path, into: Path) -> None:
        link = (stat.S_IFLNK | 0o777) << 16
        source = raw_entry(tmp_path / "a.zip", "index.html", b"/etc/passwd", attributes=link)
        with pytest.raises(Invalid):
            unpack(source, into, LIMITS)

    def it_refuses_an_encrypted_entry(self, tmp_path: Path, into: Path) -> None:
        source = raw_entry(tmp_path / "a.zip", "index.html", b"x", flags=0x1)
        with pytest.raises(Invalid):
            unpack(source, into, LIMITS)

    def it_refuses_a_method_other_than_stored_or_deflate(self, tmp_path: Path, into: Path) -> None:
        source = archive(tmp_path / "a.zip", {"index.html": b"x"}, method=zipfile.ZIP_BZIP2)
        with pytest.raises(Invalid):
            unpack(source, into, LIMITS)

    def it_refuses_a_name_that_is_neither_utf8_nor_ascii(self, tmp_path: Path, into: Path) -> None:
        source = raw_entry(tmp_path / "a.zip", "index.html", b"x")
        data = bytearray(source.read_bytes())
        # Rewrite the name bytes (local header and central directory) to a cp437 é, flag unset.
        data = bytearray(data.replace(b"index.html", b"ind\x82x.html"))
        source.write_bytes(bytes(data))
        with pytest.raises(Invalid):
            unpack(source, into, LIMITS)

    def it_refuses_duplicate_paths(self, tmp_path: Path, into: Path) -> None:
        source = tmp_path / "a.zip"
        with zipfile.ZipFile(source, "w") as writer:
            writer.writestr("index.html", b"one")
            with pytest.warns(UserWarning, match="Duplicate name"):
                writer.writestr("index.html", b"two")
        with pytest.raises(Invalid):
            unpack(source, into, LIMITS)

    def it_refuses_more_entries_than_max_files(self, tmp_path: Path, into: Path) -> None:
        entries = {f"f{n}.html": b"x" for n in range(LIMITS.max_files + 1)}
        with pytest.raises(TooLarge):
            unpack(archive(tmp_path / "a.zip", entries), into, LIMITS)

    def it_refuses_an_archive_that_inflates_past_max_artifact(
        self, tmp_path: Path, into: Path
    ) -> None:
        third = LIMITS.max_bytes // 3 + 1
        noise = random.Random(0)  # noqa: S311 - incompressible test data, not a secret
        entries = {f"{n}.bin": noise.randbytes(third) for n in range(3)}
        with pytest.raises(TooLarge):
            unpack(archive(tmp_path / "a.zip", entries), into, LIMITS)

    def it_refuses_an_entry_past_the_ratio(self, tmp_path: Path, into: Path) -> None:
        bomb = archive(tmp_path / "a.zip", {"index.html": b"\0" * (3 * 1024 * 1024)})
        with pytest.raises(TooLarge):
            unpack(bomb, into, LIMITS)

    def it_refuses_a_lying_size_header(self, tmp_path: Path, into: Path) -> None:
        source = archive(tmp_path / "a.zip", {"index.html": b"<h1>" * 4096})
        data = bytearray(source.read_bytes())
        # The central directory's uncompressed size (offset 24 of its header) says 1 byte.
        central = data.rindex(b"PK\x01\x02")
        data[central + 24 : central + 28] = struct.pack("<I", 1)
        source.write_bytes(bytes(data))
        with pytest.raises(Invalid):
            unpack(source, into, LIMITS)

    def it_refuses_a_body_that_is_not_a_zip(self, tmp_path: Path, into: Path) -> None:
        source = tmp_path / "a.zip"
        source.write_bytes(b"not a zip")
        with pytest.raises(Invalid):
            unpack(source, into, LIMITS)

    def it_refuses_an_archive_of_only_junk(self, tmp_path: Path, into: Path) -> None:
        with pytest.raises(Invalid):
            unpack(archive(tmp_path / "a.zip", {".DS_Store": b"x"}), into, LIMITS)

    def it_writes_nothing_when_one_entry_fails(self, tmp_path: Path, into: Path) -> None:
        source = tmp_path / "a.zip"
        with zipfile.ZipFile(source, "w") as writer:
            writer.writestr("index.html", b"x")
            writer.writestr(zipfile.ZipInfo("../evil.html"), b"x")
        with pytest.raises(Invalid):
            unpack(source, into, LIMITS)
        assert not into.exists()

    @pytest.mark.parametrize("seed", range(200))
    def it_never_lets_a_path_escape_the_version_root(
        self, tmp_path: Path, into: Path, seed: int
    ) -> None:
        chance = random.Random(seed)  # noqa: S311 - a reproducible fuzz
        names = {
            "/".join(chance.choice(FUZZ_SEGMENTS) for _ in range(chance.randint(1, 4)))
            for _ in range(chance.randint(1, 5))
        }
        source = tmp_path / "a.zip"
        with zipfile.ZipFile(source, "w") as writer:
            for name in names:
                writer.writestr(zipfile.ZipInfo(name), b"x")
        try:
            paths = sorted(unpack(source, into, LIMITS).files)
        except Invalid, TooLarge:
            return
        assert all(validate_path(path) == path for path in paths)


class DescribeTheSiteWarnings:
    class WhenTheEntryLoadsRootAbsoluteAssets:
        def it_warns(self) -> None:
            files = {"index.html": b'<script src="/assets/app.js"></script>'}
            assert warnings(files, entry=None)

    class WhenTheAssetsAreRelative:
        def it_says_nothing(self) -> None:
            files = {"index.html": b'<script src="./assets/app.js"></script><a href="//cdn">'}
            assert warnings(files, entry=None) == ()
