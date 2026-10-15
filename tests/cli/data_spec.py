"""``sweep``, ``audit``, ``backup`` and ``migrate``: commands over a data_dir (spec § 9)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mcp_drop.config import Settings
from mcp_drop.domain import layout
from mcp_drop.domain.models import SCHEMA_VERSION, ArtifactDraft
from mcp_drop.services.audit import AuditEvent, AuditEventName
from mcp_drop.services.container import Services
from mcp_drop.storage import schema
from tests.cli.conftest import Runner
from tests.conftest import FrozenClock

OLD = SCHEMA_VERSION - 1


@pytest.fixture
def services(settings: Settings) -> Services:
    """The services over the environment's data_dir, to arrange state."""
    return Services.from_settings(settings)


@pytest.fixture
def artifact_id(services: Services) -> str:
    """One artifact created by hermes."""
    draft = ArtifactDraft(title="Energy", files={"index.html": b"hi"})
    return services.artifacts.create(draft, client="hermes").id


@pytest.fixture
def old_event(services: Services, artifact_id: str) -> None:
    """A ci-bot event from thirty days ago."""
    services.audit.append(
        AuditEvent(
            ts=datetime.now(UTC) - timedelta(days=30),
            event=AuditEventName.LINKS_REVOKED,
            artifact=artifact_id,
            client="ci-bot",
            link_epoch=1,
        )
    )


class DescribeSweep:
    class WhenAnArtifactHasExpired:
        def it_reports_the_expiry(self, run: Runner, environment: None, settings: Settings) -> None:
            # Written by a server whose clock read last year, so it expired long ago.
            last_year = FrozenClock(datetime.now(UTC) - timedelta(days=365))
            draft = ArtifactDraft(
                title="Gone", files={"a": b"a"}, expires_at=last_year.now() + timedelta(days=1)
            )
            Services.from_settings(settings, clock=last_year).artifacts.create(
                draft, client="hermes"
            )
            assert run("sweep").out.startswith("expired 1,")

    class WhenTheDataIsOlderThanThisBuild:
        def it_refuses_to_touch_it(
            self, run: Runner, environment: None, settings: Settings, artifact_id: str
        ) -> None:
            _age(settings, artifact_id)
            assert run("sweep").status == 1


class DescribeAudit:
    class WhenNoFilterIsGiven:
        def it_prints_every_event(
            self, run: Runner, environment: None, artifact_id: str, old_event: None
        ) -> None:
            assert len(run("audit").out.splitlines()) == 2

    class WhenFilteredByClient:
        def it_prints_only_that_clients_events(
            self, run: Runner, environment: None, artifact_id: str, old_event: None
        ) -> None:
            assert "links_revoked" not in run("audit", "--client", "hermes").out

    class WhenFilteredByAge:
        def it_leaves_out_older_events(
            self, run: Runner, environment: None, artifact_id: str, old_event: None
        ) -> None:
            assert "links_revoked" not in run("audit", "--since", "7d").out

    class WhenFilteredById:
        def it_prints_only_that_artifacts_events(
            self, run: Runner, environment: None, artifact_id: str, services: Services
        ) -> None:
            other = services.artifacts.create(
                ArtifactDraft(title="Other", files={"a": b"a"}), client="hermes"
            ).id
            assert other not in run("audit", "--id", artifact_id).out

    class WhenAskedForJson:
        def it_prints_the_log_lines_as_stored(
            self, run: Runner, environment: None, artifact_id: str
        ) -> None:
            line = json.loads(run("audit", "--json").out)
            assert (line["event"], line["artifact"]) == ("created", artifact_id)

    class WhenTheAgeIsNotADuration:
        def it_exits_as_misconfigured(self, run: Runner, environment: None) -> None:
            assert run("audit", "--since", "last week").status == 2


class DescribeBackupCommand:
    class WhenTheDestinationIsNew:
        def it_reports_what_it_copied(
            self, run: Runner, environment: None, artifact_id: str, tmp_path: Path
        ) -> None:
            out = run("backup", str(tmp_path / "copy")).out
            assert out.startswith("artifacts 1, versions 1,")

    class WhenTheDestinationHoldsSomething:
        def it_fails(self, run: Runner, environment: None, tmp_path: Path) -> None:
            (tmp_path / "copy").mkdir()
            (tmp_path / "copy" / "keep").write_text("x")
            assert run("backup", str(tmp_path / "copy")).status == 1


class DescribeMigrateCommand:
    @pytest.fixture
    def plan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A build whose schema moved on from OLD, with the step that gets there."""
        monkeypatch.setattr(
            schema, "CURRENT", schema.Plan(steps={OLD: dict}, target=SCHEMA_VERSION)
        )

    class WhenItIsADryRun:
        def it_lists_the_artifacts_it_would_change(
            self, run: Runner, environment: None, plan: None, settings: Settings, artifact_id: str
        ) -> None:
            _age(settings, artifact_id)
            assert run("migrate", "--dry-run").out.split() == [artifact_id]

        def it_changes_nothing(
            self, run: Runner, environment: None, plan: None, settings: Settings, artifact_id: str
        ) -> None:
            _age(settings, artifact_id)
            run("migrate", "--dry-run")
            assert schema.survey(settings.data_dir).older == (artifact_id,)

    class WhenItRuns:
        def it_leaves_the_data_current(
            self, run: Runner, environment: None, plan: None, settings: Settings, artifact_id: str
        ) -> None:
            _age(settings, artifact_id)
            run("migrate")
            assert schema.survey(settings.data_dir) == schema.Survey()

    class WhenNothingIsOlder:
        def it_reports_nothing_to_do(
            self, run: Runner, environment: None, artifact_id: str
        ) -> None:
            assert "migrated 0 artifact(s)" in run("migrate").err


def _age(settings: Settings, artifact_id: str) -> None:
    """Rewrite one ``meta.json`` as an older build would have left it."""
    path = layout.meta_file(settings.data_dir, artifact_id)
    meta = json.loads(path.read_text()) | {"schema_version": OLD}
    path.write_text(json.dumps(meta))
