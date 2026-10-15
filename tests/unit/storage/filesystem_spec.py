"""``FileStore`` details the contract suite cannot see: the layout and the crash windows."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mcp_drop.domain import layout
from mcp_drop.domain.clock import Clock
from mcp_drop.domain.errors import NotFound, SchemaMismatch
from mcp_drop.domain.ids import new_id
from mcp_drop.domain.models import ArtifactDraft, VersionDraft
from mcp_drop.storage.base import StoreConfig
from mcp_drop.storage.filesystem import FileStore

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
            assert written["schema_version"] == 1

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
