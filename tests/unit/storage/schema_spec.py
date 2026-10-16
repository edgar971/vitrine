"""``meta.json`` schema versions: the startup refusal and forward migrations (operations § 4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_vitrine.domain import layout
from mcp_vitrine.domain.clock import Clock
from mcp_vitrine.domain.errors import SchemaMismatch
from mcp_vitrine.domain.ids import new_id
from mcp_vitrine.domain.models import SCHEMA_VERSION, ArtifactDraft
from mcp_vitrine.storage import schema
from mcp_vitrine.storage.base import StoreConfig
from mcp_vitrine.storage.filesystem import FileStore

OLD = SCHEMA_VERSION - 1


def _rename_label_to_title(meta: schema.Meta) -> schema.Meta:
    """A made-up step from the fixture's old schema: ``label`` became ``title``."""
    meta["title"] = meta.pop("label")
    return meta


# The fixture schema: version OLD called the title "label".
FIXTURE_PLAN = schema.Plan(steps={OLD: _rename_label_to_title}, target=SCHEMA_VERSION)


@pytest.fixture
def store(store_config: StoreConfig, clock: Clock) -> FileStore:
    """A store holding nothing yet."""
    return FileStore(store_config, clock)


@pytest.fixture
def root(store_config: StoreConfig) -> Path:
    """The data_dir."""
    return store_config.root


@pytest.fixture
def current(store: FileStore) -> str:
    """An artifact written by this build."""
    artifact_id = new_id()
    store.create(artifact_id, ArtifactDraft(title="Now", files={"a.txt": b"a"}), source="hermes")
    return artifact_id


def _rewrite(root: Path, artifact_id: str, **changes: object) -> None:
    """Edit one ``meta.json`` by hand, the way an older or newer build would have written it."""
    path = layout.meta_file(root, artifact_id)
    meta = json.loads(path.read_text())
    meta.update(changes)
    path.write_text(json.dumps(meta))


@pytest.fixture
def old(store: FileStore, root: Path) -> str:
    """An artifact in the fixture's old schema."""
    artifact_id = new_id()
    store.create(artifact_id, ArtifactDraft(title="Then", files={"a.txt": b"a"}), source="hermes")
    path = layout.meta_file(root, artifact_id)
    meta = json.loads(path.read_text())
    meta["label"] = meta.pop("title")
    meta["schema_version"] = OLD
    path.write_text(json.dumps(meta))
    return artifact_id


class DescribeRequireCurrent:
    class WhenEveryArtifactIsCurrent:
        def it_lets_the_server_start(self, root: Path, current: str) -> None:
            schema.require_current(root)

    class WhenAnArtifactIsOlder:
        def it_asks_for_a_migration(self, root: Path, old: str) -> None:
            with pytest.raises(SchemaMismatch, match="mcp-vitrine migrate"):
                schema.require_current(root)

    class WhenAnArtifactIsNewer:
        def it_asks_for_the_pre_upgrade_backup(self, root: Path, current: str) -> None:
            _rewrite(root, current, schema_version=SCHEMA_VERSION + 1)
            with pytest.raises(SchemaMismatch, match="restore the pre-upgrade backup"):
                schema.require_current(root)

    class WhenMetaJsonIsUnreadable:
        def it_refuses_to_guess(self, root: Path, current: str) -> None:
            layout.meta_file(root, current).write_text("{")
            with pytest.raises(SchemaMismatch, match="unreadable"):
                schema.require_current(root)

    class WhenMetaJsonHasNoSchemaVersion:
        def it_refuses_to_guess(self, root: Path, current: str) -> None:
            _rewrite(root, current, schema_version=None)
            with pytest.raises(SchemaMismatch, match="no schema_version"):
                schema.require_current(root)


class DescribeMigrate:
    class WhenItIsADryRun:
        def it_reports_the_older_artifacts(self, root: Path, old: str, current: str) -> None:
            assert schema.migrate(root, dry_run=True, plan=FIXTURE_PLAN) == (old,)

        def it_changes_no_file(self, root: Path, old: str) -> None:
            before = layout.meta_file(root, old).read_bytes()
            schema.migrate(root, dry_run=True, plan=FIXTURE_PLAN)
            assert layout.meta_file(root, old).read_bytes() == before

    class WhenItRuns:
        def it_leaves_data_this_build_reads(self, root: Path, old: str, store: FileStore) -> None:
            schema.migrate(root, plan=FIXTURE_PLAN)
            assert store.get(old).title == "Then"

        def it_leaves_no_staging_behind(self, root: Path, old: str) -> None:
            schema.migrate(root, plan=FIXTURE_PLAN)
            assert list((root / layout.TMP_DIR).iterdir()) == []

    class WhenItRunsAgain:
        def it_has_nothing_left_to_do(self, root: Path, old: str) -> None:
            schema.migrate(root, plan=FIXTURE_PLAN)
            assert schema.migrate(root, plan=FIXTURE_PLAN) == ()

    class WhenNoStepLeadsFromAVersion:
        def it_refuses(self, root: Path, old: str) -> None:
            with pytest.raises(SchemaMismatch, match=f"no migration from schema_version {OLD}"):
                schema.migrate(root, plan=schema.CURRENT)

    class WhenTheDataIsNewer:
        def it_refuses_to_go_backwards(self, root: Path, current: str) -> None:
            _rewrite(root, current, schema_version=SCHEMA_VERSION + 1)
            with pytest.raises(SchemaMismatch, match="only go forward"):
                schema.migrate(root)
