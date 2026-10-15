"""``backup``: a restorable copy of a live ``data_dir`` (operations § 2)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from mcp_drop.domain import layout
from mcp_drop.domain.clock import Clock
from mcp_drop.domain.errors import Conflict
from mcp_drop.domain.ids import new_id
from mcp_drop.domain.models import ArtifactDraft, VersionDraft
from mcp_drop.services.audit import AuditEvent, AuditEventName, AuditLog
from mcp_drop.storage.backup import BackupReport, backup
from mcp_drop.storage.base import StoreConfig
from mcp_drop.storage.filesystem import FileStore

CLIENT = "hermes"
WRITERS = 4


@pytest.fixture
def store_config(tmp_path: Path) -> StoreConfig:
    """Keep two versions, so concurrent updates also prune while the backup copies."""
    return StoreConfig(root=tmp_path / "data", retain_versions=2)


@pytest.fixture
def store(store_config: StoreConfig, clock: Clock) -> FileStore:
    """The live store being backed up."""
    return FileStore(store_config, clock)


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    """Where the backup goes."""
    return tmp_path / "backup"


@pytest.fixture
def artifact_id(store: FileStore) -> str:
    """One artifact with two files."""
    artifact_id = new_id()
    draft = ArtifactDraft(title="Energy", files={"index.html": b"<h1>hi</h1>", "a/b.csv": b"x,y"})
    store.create(artifact_id, draft, source=CLIENT)
    return artifact_id


@pytest.fixture
def audited(store_config: StoreConfig, clock: Clock, artifact_id: str) -> AuditLog:
    """An audit log holding one event, plus a torn last line a writer is midway through."""
    log = AuditLog(store_config.root, retention=timedelta(days=365), clock=clock)
    log.append(
        AuditEvent(
            ts=clock.now(),
            event=AuditEventName.DELETED,
            artifact=artifact_id,
            client=CLIENT,
        )
    )
    with layout.audit_file(store_config.root, clock.now()).open("a") as handle:
        handle.write('{"ts":"2026-')
    return log


def _restored(dest: Path, clock: Clock) -> FileStore:
    """The backup opened as a data_dir."""
    return FileStore(StoreConfig(root=dest, retain_versions=2), clock)


def _unreadable_versions(store: FileStore, root: Path) -> list[tuple[str, int]]:
    """Each version a restored ``meta.json`` lists whose files cannot be read back."""
    ids = [path.parent.name for path in (root / layout.ARTIFACTS_DIR).glob("*/meta.json")]
    return [
        (artifact_id, version.n)
        for artifact_id in ids
        for version in store.get(artifact_id).versions
        if not all(
            store.read(artifact_id, entry.path, version=version.n)
            for entry in store.files(artifact_id, version.n)
        )
    ]


class DescribeBackup:
    class WhenTheStoreIsIdle:
        def it_copies_every_file_byte_for_byte(
            self, store_config: StoreConfig, artifact_id: str, dest: Path, clock: Clock
        ) -> None:
            backup(store_config.root, dest)
            assert _restored(dest, clock).read(artifact_id, "a/b.csv") == b"x,y"

        def it_reports_what_it_copied(
            self, store_config: StoreConfig, artifact_id: str, dest: Path
        ) -> None:
            report = backup(store_config.root, dest)
            assert (report.artifacts, report.versions) == (1, 1)

        def it_copies_the_audit_log_up_to_its_last_whole_line(
            self, store_config: StoreConfig, audited: AuditLog, dest: Path, clock: Clock
        ) -> None:
            backup(store_config.root, dest)
            log = AuditLog(dest, retention=timedelta(days=365), clock=clock)
            assert [event.event for event in log.query()] == [AuditEventName.DELETED]

        def it_copies_the_idempotency_records(
            self, store_config: StoreConfig, store: FileStore, dest: Path
        ) -> None:
            store.claim_ref(client=CLIENT, ref="weekly", request_hash="h", artifact_id=new_id())
            backup(store_config.root, dest)
            assert len(list((dest / layout.REFS_DIR / CLIENT).iterdir())) == 1

    class WhenAnArtifactHasNoMetaYet:
        def it_is_left_out(self, store_config: StoreConfig, dest: Path) -> None:
            (store_config.root / layout.ARTIFACTS_DIR / new_id() / "v1").mkdir(parents=True)
            assert backup(store_config.root, dest) == BackupReport()

    class WhenTheDestinationHoldsSomething:
        def it_refuses_to_mix_into_it(self, store_config: StoreConfig, dest: Path) -> None:
            dest.mkdir()
            (dest / "keep.txt").write_text("mine")
            with pytest.raises(Conflict, match="new or empty"):
                backup(store_config.root, dest)

    class WhenWritersAreBusyDuringTheBackup:
        @pytest.fixture
        def busy(self, store: FileStore, artifact_id: str) -> Iterator[None]:
            """Writers adding (and pruning) versions as fast as they can until the backup ends."""
            done = threading.Event()

            def write() -> None:
                while not done.is_set():
                    store.add_version(
                        artifact_id, VersionDraft(files={"index.html": new_id().encode()})
                    )
                    store.prune(artifact_id)
                    store.create(
                        new_id(), ArtifactDraft(title="t", files={"x": b"x"}), source=CLIENT
                    )

            writers = [threading.Thread(target=write) for _ in range(WRITERS)]
            for writer in writers:
                writer.start()
            yield
            done.set()
            for writer in writers:
                writer.join()

        def it_restores_to_a_valid_data_dir(
            self, store_config: StoreConfig, busy: None, dest: Path, clock: Clock
        ) -> None:
            backup(store_config.root, dest)
            assert _unreadable_versions(_restored(dest, clock), dest) == []
