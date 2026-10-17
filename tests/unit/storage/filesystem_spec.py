"""``FileStore`` details the contract suite cannot see: the layout and the crash windows."""

from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

from mcp_vitrine.domain import layout
from mcp_vitrine.domain.clock import Clock
from mcp_vitrine.domain.errors import NotFound, OverQuota, SchemaMismatch
from mcp_vitrine.domain.ids import new_id
from mcp_vitrine.domain.models import SCHEMA_VERSION, ArtifactDraft, VersionDraft
from mcp_vitrine.storage import filesystem
from mcp_vitrine.storage.base import StoreConfig
from mcp_vitrine.storage.filesystem import FileStore

ENTRY = "index.html"
CLIENT = "hermes"


@pytest.fixture
def store(store_config: StoreConfig, clock: Clock) -> FileStore:
    """A filesystem store rooted in the test's temporary directory."""
    return FileStore(store_config, clock)


def _draft(**changes: object) -> ArtifactDraft:
    """A one-file draft, with ``changes`` applied."""
    fields: dict[str, object] = {"title": "Energy, September", "files": {ENTRY: b"<h1>hi</h1>"}}
    fields.update(changes)
    return ArtifactDraft.model_validate(fields)


def _create(store: FileStore, **changes: object) -> str:
    """Create an artifact and return its id."""
    artifact_id = new_id()
    store.create(artifact_id, _draft(**changes), source=CLIENT)
    return artifact_id


def _fail_on_meta_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the ``meta.json`` replace fail, which is the crash between the two renames."""
    original = Path.replace

    def replace(source: Path, target: Path) -> Path:
        if target.name == layout.META_FILE:
            raise OSError("the disk went away")
        return original(source, target)

    monkeypatch.setattr(Path, "replace", replace)


class DescribeTheLayout:
    class WhenTheStoreStarts:
        def it_creates_the_documented_directories(
            self, store: FileStore, store_config: StoreConfig
        ) -> None:
            assert [path.name for path in sorted(store_config.root.iterdir())] == [
                layout.ARTIFACTS_DIR,
                layout.AUDIT_DIR,
                layout.REFS_DIR,
                layout.TMP_DIR,
            ]

    class WhenAnArtifactIsStored:
        def it_writes_meta_json_where_spec_4_1_says(
            self, store: FileStore, store_config: StoreConfig
        ) -> None:
            assert layout.meta_file(store_config.root, _create(store)).is_file()

        def it_writes_the_version_directory(
            self, store: FileStore, store_config: StoreConfig
        ) -> None:
            artifact_id = _create(store)
            assert layout.version_dir(store_config.root, artifact_id, 1).joinpath(ENTRY).is_file()

        def it_hashes_the_idempotency_record_into_place(
            self, store: FileStore, store_config: StoreConfig
        ) -> None:
            store.claim_ref(
                client=CLIENT, ref="weekly", request_hash="aa", artifact_id=_create(store)
            )
            digest = hashlib.sha256(b"weekly").hexdigest()
            assert (store_config.root / layout.REFS_DIR / CLIENT / digest).is_file()

    class WhenTheIdIsMalformed:
        def it_is_not_found_before_any_filesystem_access(
            self, store: FileStore, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            def boom(*args: object, **kwargs: object) -> bool:
                raise AssertionError("the store touched the filesystem")

            monkeypatch.setattr(Path, "exists", boom)
            monkeypatch.setattr(Path, "read_text", boom)
            with pytest.raises(NotFound):
                store.get("not-an-id")


class DescribeTheRoot:
    class WhenTheDataDirIsReachedThroughASymlink:
        def it_writes_a_nested_file(self, tmp_path: Path, clock: Clock) -> None:
            (tmp_path / "real").mkdir()
            (tmp_path / "link").symlink_to(tmp_path / "real")
            store = FileStore(StoreConfig(root=tmp_path / "link" / "data"), clock)
            artifact_id = _create(store, files={ENTRY: b"<p>hi</p>", "a/b.md": b"# hi"})
            assert store.read(artifact_id, "a/b.md") == b"# hi"

    class WhenTheDataDirIsRelative:
        def it_writes_a_nested_file(
            self, tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            monkeypatch.chdir(tmp_path)
            store = FileStore(StoreConfig(root=Path("data")), clock)
            artifact_id = _create(store, files={ENTRY: b"<p>hi</p>", "a/b.md": b"# hi"})
            assert store.read(artifact_id, "a/b.md") == b"# hi"


class DescribeTheCrashWindows:
    class WhenTheProcessDiesBetweenTheTwoRenames:
        def it_leaves_no_visible_partial_version(
            self,
            store: FileStore,
            store_config: StoreConfig,
            clock: Clock,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            artifact_id = _create(store)
            with monkeypatch.context() as patched:
                _fail_on_meta_replace(patched)
                with pytest.raises(OSError, match="the disk went away"):
                    store.add_version(artifact_id, VersionDraft(files={"report.html": b"new"}))
            reopened = FileStore(store_config, clock)
            assert reopened.get(artifact_id).version == 1
            assert [entry.path for entry in reopened.files(artifact_id)] == [ENTRY]

        def it_hides_the_new_file(
            self,
            store: FileStore,
            store_config: StoreConfig,
            clock: Clock,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            artifact_id = _create(store)
            with monkeypatch.context() as patched:
                _fail_on_meta_replace(patched)
                with pytest.raises(OSError, match="the disk went away"):
                    store.add_version(artifact_id, VersionDraft(files={"report.html": b"new"}))
            reopened = FileStore(store_config, clock)
            with pytest.raises(NotFound):
                reopened.read(artifact_id, "report.html")

        def it_leaves_an_orphan_that_the_sweep_removes(
            self,
            store: FileStore,
            store_config: StoreConfig,
            clock: Clock,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            artifact_id = _create(store)
            with monkeypatch.context() as patched:
                _fail_on_meta_replace(patched)
                with pytest.raises(OSError, match="the disk went away"):
                    store.add_version(artifact_id, VersionDraft(files={"report.html": b"new"}))
            assert store.sweep_staging() >= 1
            assert not layout.version_dir(store_config.root, artifact_id, 2).exists()

    class WhenTheProcessDiesBeforeTheRename:
        def it_leaves_no_visible_version(
            self,
            store: FileStore,
            store_config: StoreConfig,
            clock: Clock,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            artifact_id = new_id()
            with monkeypatch.context() as patched:
                patched.setattr(Path, "write_bytes", _refuse_write)
                with pytest.raises(OSError, match="the disk went away"):
                    store.create(artifact_id, _draft(), source=CLIENT)
            reopened = FileStore(store_config, clock)
            with pytest.raises(NotFound):
                reopened.get(artifact_id)

        def it_leaves_the_staging_directory_empty(
            self, store: FileStore, store_config: StoreConfig, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            with monkeypatch.context() as patched:
                patched.setattr(Path, "write_bytes", _refuse_write)
                with pytest.raises(OSError, match="the disk went away"):
                    store.create(new_id(), _draft(), source=CLIENT)
            assert list((store_config.root / layout.TMP_DIR).iterdir()) == []

    class WhenAStageWasLeftBehind:
        def it_is_removed_by_the_sweep(self, store: FileStore, store_config: StoreConfig) -> None:
            leftover = store_config.root / layout.TMP_DIR / "abandoned"
            leftover.mkdir()
            assert store.sweep_staging() == 1
            assert not leftover.exists()

    class WhenAVersionIsNotReferenced:
        def it_is_removed_by_the_sweep(self, store: FileStore, store_config: StoreConfig) -> None:
            artifact_id = _create(store)
            orphan = layout.version_dir(store_config.root, artifact_id, 9)
            orphan.mkdir()
            (orphan / ENTRY).write_bytes(b"partial")
            assert store.sweep_staging() == 1
            assert not orphan.exists()

    class WhenTheSweepRunsBesideAWriterWithAGrace:
        def it_spares_a_fresh_stage(self, store: FileStore, store_config: StoreConfig) -> None:
            fresh = store_config.root / layout.TMP_DIR / "in-flight"
            fresh.mkdir()
            store.sweep_staging(grace=timedelta(minutes=10))
            assert fresh.exists()

        def it_spares_a_placed_version_whose_meta_has_not_landed(
            self, store: FileStore, store_config: StoreConfig
        ) -> None:
            artifact_id = _create(store)
            placed = layout.version_dir(store_config.root, artifact_id, 2)
            placed.mkdir()
            store.sweep_staging(grace=timedelta(minutes=10))
            assert placed.exists()

        def it_removes_a_stage_older_than_the_grace(
            self, store: FileStore, store_config: StoreConfig
        ) -> None:
            stale = store_config.root / layout.TMP_DIR / "abandoned"
            stale.mkdir()
            an_hour_ago = time.time() - 3600
            os.utime(stale, (an_hour_ago, an_hour_ago))
            store.sweep_staging(grace=timedelta(minutes=10))
            assert not stale.exists()

    class WhenMetaJsonCannotBeRead:
        def it_never_deletes_a_version_it_cannot_account_for(
            self, store: FileStore, store_config: StoreConfig
        ) -> None:
            artifact_id = _create(store)
            layout.meta_file(store_config.root, artifact_id).write_text("{")
            orphan = layout.version_dir(store_config.root, artifact_id, 9)
            orphan.mkdir()
            store.sweep_staging()
            assert orphan.exists()


class DescribeTheMetaFile:
    class WhenAnArtifactIsCreated:
        def it_holds_the_documented_keys(self, store: FileStore, store_config: StoreConfig) -> None:
            artifact_id = _create(store)
            written = json.loads(layout.meta_file(store_config.root, artifact_id).read_text())
            assert written["id"] == artifact_id
            assert written["schema_version"] == SCHEMA_VERSION

    class WhenAMetaFileIsCorrupt:
        def it_refuses_to_read_it(self, store: FileStore, store_config: StoreConfig) -> None:
            artifact_id = _create(store)
            layout.meta_file(store_config.root, artifact_id).write_text("{")
            with pytest.raises(SchemaMismatch):
                store.get(artifact_id)

    class WhenTheSchemaVersionIsFromTheFuture:
        def it_refuses_to_read_it(self, store: FileStore, store_config: StoreConfig) -> None:
            artifact_id = _create(store)
            path = layout.meta_file(store_config.root, artifact_id)
            written = json.loads(path.read_text())
            written["schema_version"] = 99
            path.write_text(json.dumps(written))
            with pytest.raises(SchemaMismatch):
                store.get(artifact_id)


class DescribeTheQuota:
    class WhenSeveralWritersRaceForTheLastHeadroom:
        def it_never_exceeds_the_quota(self, store_config: StoreConfig, clock: Clock) -> None:
            store = FileStore(
                store_config.model_copy(update={"quota": 8, "max_artifact": 8}), clock
            )

            def create(index: int) -> bool:
                try:
                    store.create(new_id(), _draft(files={ENTRY: b"x" * 8}), source=CLIENT)
                except OverQuota:
                    return False
                return True

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(create, range(4)))
            assert store.usage_bytes() <= 8


class DescribeIdempotencyRecords:
    class WhenARecordIsUnreadable:
        def it_reclaims_the_key(self, store: FileStore, store_config: StoreConfig) -> None:
            digest = hashlib.sha256(b"weekly").hexdigest()
            record = store_config.root / layout.REFS_DIR / CLIENT / digest
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text("not json")
            claim = store.claim_ref(
                client=CLIENT, ref="weekly", request_hash="aa", artifact_id=_create(store)
            )
            assert claim.reused is False


def _refuse_write(*args: object, **kwargs: object) -> None:
    """Stand in for a write that never completes."""
    raise OSError("the disk went away")


@pytest.fixture
def walks_under_the_lock(store: FileStore, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Whether the store-wide write lock was held, for every directory walk from now on."""
    held: list[bool] = []
    for name in ("glob", "rglob"):
        original = getattr(Path, name)

        def spy(
            path: Path, *args: object, _original: object = original, **kwargs: object
        ) -> object:
            held.append(store._writes_lock.locked())
            return _original(path, *args, **kwargs)  # type: ignore[operator]

        monkeypatch.setattr(Path, name, spy)
    return held


class DescribeTheStore:
    class WhenFilesAreCounted:
        def it_does_not_glob_under_the_write_lock(
            self, store: FileStore, walks_under_the_lock: list[bool]
        ) -> None:
            artifact_id = _create(store)
            store.add_version(artifact_id, VersionDraft(files={"b.txt": b"b"}, merge=True))
            assert True not in walks_under_the_lock

    class WhenAnotherProcessFreesBytes:
        def it_follows_after_the_next_staging_sweep(
            self, store: FileStore, store_config: StoreConfig, clock: Clock
        ) -> None:
            artifact_id = _create(store, files={ENTRY: b"0123456789"})
            FileStore(store_config, clock).delete(artifact_id)
            store.sweep_staging()
            assert store.usage_bytes() == 0

    class WhenAWriteReplacesACrashOrphan:
        def it_counts_only_the_version_that_landed(
            self, store: FileStore, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            artifact_id = _create(store, files={ENTRY: b"0123456789"})
            with monkeypatch.context() as patched:
                _fail_on_meta_replace(patched)
                with pytest.raises(OSError, match="the disk went away"):
                    store.add_version(artifact_id, VersionDraft(files={ENTRY: b"orphan"}))
            store.add_version(artifact_id, VersionDraft(files={ENTRY: b"123"}))
            assert store.usage_bytes() == 13

    class WhenAWriteLandsDuringTheMeasure:
        def it_keeps_the_running_total(
            self, store: FileStore, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            original = filesystem._measure

            def racing(root: Path) -> int:
                measured = original(root)
                _create(store, files={ENTRY: b"12345"})
                return measured

            monkeypatch.setattr(filesystem, "_measure", racing)
            store.resync_usage()
            assert store.usage_bytes() == 5
