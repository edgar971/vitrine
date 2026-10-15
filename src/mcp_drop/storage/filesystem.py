"""``FileStore``: artifacts as directories, exactly as ``docs/design.md`` § 5 writes them."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shutil
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from mcp_drop.domain import layout
from mcp_drop.domain.clock import Clock
from mcp_drop.domain.errors import (
    Conflict,
    DropError,
    Invalid,
    NotFound,
    OverQuota,
    SchemaMismatch,
    StorageUnavailable,
    TooLarge,
)
from mcp_drop.domain.ids import is_valid_id
from mcp_drop.domain.models import (
    Artifact,
    ArtifactDraft,
    ArtifactPage,
    ArtifactQuery,
    ArtifactSummary,
    FileEntry,
    Version,
    VersionDraft,
    Visibility,
)
from mcp_drop.domain.paths import choose_entry, resolve_path
from mcp_drop.storage.base import RefClaim, StoreConfig

REF_ID = "id"
REF_HASH = "request_hash"
REF_AT = "created_at"


@dataclass(frozen=True, slots=True)
class _Stage:
    """A staged version: where it is, what it holds and what it adds to the store."""

    path: Path
    entries: tuple[FileEntry, ...]
    new_bytes: int = 0

    @property
    def size(self) -> int:
        """The bytes the version weighs."""
        return sum(entry.bytes for entry in self.entries)

    @property
    def paths(self) -> tuple[str, ...]:
        """The version's relative paths, sorted, which is how the entry is chosen."""
        return tuple(entry.path for entry in self.entries)


class FileStore:
    """One directory per artifact, one immutable directory per version, one ``meta.json``."""

    def __init__(self, config: StoreConfig, clock: Clock) -> None:
        """Create the layout and keep one lock per artifact, so writers serialise."""
        self._config = config
        self._clock = clock
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._refs_lock = threading.Lock()
        self._writes_lock = threading.Lock()
        layout.ensure(config.root)

    def create(self, artifact_id: str, draft: ArtifactDraft, *, source: str) -> Artifact:
        """Write version 1 and its ``meta.json``; ``Conflict`` if the id is taken."""
        layout.artifact_dir(self._config.root, artifact_id)
        with self._locked(artifact_id):
            if layout.meta_file(self._config.root, artifact_id).exists():
                raise Conflict(f"artifact {artifact_id} already exists")
            with self._staged(draft.files) as stage:
                at = self._clock.now()
                entry = choose_entry(files=stage.paths, requested=draft.entry)
                with self._writes_lock:
                    self._refuse_oversized(stage)
                    self._place(artifact_id, stage.path)
                version = Version(
                    n=1, at=at, note=draft.note, bytes=stage.size, files=len(stage.entries)
                )
                artifact = Artifact(
                    id=artifact_id,
                    title=draft.title,
                    entry=entry,
                    version=1,
                    versions=(version,),
                    created_at=at,
                    updated_at=at,
                    expires_at=draft.expires_at,
                    source=source,
                    client_ref=draft.client_ref,
                    tags=draft.tags,
                )
                self._write_meta(artifact)
            return artifact

    def add_version(self, artifact_id: str, draft: VersionDraft) -> Artifact:
        """Write the next version and replace ``meta.json``; the caller prunes afterwards."""
        with self._locked(artifact_id):
            previous = self._read_meta(artifact_id)
            source = (
                layout.version_dir(self._config.root, artifact_id, previous.version)
                if draft.merge
                else None
            )
            with self._staged(draft.files or {}, merge_from=source) as stage:
                at = self._clock.now()
                entry = choose_entry(
                    files=stage.paths, requested=draft.entry, previous=previous.entry
                )
                number = previous.version + 1
                with self._writes_lock:
                    self._refuse_oversized(stage)
                    self._place(artifact_id, stage.path, number)
                version = Version(
                    n=number, at=at, note=draft.note, bytes=stage.size, files=len(stage.entries)
                )
                artifact = previous.model_copy(
                    update={
                        "version": number,
                        "versions": (*previous.versions, version),
                        "entry": entry,
                        "title": previous.title if draft.title is None else draft.title,
                        "tags": previous.tags if draft.tags is None else draft.tags,
                        "expires_at": _updated_expiry(previous, draft),
                        "updated_at": at,
                    }
                )
                self._write_meta(artifact)
            return artifact

    def prune(self, artifact_id: str) -> Artifact:
        """Drop versions beyond ``retain_versions``, from ``meta.json`` first (spec § 4.3)."""
        with self._locked(artifact_id):
            artifact = self._read_meta(artifact_id)
            keep = self._config.retain_versions
            if len(artifact.versions) <= keep:
                return artifact
            dropped, retained = artifact.versions[:-keep], artifact.versions[-keep:]
            pruned = artifact.model_copy(update={"versions": retained})
            self._write_meta(pruned)
            for version in dropped:
                shutil.rmtree(
                    layout.version_dir(self._config.root, artifact_id, version.n),
                    ignore_errors=True,
                )
            return pruned

    def get(self, artifact_id: str) -> Artifact:
        """Read one ``meta.json``; ``NotFound`` for a malformed or unknown id."""
        return self._read_meta(artifact_id)

    def files(self, artifact_id: str, version: int | None = None) -> tuple[FileEntry, ...]:
        """List a version's files; ``NotFound`` if that artifact or version is gone."""
        artifact = self._read_meta(artifact_id)
        directory = layout.version_dir(self._config.root, artifact_id, version or artifact.version)
        if not directory.is_dir():
            raise NotFound()
        return _scan_tree(directory)

    def read(self, artifact_id: str, path: str, *, version: int | None = None) -> bytes:
        """Read one file's bytes, applying the path rule when resolving (spec § 4.3)."""
        artifact = self._read_meta(artifact_id)
        directory = layout.version_dir(self._config.root, artifact_id, version or artifact.version)
        if not directory.is_dir():
            raise NotFound()
        target = resolve_path(directory, path)
        if not target.is_file():
            raise NotFound()
        return target.read_bytes()

    def list(self, query: ArtifactQuery) -> ArtifactPage:
        """Scan the ``meta.json`` files, newest first, one page at a time (spec § 5.1)."""
        summaries = [artifact.summary() for artifact in self._all() if _matches(artifact, query)]
        summaries.sort(key=_sort_key, reverse=True)
        if query.cursor is not None:
            after = _decode_cursor(query.cursor)
            summaries = [summary for summary in summaries if _sort_key(summary) < after]
        page = summaries[: query.limit]
        next_cursor = _encode_cursor(page[-1]) if len(summaries) > query.limit else None
        return ArtifactPage(items=tuple(page), next_cursor=next_cursor)

    def delete(self, artifact_id: str) -> None:
        """Remove the artifact and free its ``client_ref`` (spec § 4.5)."""
        directory = layout.artifact_dir(self._config.root, artifact_id)
        with self._locked(artifact_id):
            if not (directory / layout.META_FILE).exists():
                raise NotFound()
            try:
                shutil.rmtree(directory)
            except OSError as error:
                raise StorageUnavailable(
                    f"cannot delete {artifact_id}: {error.strerror or error}"
                ) from error
            with self._refs_lock:
                for path, claimed in self._ref_records():
                    if claimed is not None and claimed.get(REF_ID) == artifact_id:
                        path.unlink(missing_ok=True)

    def set_visibility(self, artifact_id: str, visibility: Visibility) -> Artifact:
        """Persist a visibility change, moving the link epoch with it (spec § 4.4)."""
        with self._locked(artifact_id):
            artifact = self._read_meta(artifact_id)
            if artifact.visibility is visibility:
                return artifact
            return self._update_meta(
                artifact,
                visibility=visibility,
                link_epoch=artifact.link_epoch + 1,
                updated_at=self._clock.now(),
            )

    def revoke_links(self, artifact_id: str) -> Artifact:
        """Increment ``link_epoch``, revoking every private link (spec § 2)."""
        with self._locked(artifact_id):
            artifact = self._read_meta(artifact_id)
            return self._update_meta(
                artifact, link_epoch=artifact.link_epoch + 1, updated_at=self._clock.now()
            )

    def replace_meta(self, artifact: Artifact) -> None:
        """Write ``meta.json`` as given, to compensate a mutation whose audit write failed."""
        with self._locked(artifact.id):
            self._write_meta(artifact)

    def drop_version(self, artifact_id: str, version: int) -> None:
        """Remove one version directory, leaving ``meta.json`` alone (compensation only)."""
        with self._locked(artifact_id):
            shutil.rmtree(
                layout.version_dir(self._config.root, artifact_id, version), ignore_errors=True
            )

    def claim_ref(self, *, client: str, ref: str, request_hash: str, artifact_id: str) -> RefClaim:
        """Claim ``client_ref`` exclusively, or report the artifact an earlier call made.

        The exclusive create makes the claim atomic across processes; the lock stops two threads
        from reading a record the other has created but not finished writing.
        """
        record = {
            REF_ID: artifact_id,
            REF_HASH: request_hash,
            REF_AT: self._clock.now().isoformat(),
        }
        path = _ref_path(self._config.root, client, ref)
        with self._refs_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if _claim_new_record(path, record):
                return RefClaim(id=artifact_id, request_hash=request_hash, reused=False)
            claimed = _read_ref(path)
            if claimed is not None and not self._ref_is_stale(claimed):
                if claimed[REF_HASH] != request_hash:
                    raise Conflict("client_ref is already bound to a different request")
                return RefClaim(id=str(claimed[REF_ID]), request_hash=request_hash, reused=True)
            _write_ref(path, record)
            return RefClaim(id=artifact_id, request_hash=request_hash, reused=False)

    def usage_bytes(self) -> int:
        """The bytes the versions hold, hard links counted once (spec § 8.2)."""
        seen: set[tuple[int, int]] = set()
        total = 0
        pattern = f"*/{layout.VERSION_PREFIX}*/**/*"
        for path in (self._config.root / layout.ARTIFACTS_DIR).glob(pattern):
            if path.is_symlink() or not path.is_file():
                continue
            info = path.stat()
            if (info.st_dev, info.st_ino) in seen:
                continue
            seen.add((info.st_dev, info.st_ino))
            total += info.st_size
        return total

    def expired_ids(self) -> tuple[str, ...]:
        """Ids past ``expires_at``; ``ArtifactService`` deletes and audits them (spec § 4.3)."""
        now = self._clock.now()
        return tuple(sorted(artifact.id for artifact in self._all() if artifact.is_expired(now)))

    def sweep_staging(self) -> int:
        """Remove ``tmp/`` and every version directory no ``meta.json`` references."""
        removed = 0
        for path in (self._config.root / layout.TMP_DIR).iterdir():
            _remove(path)
            removed += 1
        for directory in (self._config.root / layout.ARTIFACTS_DIR).iterdir():
            referenced = self._referenced_versions(directory)
            if referenced is None:
                continue
            for path in directory.iterdir():
                if path.is_dir() and path.name not in referenced:
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
        return removed

    def sweep_refs(self) -> int:
        """Remove idempotency records past ``ref_ttl`` (spec § 4.5: the sweep frees stale keys)."""
        removed = 0
        with self._refs_lock:
            for path, claimed in self._ref_records():
                if claimed is None or self._ref_is_stale(claimed):
                    path.unlink(missing_ok=True)
                    removed += 1
        return removed

    @contextmanager
    def _locked(self, artifact_id: str) -> Iterator[None]:
        """Serialise writers per artifact; readers take no lock (spec § 4.3)."""
        with self._locks_guard:
            lock = self._locks.setdefault(artifact_id, threading.Lock())
        with lock:
            yield

    @contextmanager
    def _staged(
        self, files: Mapping[str, bytes], merge_from: Path | None = None
    ) -> Iterator[_Stage]:
        """Stage the files under ``tmp/``; the stage is discarded unless the block places it."""
        stage = self._config.root / layout.TMP_DIR / uuid.uuid4().hex
        stage.mkdir(parents=True)
        shared = _inodes(merge_from) if merge_from is not None else frozenset()
        try:
            if merge_from is not None:
                _link_tree(merge_from, stage)
            for path, content in files.items():
                target = resolve_path(stage, path)
                _refuse_type_conflict(stage, target, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target.unlink()
                target.write_bytes(content)
            entries = _scan_tree(stage)
            if not entries:
                raise Invalid("a version needs at least one file")
            if len(entries) > self._config.max_files:
                raise TooLarge(f"a version holds at most {self._config.max_files} files")
            new_bytes = sum(
                entry.bytes for entry in entries if _inode(stage / entry.path) not in shared
            )
            yield _Stage(path=stage, entries=entries, new_bytes=new_bytes)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _refuse_oversized(self, stage: _Stage) -> None:
        """Refuse a version over ``max_artifact``, or one whose new bytes would break ``quota``."""
        if stage.size > self._config.max_artifact:
            raise TooLarge(f"a version is limited to {self._config.max_artifact} bytes")
        if self.usage_bytes() + stage.new_bytes > self._config.quota:
            raise OverQuota(f"the store is limited to {self._config.quota} bytes")

    def _place(self, artifact_id: str, stage: Path, version: int = 1) -> None:
        """Rename the stage into ``artifacts/<id>/v<n>``; a crash before this leaves only tmp."""
        directory = layout.artifact_dir(self._config.root, artifact_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = layout.version_dir(self._config.root, artifact_id, version)
        if target.exists():
            shutil.rmtree(target)
        stage.rename(target)

    def _write_meta(self, artifact: Artifact) -> None:
        """Replace ``meta.json`` in one rename, so a reader never sees half of it (spec § 4.3)."""
        staged = self._config.root / layout.TMP_DIR / f"{uuid.uuid4().hex}.json"
        staged.write_text(artifact.model_dump_json(indent=2) + "\n", encoding="utf-8")
        staged.replace(layout.meta_file(self._config.root, artifact.id))

    def _update_meta(self, artifact: Artifact, **changes: object) -> Artifact:
        """Apply changes and persist them in one replace."""
        updated = artifact.model_copy(update=changes)
        self._write_meta(updated)
        return updated

    def _read_meta(self, artifact_id: str) -> Artifact:
        """Read and validate one ``meta.json``, mapping anything unreadable to a typed error."""
        path = layout.meta_file(self._config.root, artifact_id)
        try:
            return Artifact.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise NotFound() from None
        except (ValidationError, UnicodeDecodeError) as error:
            raise SchemaMismatch(f"{artifact_id}: meta.json is unreadable ({error})") from error

    def _all(self) -> Iterator[Artifact]:
        """Every readable artifact, in no particular order."""
        for directory in sorted((self._config.root / layout.ARTIFACTS_DIR).iterdir()):
            if not (directory / layout.META_FILE).exists():
                continue
            yield self._read_meta(directory.name)

    def _referenced_versions(self, directory: Path) -> set[str] | None:
        """The version directories ``meta.json`` names, or ``None`` when it cannot be read."""
        meta = directory / layout.META_FILE
        if not meta.exists():
            return set()
        try:
            artifact = self._read_meta(directory.name)
        except DropError:
            return None
        return {f"{layout.VERSION_PREFIX}{version.n}" for version in artifact.versions}

    def _ref_records(self) -> Iterator[tuple[Path, dict[str, object] | None]]:
        """Every idempotency record, with the file it lives in."""
        for path in sorted((self._config.root / layout.REFS_DIR).glob("*/*")):
            yield path, _read_ref(path)

    def _ref_is_stale(self, claimed: Mapping[str, object]) -> bool:
        """Whether a record outlived ``ref_ttl``.

        A claim whose artifact has not landed yet is live, not stale: freeing it early is what
        lets a retried create build a second artifact under the same key (spec § 4.5).
        """
        claimed_at = _parse_time(claimed.get(REF_AT))
        artifact_id = claimed.get(REF_ID)
        if claimed_at is None or not isinstance(artifact_id, str) or not is_valid_id(artifact_id):
            return True
        return self._clock.now() - claimed_at >= self._config.ref_ttl


def _ref_path(root: Path, client: str, ref: str) -> Path:
    """``refs/<client>/<sha256(ref)>`` (spec § 4.1)."""
    return layout.ref_dir(root, client) / hashlib.sha256(ref.encode()).hexdigest()


def _claim_new_record(path: Path, record: Mapping[str, object]) -> bool:
    """Create the record exclusively, reporting whether this call is the one that made it."""
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(record, handle)
    except FileExistsError:
        return False
    return True


def _read_ref(path: Path) -> dict[str, object] | None:
    """Read a record, or ``None`` when it is unreadable."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _write_ref(path: Path, record: Mapping[str, object]) -> None:
    """Replace a record in one rename."""
    staged = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    staged.write_text(json.dumps(record), encoding="utf-8")
    staged.replace(path)


def _link_tree(source: Path, target: Path) -> None:
    """Hard-link an immutable version into the stage, so a merge never copies bytes."""
    for path in sorted(source.rglob("*")):
        destination = target / path.relative_to(source)
        if path.is_dir():
            destination.mkdir(exist_ok=True)
        elif path.is_file():
            os.link(path, destination)


def _refuse_type_conflict(stage: Path, target: Path, path: str) -> None:
    """Refuse a path that needs a directory where the version already holds a file (spec § 10)."""
    if target.is_dir():
        raise Invalid(f"{path} names a directory that already exists in this version")
    parent = target.parent
    while parent != stage:
        if parent.exists() and not parent.is_dir():
            raise Invalid(
                f"{path} needs the directory {parent.name}, which this version holds as a file"
            )
        parent = parent.parent


def _inode(path: Path) -> tuple[int, int]:
    """The identity of one file, which a hard link shares and a fresh copy does not."""
    info = path.stat()
    return info.st_dev, info.st_ino


def _inodes(root: Path) -> frozenset[tuple[int, int]]:
    """The identities of every file under ``root``."""
    return frozenset(_inode(path) for path in root.rglob("*") if path.is_file())


def _scan_tree(directory: Path) -> tuple[FileEntry, ...]:
    """The regular files under a version directory, by relative path, symlinks ignored."""
    entries = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        entries.append(
            FileEntry(path=path.relative_to(directory).as_posix(), bytes=path.stat().st_size)
        )
    return tuple(entries)


def _remove(path: Path) -> None:
    """Remove a staging entry, whatever it is."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _updated_expiry(previous: Artifact, draft: VersionDraft) -> datetime | None:
    """The expiry an update leaves behind (spec § 5.1)."""
    if draft.clear_expiry:
        return None
    return previous.expires_at if draft.expires_at is None else draft.expires_at


def _sort_key(summary: ArtifactSummary) -> tuple[datetime, str]:
    """Newest first, with the id breaking ties, so a cursor is stable (spec § 5.1)."""
    return summary.updated_at, summary.id


def _matches(artifact: Artifact, query: ArtifactQuery) -> bool:
    """Whether one artifact satisfies every filter (spec § 5.1)."""
    if query.visibility is not None and artifact.visibility is not query.visibility:
        return False
    if query.source is not None and artifact.source != query.source:
        return False
    if query.tag is not None and query.tag.casefold() not in {
        tag.casefold() for tag in artifact.tags
    }:
        return False
    if query.query is None:
        return True
    needle = query.query.casefold()
    return needle in artifact.title.casefold() or any(
        needle in tag.casefold() for tag in artifact.tags
    )


def _encode_cursor(summary: ArtifactSummary) -> str:
    """An opaque cursor: where the next page starts."""
    raw = f"{summary.updated_at.isoformat()}|{summary.id}".encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Undo :func:`_encode_cursor`, refusing anything malformed."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        stamp, artifact_id = base64.urlsafe_b64decode(padded).decode().split("|")
        return datetime.fromisoformat(stamp), artifact_id
    except (ValueError, binascii.Error, UnicodeDecodeError) as error:
        raise Invalid("the cursor is not a cursor") from error


def _parse_time(value: object) -> datetime | None:
    """Parse a stored timestamp, or ``None`` when it is missing or malformed."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
