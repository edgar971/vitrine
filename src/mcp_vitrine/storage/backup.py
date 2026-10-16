"""``backup``: a restorable copy of a live ``data_dir`` (operations § 2).

The server's per-artifact locks live in its own process, so a backup run from the CLI cannot take
them. It does not need to: versions are immutable once placed, and ``meta.json`` only changes by
rename. Each artifact is copied optimistically: read ``meta.json``, copy the versions it lists,
read it again, and start over if it moved. The snapshot written is a ``meta.json`` whose every
version was copied while that very ``meta.json`` was current.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from mcp_vitrine.domain import layout
from mcp_vitrine.domain.errors import Conflict, StorageUnavailable

# A writer that keeps an artifact moving for this many attempts in a row is not a race to wait out.
ATTEMPTS = 50


@dataclass(frozen=True, slots=True)
class BackupReport:
    """What one backup copied."""

    artifacts: int = 0
    versions: int = 0
    bytes: int = 0


class _MovedError(Exception):
    """The artifact changed while it was being copied."""


def backup(source: Path, dest: Path) -> BackupReport:
    """Copy ``source`` into ``dest``, which must not exist or be empty; ``dest`` is a data_dir.

    Artifacts first, then idempotency records, then the audit log (closed months, the open one
    last), so the log is never behind the state it describes.
    """
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise Conflict(f"{dest} must be a new or empty directory")
    if not (source / layout.ARTIFACTS_DIR).is_dir():
        raise StorageUnavailable(f"{source} is not a vitrine data_dir")
    layout.ensure(dest)
    artifacts = versions = copied = 0
    for directory in sorted((source / layout.ARTIFACTS_DIR).iterdir()):
        result = _copy_artifact(directory, dest / layout.ARTIFACTS_DIR / directory.name)
        if result is not None:
            artifacts += 1
            versions += result[0]
            copied += result[1]
    copied += _copy_refs(source / layout.REFS_DIR, dest / layout.REFS_DIR)
    copied += _copy_audit(source / layout.AUDIT_DIR, dest / layout.AUDIT_DIR)
    return BackupReport(artifacts=artifacts, versions=versions, bytes=copied)


def _copy_artifact(source: Path, target: Path) -> tuple[int, int] | None:
    """Copy one artifact consistently; ``(versions, bytes)``, or ``None`` if it is gone."""
    for _ in range(ATTEMPTS):
        shutil.rmtree(target, ignore_errors=True)
        before = _read(source / layout.META_FILE)
        if before is None:
            return None
        try:
            names = _version_names(before)
            target.mkdir(parents=True)
            copied = sum(_copy_tree(source / name, target / name) for name in names)
        except _MovedError:
            continue
        if _read(source / layout.META_FILE) != before:
            continue
        (target / layout.META_FILE).write_bytes(before)
        return len(names), copied + len(before)
    raise StorageUnavailable(f"{source.name} kept changing; run the backup again")


def _version_names(meta: bytes) -> tuple[str, ...]:
    """The version directories one ``meta.json`` lists."""
    try:
        versions = json.loads(meta)["versions"]
        return tuple(f"{layout.VERSION_PREFIX}{int(version['n'])}" for version in versions)
    except (ValueError, KeyError, TypeError) as error:
        # A half-written meta.json cannot be seen (it is renamed into place), so this is a race
        # with a delete, or damage that a re-read will show again.
        raise _MovedError from error


def _copy_tree(source: Path, target: Path) -> int:
    """Copy one immutable version directory; ``_MovedError`` if it vanished (pruned or deleted)."""
    try:
        shutil.copytree(source, target, symlinks=True)
    except (FileNotFoundError, shutil.Error) as error:
        raise _MovedError from error
    return sum(path.stat().st_size for path in target.rglob("*") if path.is_file())


def _copy_refs(source: Path, target: Path) -> int:
    """Copy the idempotency records that read back whole; one mid-claim is not worth keeping."""
    copied = 0
    for path in sorted(source.glob("*/*")):
        data = _read(path)
        if data is None or not _is_json_object(data):
            continue
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        copied += len(data)
    return copied


def _copy_audit(source: Path, target: Path) -> int:
    """Copy each month, oldest first, up to its last complete line (a writer may be appending)."""
    copied = 0
    for path in sorted(source.glob("*.jsonl")):
        data = _read(path)
        if data is None:
            continue
        data = data[: data.rfind(b"\n") + 1]
        (target / path.name).write_bytes(data)
        copied += len(data)
    return copied


def _read(path: Path) -> bytes | None:
    """A file's bytes, or ``None`` if it is not there."""
    try:
        return path.read_bytes()
    except FileNotFoundError, NotADirectoryError:
        return None


def _is_json_object(data: bytes) -> bool:
    """Whether ``data`` parses as one JSON object."""
    try:
        return isinstance(json.loads(data), dict)
    except ValueError:
        return False
