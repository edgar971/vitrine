"""``maintenance.migrate``: the audited upgrade of a whole ``data_dir`` (operations § 4)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from mcp_drop.domain import layout
from mcp_drop.domain.clock import Clock
from mcp_drop.domain.errors import AuditUnavailable
from mcp_drop.domain.ids import new_id
from mcp_drop.domain.models import SCHEMA_VERSION, ArtifactDraft
from mcp_drop.services import maintenance
from mcp_drop.services.audit import OPERATOR, AuditEvent, AuditEventName, AuditLog
from mcp_drop.storage import schema
from mcp_drop.storage.base import StoreConfig
from mcp_drop.storage.filesystem import FileStore

OLD = SCHEMA_VERSION - 1
# Version OLD carried an extra key this build no longer has.
PLAN = schema.Plan(
    steps={OLD: lambda meta: {key: value for key, value in meta.items() if key != "legacy"}},
    target=SCHEMA_VERSION,
)


class FailingAudit(AuditLog):
    """An audit log whose writes fail, the way a full disk would."""

    def append(self, event: AuditEvent) -> None:
        """Refuse every event."""
        raise AuditUnavailable("the disk went away")


@pytest.fixture
def root(store_config: StoreConfig) -> Path:
    """The data_dir."""
    return store_config.root


@pytest.fixture
def audit(root: Path, clock: Clock) -> AuditLog:
    """The audit log of the data_dir."""
    return AuditLog(root, retention=timedelta(days=365), clock=clock)


@pytest.fixture
def old(store_config: StoreConfig, clock: Clock) -> str:
    """An artifact in the fixture's old schema."""
    artifact_id = new_id()
    store = FileStore(store_config, clock)
    store.create(artifact_id, ArtifactDraft(title="t", files={"a": b"a"}), source="hermes")
    path = layout.meta_file(store_config.root, artifact_id)
    meta = json.loads(path.read_text()) | {"schema_version": OLD, "legacy": True}
    path.write_text(json.dumps(meta))
    return artifact_id


def _migrated(audit: AuditLog) -> list[AuditEvent]:
    """The ``migrated`` events in the log."""
    return [event for event in audit.query() if event.event is AuditEventName.MIGRATED]


class DescribeMigrate:
    class WhenSomethingIsMigrated:
        def it_audits_the_run_once(
            self, root: Path, audit: AuditLog, clock: Clock, old: str
        ) -> None:
            maintenance.migrate(root, audit=audit, clock=clock, plan=PLAN)
            event = _migrated(audit)[0]
            assert (event.client, event.version, event.files) == (OPERATOR, SCHEMA_VERSION, 1)

    class WhenItIsADryRun:
        def it_audits_nothing(self, root: Path, audit: AuditLog, clock: Clock, old: str) -> None:
            maintenance.migrate(root, audit=audit, clock=clock, dry_run=True, plan=PLAN)
            assert _migrated(audit) == []

    class WhenNothingIsOlder:
        def it_audits_nothing(self, root: Path, audit: AuditLog, clock: Clock) -> None:
            maintenance.migrate(root, audit=audit, clock=clock, plan=PLAN)
            assert _migrated(audit) == []

    class WhenTheRunCannotBeAudited:
        def it_changes_no_file(self, root: Path, clock: Clock, old: str) -> None:
            failing = FailingAudit(root, retention=timedelta(days=365), clock=clock)
            with pytest.raises(AuditUnavailable):
                maintenance.migrate(root, audit=failing, clock=clock, plan=PLAN)
            assert json.loads(layout.meta_file(root, old).read_text())["schema_version"] == OLD
