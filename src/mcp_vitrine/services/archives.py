"""Zip archives unpacked into a version, under the write path's rules (spec § 7.3, ADR 0007).

An archive is new parsing surface that an agent controls, so every rule below fails the whole
upload before anything reaches the store: nothing an archive declares about itself is trusted,
sizes are counted while inflating, and each name passes the same path rule as a multipart part.
"""

from __future__ import annotations

import re
import stat
import uuid
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from mcp_vitrine.domain.errors import Invalid, TooLarge
from mcp_vitrine.domain.models import Content, Spooled
from mcp_vitrine.domain.paths import FORBIDDEN_CHARACTERS, choose_entry, validate_path

ZIP_TYPE = "application/zip"
UNPACK_ZIP = "zip"
CHUNK = 64 * 1024
# A ratio only means something once an entry is large enough to hurt.
RATIO_FLOOR = 1024 * 1024
JUNK_FOLDER = "__MACOSX"
UTF8_FLAG = 0x800
ENCRYPTED_FLAG = 0x1
METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
WARN_BYTES = 2 * 1024 * 1024
_ROOT_ABSOLUTE = re.compile(rb"""(?:src|href)\s*=\s*["']?/(?!/)""", re.IGNORECASE)
ROOT_ABSOLUTE_WARNING = (
    '{entry} loads assets from root-absolute URLs (src="/…" or href="/…"), which resolve '
    "outside the artifact: build with a relative base (vite build --base=./, "
    '"homepage": "." for CRA, assetPrefix: "./" for static Next)'
)


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    """The bounds an archive must stay inside: the same ones a multipart version has."""

    max_files: int
    max_bytes: int
    max_ratio: int


@dataclass(frozen=True, slots=True)
class Unpacked:
    """An archive's files, already inflated to disk, and how many junk entries were skipped."""

    files: dict[str, Content]
    skipped: int


def unpack(archive: Path, into: Path, limits: ArchiveLimits) -> Unpacked:
    """Check every entry, then inflate them one by one into ``into``; any refusal fails all.

    The checks that need only the central directory run first, so a refused archive writes
    nothing at all; the sizes are then enforced while inflating, where they cannot lie.
    """
    try:
        with zipfile.ZipFile(archive) as reader:
            members, skipped = _members(reader.infolist(), limits)
            into.mkdir(parents=True, exist_ok=True)
            files: dict[str, Content] = {}
            total = 0
            for path, info in members:
                target = into / uuid.uuid4().hex
                total += _inflate(reader, info, target, limits, already=total)
                files[path] = Spooled(target)
    except zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, zlib.error:
        raise Invalid("the body is not a readable zip archive") from None
    return Unpacked(files=files, skipped=skipped)


def warnings(files: Mapping[str, Content], *, entry: str | None) -> tuple[str, ...]:
    """What the caller should know about a site that unpacked: root-absolute asset URLs."""
    try:
        chosen = choose_entry(files=sorted(files), requested=entry)
    except Invalid:
        return ()
    content = files[chosen]
    if PurePosixPath(chosen).suffix.lower() not in {".html", ".htm"}:
        return ()
    if isinstance(content, Spooled):
        with content.path.open("rb") as file:
            head = file.read(WARN_BYTES)
    else:
        head = content[:WARN_BYTES]
    return (ROOT_ABSOLUTE_WARNING.format(entry=chosen),) if _ROOT_ABSOLUTE.search(head) else ()


def _members(
    infos: list[zipfile.ZipInfo], limits: ArchiveLimits
) -> tuple[list[tuple[str, zipfile.ZipInfo]], int]:
    """The entries to unpack, by their path in the version, and the count of skipped junk."""
    entries = [info for info in infos if not info.is_dir()]
    if len(entries) > limits.max_files:
        raise TooLarge(f"a version holds at most {limits.max_files} files")
    for info in entries:
        _refuse_entry(info)
    names = [info.filename for info in entries]
    prefix = _shared_folder(names)
    members: list[tuple[str, zipfile.ZipInfo]] = []
    seen: set[str] = set()
    skipped = 0
    for info in entries:
        path = info.filename.removeprefix(prefix)
        if _is_junk(path):
            skipped += 1
            continue
        validate_path(path)
        if path in seen:
            raise Invalid(f"{path} is in the archive twice")
        seen.add(path)
        members.append((path, info))
    if not members:
        raise Invalid("the archive holds no files")
    return members, skipped


def _refuse_entry(info: zipfile.ZipInfo) -> None:
    """The rules on one entry that hold before any name is rewritten (spec § 7.3)."""
    name = info.filename
    if not (info.flag_bits & UTF8_FLAG or name.isascii()):
        raise Invalid("an archive entry's name is neither UTF-8 nor ASCII")
    if info.flag_bits & ENCRYPTED_FLAG:
        raise Invalid(f"{name} is encrypted")
    if info.compress_type not in METHODS:
        raise Invalid(f"{name} is compressed with a method other than stored or deflate")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise Invalid(f"{name} is a symbolic link")
    # zipfile turns a backslash into "/" on Windows only; refuse it everywhere.
    if name.startswith("/") or any(character in name for character in FORBIDDEN_CHARACTERS):
        raise Invalid("an archive entry's path cannot be absolute or hold a backslash")
    if any(segment in {".", ".."} for segment in name.split("/")):
        raise Invalid("an archive entry's path cannot contain '.' or '..'")


def _shared_folder(names: list[str]) -> str:
    """``dist/`` when every entry sits under that one folder, which a ``zip -r dist`` adds."""
    firsts = {name.split("/", 1)[0] for name in names if not _is_junk(name)}
    if len(firsts) != 1:
        return ""
    folder = next(iter(firsts))
    candidates = [name for name in names if not _is_junk(name)]
    return f"{folder}/" if all(name.startswith(f"{folder}/") for name in candidates) else ""


def _is_junk(path: str) -> bool:
    """Finder's ``__MACOSX/`` and dot-files (``.DS_Store``, ``.git/``), as the plugin skips."""
    segments = path.split("/")
    return segments[0] == JUNK_FOLDER or any(segment.startswith(".") for segment in segments)


def _inflate(
    reader: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    limits: ArchiveLimits,
    *,
    already: int,
) -> int:
    """Inflate one entry, counting every byte; the header's sizes are never believed."""
    written = 0
    compressed = max(info.compress_size, 1)
    with reader.open(info) as source, target.open("xb") as sink:
        while chunk := source.read(CHUNK):
            written += len(chunk)
            if already + written > limits.max_bytes:
                raise TooLarge(f"the archive inflates past {limits.max_bytes} bytes")
            if written > RATIO_FLOOR and written / compressed > limits.max_ratio:
                raise TooLarge(
                    f"{info.filename} inflates more than {limits.max_ratio} times its size"
                )
            sink.write(chunk)
    return written
