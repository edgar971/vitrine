"""``AuditLog``: the append-only contract of spec § 11.3."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from mcp_vitrine.domain.clock import Clock
from mcp_vitrine.domain.errors import AuditUnavailable
from mcp_vitrine.domain.models import GateDecision, Visibility
from mcp_vitrine.services.audit import (
    AuditEvent,
    AuditEventName,
    AuditLog,
    DenialReason,
)

NOW = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
ARTIFACT = "k7m2q9x4tz3wbn6r"
CLIENT = "hermes"


def _created(**changes: object) -> AuditEvent:
    """A complete ``created`` event, with ``changes`` applied."""
    fields: dict[str, object] = {
        "ts": NOW,
        "event": AuditEventName.CREATED,
        "artifact": ARTIFACT,
        "client": CLIENT,
        "version": 1,
        "files": 2,
        "bytes": 300,
    }
    fields.update(changes)
    return AuditEvent.model_validate(fields)


def _visibility_changed(**changes: object) -> AuditEvent:
    """A complete ``visibility_changed`` event, with ``changes`` applied."""
    fields: dict[str, object] = {
        "ts": NOW,
        "event": AuditEventName.VISIBILITY_CHANGED,
        "artifact": ARTIFACT,
        "client": CLIENT,
        "gate": GateDecision.APPROVED,
        "from_visibility": Visibility.PRIVATE,
        "to_visibility": Visibility.PUBLIC,
    }
    fields.update(changes)
    return AuditEvent.model_validate(fields)


@pytest.fixture
def audit(tmp_path: Path, clock: Clock) -> AuditLog:
    """A log under the test's temporary ``data_dir``, keeping a year of months."""
    return AuditLog(tmp_path, retention=timedelta(days=365), clock=clock)


class DescribeTheEventModel:
    class WhenItIsInspected:
        def it_holds_only_the_fields_the_spec_documents(self) -> None:
            assert set(AuditEvent.model_fields) == {
                "ts",
                "event",
                "artifact",
                "client",
                "version",
                "files",
                "bytes",
                "client_ref",
                "merge",
                "gate",
                "from_visibility",
                "to_visibility",
                "link_epoch",
                "expires_at",
                "reason",
                "tool",
                "route",
            }

    class WhenAnEventCarriesAFieldTheSpecDoesNotList:
        def it_refuses_it(self) -> None:
            with pytest.raises(ValidationError):
                _created(title="Energy, September")

    class WhenACreatedEventMissesTheCounts:
        def it_refuses_it(self) -> None:
            with pytest.raises(ValidationError):
                AuditEvent(
                    ts=NOW,
                    event=AuditEventName.CREATED,
                    artifact=ARTIFACT,
                    client=CLIENT,
                    version=1,
                )

    class WhenACreatedEventCarriesTheMergeFlag:
        def it_refuses_it(self) -> None:
            with pytest.raises(ValidationError):
                _created(merge=True)

    class WhenACreatedEventNamesItsClientRef:
        def it_accepts_it(self) -> None:
            assert _created(client_ref="weekly").client_ref == "weekly"

    class WhenAPublishRefusalOmitsTheGate:
        def it_refuses_it(self) -> None:
            with pytest.raises(ValidationError):
                AuditEvent(
                    ts=NOW,
                    event=AuditEventName.PUBLISH_REFUSED,
                    artifact=ARTIFACT,
                    client=CLIENT,
                )

    class WhenADeniedCallNamesNeitherToolNorRoute:
        def it_refuses_it(self) -> None:
            with pytest.raises(ValidationError):
                AuditEvent(
                    ts=NOW,
                    event=AuditEventName.ACCESS_DENIED,
                    artifact=ARTIFACT,
                    client=CLIENT,
                    reason=DenialReason.RATE_LIMITED,
                )

    class WhenADeniedCallNamesTheTool:
        def it_accepts_it(self) -> None:
            event = AuditEvent(
                ts=NOW,
                event=AuditEventName.ACCESS_DENIED,
                artifact=ARTIFACT,
                client=CLIENT,
                reason=DenialReason.SCOPE,
                tool="artifact_create",
            )
            assert event.tool == "artifact_create"

    class WhenADeniedCallNamesTheRoute:
        def it_accepts_it(self) -> None:
            event = AuditEvent(
                ts=NOW,
                event=AuditEventName.ACCESS_DENIED,
                client=CLIENT,
                reason=DenialReason.RATE_LIMITED,
                route="api_create",
            )
            assert event.route == "api_create"

    class WhenADeniedCallNamesNoArtifact:
        def it_accepts_it(self) -> None:
            event = AuditEvent(
                ts=NOW,
                event=AuditEventName.ACCESS_DENIED,
                client=CLIENT,
                reason=DenialReason.SCOPE,
                tool="artifact_create",
            )
            assert event.artifact is None

    class WhenAStateChangeNamesNoArtifact:
        def it_refuses_it(self) -> None:
            with pytest.raises(ValidationError):
                AuditEvent(ts=NOW, event=AuditEventName.DELETED, client=CLIENT)

    class WhenADeniedCallNamesBoth:
        def it_refuses_it(self) -> None:
            with pytest.raises(ValidationError):
                AuditEvent(
                    ts=NOW,
                    event=AuditEventName.ACCESS_DENIED,
                    artifact=ARTIFACT,
                    client=CLIENT,
                    reason=DenialReason.SCOPE,
                    tool="artifact_create",
                    route="api_create",
                )

    class WhenADeletionIsRecorded:
        def it_carries_nothing_else(self) -> None:
            event = AuditEvent(
                ts=NOW, event=AuditEventName.DELETED, artifact=ARTIFACT, client=CLIENT
            )
            assert event.version is None


class DescribeAppending:
    class WhenAnEventIsAppended:
        def it_writes_one_line_in_the_months_file(self, audit: AuditLog, tmp_path: Path) -> None:
            audit.append(_created())
            written = (tmp_path / "audit" / "2026-10.jsonl").read_text()
            assert len(written.strip().splitlines()) == 1

        def it_writes_the_event_as_json(self, audit: AuditLog, tmp_path: Path) -> None:
            audit.append(_created())
            written = json.loads((tmp_path / "audit" / "2026-10.jsonl").read_text())
            assert written["event"] == "created"
            assert written["artifact"] == ARTIFACT

        def it_uses_the_field_names_spec_11_3_gives_them(
            self, audit: AuditLog, tmp_path: Path
        ) -> None:
            audit.append(_visibility_changed())
            written = json.loads((tmp_path / "audit" / "2026-10.jsonl").read_text())
            assert (written["from"], written["to"]) == ("private", "public")

        def it_omits_the_fields_the_event_does_not_carry(
            self, audit: AuditLog, tmp_path: Path
        ) -> None:
            audit.append(_created())
            written = json.loads((tmp_path / "audit" / "2026-10.jsonl").read_text())
            assert "merge" not in written
            assert "gate" not in written

        def it_flushes_the_line_to_disk(
            self, audit: AuditLog, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            synced: list[int] = []
            monkeypatch.setattr(os, "fsync", synced.append)
            audit.append(_created())
            assert synced

    class WhenTheLogCannotBeWritten:
        def it_raises_audit_unavailable(
            self, audit: AuditLog, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            def refuse(*args: object, **kwargs: object) -> None:
                raise OSError(28, "No space left on device")

            monkeypatch.setattr(Path, "open", refuse)
            with pytest.raises(AuditUnavailable):
                audit.append(_created())


class DescribeReading:
    class WhenSeveralMonthsExist:
        def it_gathers_them(self, audit: AuditLog) -> None:
            audit.append(_created())
            audit.append(_created(ts=datetime(2026, 11, 1, 9, 0, tzinfo=UTC), version=2))
            assert len(audit.query()) == 2

        def it_returns_the_newest_first(self, audit: AuditLog) -> None:
            audit.append(_created(version=1))
            audit.append(_created(ts=datetime(2026, 10, 15, 11, 0, tzinfo=UTC), version=2))
            assert [event.version for event in audit.query()] == [2, 1]

    class WhenItIsFiltered:
        def it_matches_the_artifact(self, audit: AuditLog) -> None:
            audit.append(_created())
            audit.append(_created(artifact="zzzzzzzzzzzzzzzz"))
            assert len(audit.query(artifact=ARTIFACT)) == 1

        def it_matches_the_client(self, audit: AuditLog) -> None:
            audit.append(_created())
            audit.append(_created(client="ci-bot"))
            assert len(audit.query(client="ci-bot")) == 1

        def it_matches_the_start_of_the_window(self, audit: AuditLog) -> None:
            audit.append(_created())
            audit.append(_created(ts=datetime(2026, 10, 16, 9, 0, tzinfo=UTC), version=2))
            since = datetime(2026, 10, 16, 0, 0, tzinfo=UTC)
            assert [event.version for event in audit.query(since=since)] == [2]

    class WhenALineIsNotAnEvent:
        def it_skips_the_line(self, audit: AuditLog, tmp_path: Path) -> None:
            (tmp_path / "audit" / "2026-10.jsonl").write_text("not an event\n")
            assert audit.query() == ()

        def it_still_reads_the_events_around_it(self, audit: AuditLog, tmp_path: Path) -> None:
            audit.append(_created())
            with (tmp_path / "audit" / "2026-10.jsonl").open("a") as log:
                log.write("not an event\n")
            audit.append(_created())
            assert len(audit.query()) == 2

    class WhenACrashTornTheLastLine:
        def it_starts_the_next_event_on_a_line_of_its_own(
            self, audit: AuditLog, tmp_path: Path
        ) -> None:
            (tmp_path / "audit" / "2026-10.jsonl").write_text('{"ts": "2026-10')
            audit.append(_created())
            assert len(audit.query()) == 1

    class WhenTheFileIsEmpty:
        def it_returns_nothing(self, audit: AuditLog, tmp_path: Path) -> None:
            (tmp_path / "audit" / "2026-10.jsonl").write_text("\n")
            assert audit.query() == ()


class DescribeSweeping:
    class WhenAMonthIsOutsideTheRetentionWindow:
        def it_deletes_the_whole_month(self, audit: AuditLog, tmp_path: Path) -> None:
            old = tmp_path / "audit" / "2025-01.jsonl"
            old.write_text(_created().model_dump_json() + "\n")
            assert audit.sweep() == 1
            assert not old.exists()

    class WhenAMonthIsInsideTheWindow:
        def it_keeps_it(self, audit: AuditLog) -> None:
            audit.append(_created())
            assert audit.sweep() == 0


class DescribeTheRetentionWindow:
    class WhenTheRetentionIsShort:
        def it_uses_it(self, tmp_path: Path, clock: Clock) -> None:
            log = AuditLog(tmp_path, retention=timedelta(days=1), clock=clock)
            (tmp_path / "audit" / "2026-09.jsonl").write_text(_created().model_dump_json() + "\n")
            assert log.sweep() == 1
