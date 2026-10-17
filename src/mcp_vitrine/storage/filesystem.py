"""``FileStore``: artifacts as directories, exactly as ``docs/design.md`` § 5 writes them."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

from pydantic import ValidationError

from mcp_vitrine.domain import layout
from mcp_vitrine.domain.clock import Clock
from mcp_vitrine.domain.errors import (
    Conflict,
    Invalid,
    NotFound,
    OverQuota,
    Refused,
    SchemaMismatch,
    StorageUnavailable,
    TooLarge,
    VitrineError,
)
from mcp_vitrine.domain.ids import is_valid_id
from mcp_vitrine.domain.models import (
    LEGACY_NONCES_KEPT,
    Artifact,
    ArtifactDraft,
    ArtifactPage,
    ArtifactQuery,
    Content,
    Facets,
    FileEntry,
    LinkRecord,
    ListSort,
    MetaChange,
    SpentNonce,
    Spooled,
    Version,
    VersionDraft,
    Visibility,
)
from mcp_vitrine.domain.paths import choose_entry, resolve_path, validate_path
from mcp_vitrine.storage.base import CHANGED, PUBLIC_UPDATE, RefClaim, StoreConfig

REF_ID = "id"
REF_HASH = "request_hash"
REF_AT = "created_at"
UPLOAD_REUSED = "this upload URL was already used"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


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
        # A running total, so the quota check under ``_writes_lock`` never walks the store; every
        # change to ``_usage`` also moves ``_generation``, which tells a re-measure it raced one.
        self._usage = _measure(config.root)
        self._generation = 0

    def create(self, artifact_id: str, draft: ArtifactDraft, *, source: str) -> Artifact:
        """Write version 1 and its ``meta.json``; ``Conflict`` if the id is taken."""
        layout.artifact_dir(self._config.root, artifact_id)
        with self._locked(artifact_id):
            if layout.meta_file(self._config.root, artifact_id).exists():
                raise Conflict(f"artifact {artifact_id} already exists")
            with self._staged(draft.files) as stage:
                at = self._clock.now()
                entry = choose_entry(files=stage.paths, requested=draft.entry)
                fallback = _fallback(stage.paths, draft.fallback)
                self._clear_orphan(artifact_id, 1)
                with self._writes_lock:
                    self._refuse_oversized(stage)
                    self._place(artifact_id, stage.path)
                    self._count(stage.new_bytes)
                version = Version(
                    n=1,
                    at=at,
                    note=draft.note,
                    bytes=stage.size,
                    files=len(stage.entries),
                    fallback=fallback,
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
                    project=draft.project,
                    fallback=fallback,
                    used_upload_nonces=_spend((), draft.upload, at),
                )
                self._write_meta(artifact)
            return artifact

    def add_version(
        self,
        artifact_id: str,
        draft: VersionDraft,
        *,
        if_version: int | None = None,
        if_private: bool = False,
    ) -> Artifact:
        """Write the next version and replace ``meta.json``; the caller prunes afterwards."""
        with self._locked(artifact_id):
            previous = self._read_meta(artifact_id)
            _check_version(previous, if_version)
            if if_private and previous.visibility is Visibility.PUBLIC:
                raise Refused(PUBLIC_UPDATE)
            # Checked and spent under the artifact's lock, so two PUTs of one URL cannot both land.
            if draft.upload is not None and any(
                spent.nonce == draft.upload.nonce for spent in previous.used_upload_nonces
            ):
                raise Conflict(UPLOAD_REUSED)
            # No files means nothing replaces the current ones: the version carries them over.
            source = (
                layout.version_dir(self._config.root, artifact_id, previous.version)
                if draft.merge or draft.files is None
                else None
            )
            with self._staged(draft.files or {}, merge_from=source) as stage:
                at = self._clock.now()
                entry = choose_entry(
                    files=stage.paths, requested=draft.entry, previous=previous.entry
                )
                fallback = _fallback(
                    stage.paths,
                    previous.fallback if draft.fallback is None else draft.fallback or None,
                )
                number = previous.version + 1
                self._clear_orphan(artifact_id, number)
                with self._writes_lock:
                    self._refuse_oversized(stage)
                    self._place(artifact_id, stage.path, number)
                    self._count(stage.new_bytes)
                version = Version(
                    n=number,
                    at=at,
                    note=draft.note,
                    bytes=stage.size,
                    files=len(stage.entries),
                    fallback=fallback,
                )
                artifact = previous.model_copy(
                    update={
                        "version": number,
                        "versions": (*previous.versions, version),
                        "entry": entry,
                        "title": previous.title if draft.title is None else draft.title,
                        "tags": previous.tags if draft.tags is None else draft.tags,
                        "project": previous.project
                        if draft.project is None
                        else (draft.project or None),
                        "expires_at": _updated_expiry(previous, draft),
                        "fallback": fallback,
                        "updated_at": at,
                        "used_upload_nonces": _spend(previous.used_upload_nonces, draft.upload, at),
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
                self._remove_counted(layout.version_dir(self._config.root, artifact_id, version.n))
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
        return self.locate(artifact_id, path, version=version).read_bytes()

    def locate(self, artifact_id: str, path: str, *, version: int | None = None) -> Path:
        """The local file behind one path, for streaming it; the same rules as ``read``."""
        artifact = self._read_meta(artifact_id)
        directory = layout.version_dir(self._config.root, artifact_id, version or artifact.version)
        if not directory.is_dir():
            raise NotFound()
        target = resolve_path(directory, path)
        if not target.is_file():
            raise NotFound()
        return target

    def list(self, query: ArtifactQuery) -> ArtifactPage:
        """Scan the ``meta.json`` files, in the query's order, one page at a time (spec § 5.1)."""
        now = self._clock.now()
        matched = [artifact for artifact in self._all() if _matches(artifact, query, now)]
        # Every order is ascending on its key: the time orders negate the timestamp, newest first.
        matched.sort(key=partial(_sort_key, query.sort))
        rest = matched
        if query.cursor is not None:
            after = _decode_cursor(query.cursor, query.sort)
            rest = [artifact for artifact in matched if _sort_key(query.sort, artifact) > after]
        page = rest[: query.limit]
        return ArtifactPage(
            items=tuple(artifact.summary() for artifact in page),
            next_cursor=_encode_cursor(query.sort, page[-1]) if len(rest) > query.limit else None,
            # Over the whole match, not the page, so a client can group without paging through.
            facets=_facets(matched) if query.facets else None,
        )

    def delete(self, artifact_id: str) -> None:
        """Remove the artifact and free its ``client_ref`` (spec § 4.5)."""
        directory = layout.artifact_dir(self._config.root, artifact_id)
        with self._locked(artifact_id):
            if not (directory / layout.META_FILE).exists():
                raise NotFound()
            # No other artifact shares an inode with this one, so every byte it holds is freed.
            freed = _measure_tree(directory)
            try:
                shutil.rmtree(directory)
            except OSError as error:
                self.resync_usage()
                raise StorageUnavailable(
                    f"cannot delete {artifact_id}: {error.strerror or error}"
                ) from error
            with self._writes_lock:
                self._count(-freed)
            with self._refs_lock:
                for path, claimed in self._ref_records():
                    if claimed is not None and claimed.get(REF_ID) == artifact_id:
                        path.unlink(missing_ok=True)

    def set_visibility(
        self,
        artifact_id: str,
        visibility: Visibility,
        *,
        if_version: int | None = None,
        public_until: datetime | None = None,
    ) -> Artifact:
        """Persist a visibility change, moving the link epoch with it (spec § 4.4).

        A change of visibility ends every private link, recorded ones included. A public artifact
        asked to be public again only moves its window (``public_until``): the links stay.
        """
        with self._locked(artifact_id):
            artifact = self._read_meta(artifact_id)
            _check_version(artifact, if_version)
            if artifact.visibility is visibility:
                if visibility is Visibility.PRIVATE or artifact.public_until == public_until:
                    return artifact
                return self._update_meta(
                    artifact, public_until=public_until, updated_at=self._clock.now()
                )
            return self._update_meta(
                artifact,
                visibility=visibility,
                public_until=public_until if visibility is Visibility.PUBLIC else None,
                link_epoch=artifact.link_epoch + 1,
                links=(),
                updated_at=self._clock.now(),
            )

    def revoke_links(self, artifact_id: str) -> Artifact:
        """Increment ``link_epoch`` and drop every link record, revoking every private link."""
        with self._locked(artifact_id):
            artifact = self._read_meta(artifact_id)
            return self._update_meta(
                artifact, link_epoch=artifact.link_epoch + 1, links=(), updated_at=self._clock.now()
            )

    def add_link(self, artifact_id: str, record: LinkRecord, *, limit: int) -> Artifact:
        """Record one minted link; expired records go first, and ``limit`` live ones at most."""
        with self._locked(artifact_id):
            artifact = self._read_meta(artifact_id)
            live = _live_links(artifact, self._clock.now())
            if len(live) >= limit:
                raise TooLarge(f"an artifact holds at most {limit} live links: revoke links first")
            return self._update_meta(artifact, links=(*live, record))

    def remove_link(self, artifact_id: str, lid: str) -> Artifact:
        """Delete one link record, which revokes that link alone; ``NotFound`` if it is gone."""
        with self._locked(artifact_id):
            artifact = self._read_meta(artifact_id)
            if artifact.link(lid) is None:
                raise NotFound()
            return self._update_meta(
                artifact, links=tuple(record for record in artifact.links if record.lid != lid)
            )

    def set_meta(self, artifact_id: str, change: MetaChange) -> tuple[Artifact, Artifact]:
        """Apply a metadata change to ``meta.json`` alone: no version, no epoch (spec § 5.1)."""
        with self._locked(artifact_id):
            artifact = self._read_meta(artifact_id)
            changes = change.applied_to(artifact)
            if not changes:
                return artifact, artifact
            return artifact, self._update_meta(artifact, **changes, updated_at=self._clock.now())

    def replace_meta(self, artifact: Artifact, *, if_current: Artifact | None = None) -> bool:
        """Write ``meta.json`` as given, to compensate a mutation whose audit write failed.

        With ``if_current``, only while ``meta.json`` is still exactly that: a change that landed
        since was audited on its own, and putting an older snapshot back would undo it.
        """
        with self._locked(artifact.id):
            if if_current is not None and self._read_meta(artifact.id) != if_current:
                return False
            self._write_meta(artifact)
            return True

    def vitrine_version(self, artifact_id: str, version: int) -> None:
        """Remove one version directory, leaving ``meta.json`` alone (compensation only)."""
        with self._locked(artifact_id):
            self._remove_counted(layout.version_dir(self._config.root, artifact_id, version))

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

    def release_ref(self, *, client: str, ref: str, artifact_id: str) -> None:
        """Free a ``client_ref`` whose create failed, unless it now names another artifact."""
        path = _ref_path(self._config.root, client, ref)
        with self._refs_lock:
            claimed = _read_ref(path)
            if claimed is not None and claimed[REF_ID] == artifact_id:
                path.unlink(missing_ok=True)

    def usage_bytes(self) -> int:
        """The bytes the versions hold, hard links counted once (spec § 8.2).

        A running total: measured once when the store opens and moved by every write and removal
        since, so the quota check does not walk the store under the store-wide write lock.
        """
        return self._usage

    def resync_usage(self) -> None:
        """Measure the store again, without the write lock, and keep it if nothing moved meanwhile.

        Another process (a CLI sweep beside the server) changes the disk without telling this
        one; the periodic sweep calls this so the total follows. A measure that raced a write is
        dropped rather than trusted, and the next sweep tries again.
        """
        with self._writes_lock:
            generation = self._generation
        measured = _measure(self._config.root)
        with self._writes_lock:
            if self._generation == generation:
                self._usage = measured

    def count(self) -> int:
        """How many artifacts have a ``meta.json``; nothing is parsed, so scraping stays cheap."""
        return sum(
            1 for _ in (self._config.root / layout.ARTIFACTS_DIR).glob(f"*/{layout.META_FILE}")
        )

    def expired_ids(self) -> tuple[str, ...]:
        """Ids past ``expires_at``; ``ArtifactService`` deletes and audits them (spec § 4.3)."""
        now = self._clock.now()
        return tuple(sorted(artifact.id for artifact in self._all() if artifact.is_expired(now)))

    def lapsed_ids(self) -> tuple[str, ...]:
        """Ids still public on disk past ``public_until``; the service makes them private."""
        now = self._clock.now()
        return tuple(sorted(artifact.id for artifact in self._all() if artifact.has_lapsed(now)))

    def sweep_links(self) -> int:
        """Drop the link records past their expiry; how many went (spec § 4.3)."""
        now = self._clock.now()
        removed = 0
        for stale in [
            artifact for artifact in self._all() if _live_links(artifact, now) != artifact.links
        ]:
            with self._locked(stale.id):
                try:
                    artifact = self._read_meta(stale.id)
                except NotFound:
                    continue
                live = _live_links(artifact, now)
                removed += len(artifact.links) - len(live)
                self._update_meta(artifact, links=live)
        return removed

    def sweep_staging(self, *, grace: timedelta = timedelta(0)) -> int:
        """Remove ``tmp/`` and every version directory no ``meta.json`` references.

        Only entries older than ``grace`` go: a live server is staging into ``tmp/``, and a placed
        version is unreferenced until its ``meta.json`` lands. Both take seconds, so a sweep beside
        a running server passes a grace; the startup sweep, with no writer yet, passes none.
        """
        cutoff = time.time() - grace.total_seconds()
        removed = 0
        for path in (self._config.root / layout.TMP_DIR).iterdir():
            if _modified(path) <= cutoff:
                _remove(path)
                removed += 1
        for directory in (self._config.root / layout.ARTIFACTS_DIR).iterdir():
            referenced = self._referenced_versions(directory)
            if referenced is None:
                continue
            for path in directory.iterdir():
                if path.is_dir() and path.name not in referenced and _modified(path) <= cutoff:
                    self._remove_counted(path)
                    removed += 1
        self.resync_usage()
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
        self, files: Mapping[str, Content], merge_from: Path | None = None
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
                if isinstance(content, Spooled):
                    # A rename within data_dir: a streamed upload is never copied or read back.
                    shutil.move(content.path, target)
                else:
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

    def _count(self, delta: int) -> None:
        """Move the running total; the caller holds ``_writes_lock``."""
        self._usage = max(0, self._usage + delta)
        self._generation += 1

    def _remove_counted(self, version: Path) -> None:
        """Remove one version directory and take the bytes only it held off the total.

        A file another version hard-links is still on disk afterwards, so it frees nothing.
        """
        freed = sum(info.st_size for info in _file_stats(version) if info.st_nlink == 1)
        shutil.rmtree(version, ignore_errors=True)
        with self._writes_lock:
            self._count(-freed)

    def _clear_orphan(self, artifact_id: str, version: int) -> None:
        """Remove a ``v<n>`` a crash left between the two renames, so this write can take it.

        Outside the store-wide write lock: the artifact's lock already keeps it from coming back.
        """
        orphan = layout.version_dir(self._config.root, artifact_id, version)
        if orphan.exists():
            self._remove_counted(orphan)

    def _place(self, artifact_id: str, stage: Path, version: int = 1) -> None:
        """Rename the stage into ``artifacts/<id>/v<n>``; a crash before this leaves only tmp."""
        directory = layout.artifact_dir(self._config.root, artifact_id)
        directory.mkdir(parents=True, exist_ok=True)
        stage.rename(layout.version_dir(self._config.root, artifact_id, version))

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
        except VitrineError:
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
    # ``target`` is resolved, so the walk must stop at the resolved stage: a relative data_dir or
    # one reached through a symlink would otherwise never compare equal, and the walk never ends.
    stage = stage.resolve()
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


def _file_stats(root: Path) -> Iterator[os.stat_result]:
    """The ``stat`` of every regular file under ``root``, symlinks ignored."""
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            yield path.stat()
        except FileNotFoundError:
            continue


def _measure_tree(root: Path) -> int:
    """The bytes under ``root``, hard links counted once."""
    seen: set[tuple[int, int]] = set()
    total = 0
    for info in _file_stats(root):
        if (info.st_dev, info.st_ino) not in seen:
            seen.add((info.st_dev, info.st_ino))
            total += info.st_size
    return total


def _measure(root: Path) -> int:
    """The bytes every version directory holds, hard links counted once (spec § 8.2)."""
    artifacts = root / layout.ARTIFACTS_DIR
    return sum(
        _measure_tree(version)
        for version in artifacts.glob(f"*/{layout.VERSION_PREFIX}*")
        if version.is_dir()
    )


def _modified(path: Path) -> float:
    """When an entry last changed; one that vanished meanwhile counts as old (it is gone)."""
    try:
        return path.lstat().st_mtime
    except FileNotFoundError:
        return 0.0


def _remove(path: Path) -> None:
    """Remove a staging entry, whatever it is."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _fallback(files: tuple[str, ...], fallback: str | None) -> str | None:
    """The fallback a version keeps; it must name one of its files (spec § 4.3)."""
    if fallback is None:
        return None
    validate_path(fallback)
    if fallback not in files:
        raise Invalid(
            f"fallback {fallback!r} is not in this version: name another, or clear it with ''"
        )
    return fallback


def _updated_expiry(previous: Artifact, draft: VersionDraft) -> datetime | None:
    """The expiry an update leaves behind (spec § 5.1)."""
    if draft.clear_expiry:
        return None
    return previous.expires_at if draft.expires_at is None else draft.expires_at


def _sort_key(sort: ListSort, artifact: Artifact) -> tuple[int | str, str]:
    """An ascending key for one order, with the id breaking ties, so a cursor is stable.

    The time orders are newest first, so their key is the negated time, in whole microseconds:
    a float would round two close times together.
    """
    match sort:
        case ListSort.UPDATED:
            return -_microseconds(artifact.updated_at), artifact.id
        case ListSort.CREATED:
            return -_microseconds(artifact.created_at), artifact.id
        case ListSort.TITLE:
            return artifact.title.casefold(), artifact.id


def _microseconds(moment: datetime) -> int:
    """``moment`` as an exact count of microseconds since the epoch."""
    return (moment - EPOCH) // timedelta(microseconds=1)


def _matches(artifact: Artifact, query: ArtifactQuery, now: datetime) -> bool:
    """Whether one artifact satisfies every filter and every search term (spec § 5.1)."""
    filters = (
        query.visibility is None or artifact.visibility is query.visibility,
        query.source is None or artifact.source == query.source,
        query.project is None or artifact.project == query.project,
        query.tag is None or query.tag.casefold() in {tag.casefold() for tag in artifact.tags},
        query.updated_after is None or artifact.updated_at > query.updated_after,
        query.expiring_within is None
        or (artifact.expires_at is not None and artifact.expires_at <= now + query.expiring_within),
    )
    if not all(filters):
        return False
    haystack = tuple(text.casefold() for text in artifact.text())
    return all(any(term in text for text in haystack) for term in query.terms)


def _facets(artifacts: Iterable[Artifact]) -> Facets:
    """Count the projects and the tags of the artifacts, each artifact once per name."""
    projects: Counter[str] = Counter()
    tags: Counter[str] = Counter()
    for artifact in artifacts:
        if artifact.project is not None:
            projects[artifact.project] += 1
        tags.update(set(artifact.tags))
    return Facets(projects=dict(sorted(projects.items())), tags=dict(sorted(tags.items())))


def _encode_cursor(sort: ListSort, artifact: Artifact) -> str:
    """An opaque cursor: the order it belongs to, and where the next page starts."""
    key, artifact_id = _sort_key(sort, artifact)
    raw = json.dumps([sort.value, key, artifact_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_cursor(cursor: str, sort: ListSort) -> tuple[int | str, str]:
    """Undo :func:`_encode_cursor`, refusing anything malformed or from another order."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, binascii.Error) as error:
        raise Invalid("the cursor is not a cursor") from error
    match decoded:
        case [str() as order, int() | str() as key, str() as artifact_id] if not isinstance(
            key, bool
        ):
            pass
        case _:
            raise Invalid("the cursor is not a cursor")
    if order != sort.value:
        raise Invalid(f"the cursor belongs to sort={order}, not sort={sort.value}")
    if isinstance(key, str) is not (sort is ListSort.TITLE):
        raise Invalid("the cursor is not a cursor")
    return key, artifact_id


def _parse_time(value: object) -> datetime | None:
    """Parse a stored timestamp, or ``None`` when it is missing or malformed."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _spend(
    spent: tuple[SpentNonce, ...], upload: SpentNonce | None, now: datetime
) -> tuple[SpentNonce, ...]:
    """Record a used upload nonce, dropping only those whose URL has expired (spec § 7.3).

    A count cap would forget a nonce while its URL still verifies. The upload rate limit and
    ``upload_ttl_max`` bound how many can be live at once.
    """
    if upload is None:
        return spent
    live = [nonce for nonce in spent if nonce.is_live(now)]
    legacy = [nonce for nonce in live if nonce.expires_at is None][-LEGACY_NONCES_KEPT:]
    dated = [nonce for nonce in live if nonce.expires_at is not None]
    return (*legacy, *dated, upload)


def _live_links(artifact: Artifact, now: datetime) -> tuple[LinkRecord, ...]:
    """The records whose link has not expired yet."""
    return tuple(record for record in artifact.links if record.expires_at > now)


def _check_version(artifact: Artifact, expected: int | None) -> None:
    """Refuse a change that was decided against a version that is no longer the current one."""
    if expected is not None and artifact.version != expected:
        raise Conflict(CHANGED)
