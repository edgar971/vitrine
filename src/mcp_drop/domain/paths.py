"""The one path rule, applied when writing and when resolving (spec § 4.3)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from mcp_drop.domain.errors import Invalid

MAX_PATH_BYTES = 255
INDEX_ENTRY = "index.html"
FORBIDDEN_CHARACTERS = ("\\", "\x00")


def validate_path(path: str) -> str:
    """Return the path if it is a safe relative POSIX path, else raise ``Invalid``."""
    if not path:
        raise Invalid("a file path cannot be empty")
    if len(path.encode()) > MAX_PATH_BYTES:
        raise Invalid(f"a file path cannot exceed {MAX_PATH_BYTES} bytes")
    if path.startswith("/"):
        raise Invalid("a file path cannot be absolute")
    if any(character in path for character in FORBIDDEN_CHARACTERS):
        raise Invalid("a file path cannot contain a backslash or NUL")
    for segment in path.split("/"):
        if segment in ("", ".", "..") or segment.startswith("."):
            raise Invalid(f"a file path cannot contain the segment {segment!r}")
    return path


def resolve_path(root: Path, path: str) -> Path:
    """Join a validated path to ``root``, refusing a symlink or an escape (spec § 4.3)."""
    target = root
    for segment in validate_path(path).split("/"):
        target = target / segment
        if target.is_symlink():
            raise Invalid(f"{path} is not a regular file")
    resolved = target.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise Invalid(f"{path} escapes the version directory")
    return resolved


def choose_entry(
    *, files: Sequence[str], requested: str | None = None, previous: str | None = None
) -> str:
    """Pick the file a link opens (spec § 4.3)."""
    if requested is not None:
        validate_path(requested)
        if requested not in files:
            raise Invalid(f"entry {requested!r} is not in this version")
        return requested
    if previous is not None and previous in files:
        return previous
    if INDEX_ENTRY in files:
        return INDEX_ENTRY
    if len(files) == 1:
        return files[0]
    raise Invalid(f"name an entry: this version has {len(files)} files and no {INDEX_ENTRY}")
