"""``ArtifactService``: the pipeline, the audit and the fail-closed compensations."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mcp_drop.domain.clock import Clock
from mcp_drop.domain.errors import (
    AuditUnavailable,
    Conflict,
    Expired,
    Invalid,
    NotFound,
    Refused,
    StorageUnavailable,
)
from mcp_drop.domain.ids import new_id
from mcp_drop.domain.models import (
    ArtifactDraft,
    ArtifactQuery,
    GateDecision,
    VersionDraft,
    Visibility,
)
from mcp_drop.services.artifacts import ArtifactService, SweepReport, request_hash
from mcp_drop.services.audit import AuditEvent, AuditEventName, AuditLog
from mcp_drop.services.links import LinkPolicy, LinkService
from mcp_drop.storage.base import StoreConfig
from mcp_drop.storage.filesystem import FileStore

BASE = "https://drop.example.com"
SECRET = b"0123456789abcdef0123456789abcdef"
CLIENT = "hermes"
ENTRY = "index.html"


def _draft(**changes: object) -> ArtifactDraft:
    """A one-file draft, with ``changes`` applied."""
    fields: dict[str, object] = {"title": "Energy, September", "files": {ENTRY: b"<h1>hi</h1>"}}
    fields.update(changes)
    return ArtifactDraft.model_validate(fields)


class FailingAudit(AuditLog):
    """An audit log whose writes fail, for the fail-closed specs."""

    def append(self, event: AuditEvent) -> None:
        """Refuse every event, the way a full disk would."""
        raise AuditUnavailable("the disk went away")


@pytest.fixture
def store(store_config: StoreConfig, clock: Clock) -> FileStore:
    """The filesystem store the service writes through."""
    return FileStore(store_config, clock)


@pytest.fixture
def audit(tmp_path: Path, clock: Clock) -> AuditLog:
    """A real audit log, so the specs read back what the service recorded."""
    return AuditLog(tmp_path, retention=timedelta(days=365), clock=clock)


@pytest.fixture
def links(clock: Clock) -> LinkService:
    """A signer with a known secret."""
    return LinkService(LinkPolicy(base_url=BASE), SECRET, clock)


@pytest.fixture
def service(store: FileStore, links: LinkService, audit: AuditLog, clock: Clock) -> ArtifactService:
    """The service under spec."""
    return ArtifactService(store=store, links=links, audit=audit, clock=clock)


@pytest.fixture
def retaining(
    store_config: StoreConfig, clock: Clock, links: LinkService, audit: AuditLog
) -> ArtifactService:
    """A service whose store keeps two versions, so retention is reachable."""
    return ArtifactService(
        store=FileStore(store_config.model_copy(update={"retain_versions": 2}), clock),
        links=links,
        audit=audit,
        clock=clock,
    )


@pytest.fixture
def broken(tmp_path: Path, store: FileStore, links: LinkService, clock: Clock) -> ArtifactService:
    """The same service, with an audit log that refuses every write."""
    return ArtifactService(
        store=store,
        links=links,
        audit=FailingAudit(tmp_path, retention=timedelta(days=365), clock=clock),
        clock=clock,
    )


def _events(audit: AuditLog, artifact: str) -> tuple[AuditEvent, ...]:
    """The events recorded for one artifact, newest first."""
    return audit.query(artifact=artifact)


class DescribeCreate:
    class WhenTheDraftIsValid:
        def it_creates_a_private_artifact(self, service: ArtifactService) -> None:
            published = service.create(_draft(), client=CLIENT)
            assert service.get(published.id).artifact.visibility is Visibility.PRIVATE

        def it_starts_at_version_one(self, service: ArtifactService) -> None:
            assert service.create(_draft(), client=CLIENT).version == 1

        def it_answers_with_a_fresh_private_link(self, service: ArtifactService) -> None:
            published = service.create(_draft(), client=CLIENT)
            assert published.url.startswith(f"{BASE}/s/")

        def it_marks_the_artifact_as_created(self, service: ArtifactService) -> None:
            assert service.create(_draft(), client=CLIENT).created is True

        def it_records_the_client_as_the_source(self, service: ArtifactService) -> None:
            published = service.create(_draft(), client=CLIENT)
            assert service.get(published.id).artifact.source == CLIENT

        def it_writes_exactly_one_created_event(
            self, service: ArtifactService, audit: AuditLog
        ) -> None:
            published = service.create(_draft(files={ENTRY: b"<h1>hi</h1>"}), client=CLIENT)
            events = _events(audit, published.id)
            assert [event.event for event in events] == [AuditEventName.CREATED]

        def it_counts_the_files_and_the_bytes(
            self, service: ArtifactService, audit: AuditLog
        ) -> None:
            published = service.create(_draft(files={ENTRY: b"12345"}), client=CLIENT)
            event = _events(audit, published.id)[0]
            assert (event.files, event.bytes) == (1, 5)

        def it_keeps_the_content_out_of_the_log(
            self, service: ArtifactService, audit: AuditLog
        ) -> None:
            service.create(_draft(title="Secret plans"), client=CLIENT)
            assert "Secret plans" not in audit._root.joinpath("2026-10.jsonl").read_text()

    class WhenTheDraftHasNoFiles:
        def it_refuses_the_call(self, service: ArtifactService) -> None:
            with pytest.raises(Invalid):
                service.create(_draft(files={}), client=CLIENT)

        def it_records_nothing(self, service: ArtifactService, audit: AuditLog) -> None:
            with pytest.raises(Invalid):
                service.create(_draft(files={}), client=CLIENT)
            assert audit.query() == ()

    class WhenAPathEscapesTheVersion:
        def it_refuses_the_call(self, service: ArtifactService) -> None:
            with pytest.raises(Invalid):
                service.create(_draft(files={"../escape.txt": b"x"}), client=CLIENT)

        def it_records_nothing(self, service: ArtifactService, audit: AuditLog) -> None:
            with pytest.raises(Invalid):
                service.create(_draft(files={"../escape.txt": b"x"}), client=CLIENT)
            assert audit.query() == ()

    class WhenTheSameRequestIsRetried:
        def it_returns_the_first_artifact(self, service: ArtifactService) -> None:
            first = service.create(_draft(client_ref="weekly"), client=CLIENT)
            again = service.create(_draft(client_ref="weekly"), client=CLIENT)
            assert (again.id, again.created) == (first.id, False)

        def it_writes_one_event_only(self, service: ArtifactService, audit: AuditLog) -> None:
            service.create(_draft(client_ref="weekly"), client=CLIENT)
            service.create(_draft(client_ref="weekly"), client=CLIENT)
            assert len(audit.query()) == 1

    class WhenTheSameRefCarriesADifferentRequest:
        def it_conflicts(self, service: ArtifactService) -> None:
            service.create(_draft(client_ref="weekly"), client=CLIENT)
            with pytest.raises(Conflict):
                service.create(_draft(title="Other", client_ref="weekly"), client=CLIENT)

    class WhenTheClientRefIsMalformed:
        def it_refuses_the_call(self, service: ArtifactService) -> None:
            with pytest.raises(Invalid):
                service.create(_draft(client_ref="a b"), client=CLIENT)

    class WhenTheRetriedRefNamesAnExpiredArtifact:
        def it_answers_expired(
            self,
            service: ArtifactService,
            clock: Clock,
            advance: Callable[[timedelta], None],
        ) -> None:
            expires_at = clock.now() + timedelta(minutes=1)
            service.create(_draft(client_ref="weekly", expires_at=expires_at), client=CLIENT)
            advance(timedelta(minutes=2))
            with pytest.raises(Expired):
                service.create(_draft(client_ref="weekly", expires_at=expires_at), client=CLIENT)


class DescribeUpdate:
    class WhenFilesAreGiven:
        def it_writes_the_next_version(self, service: ArtifactService) -> None:
            published = service.create(_draft(), client=CLIENT)
            updated = service.update(
                published.id, VersionDraft(files={"report.html": b"2"}), client=CLIENT
            )
            assert (updated.id, updated.version) == (published.id, 2)

        def it_records_a_version_added_event(
            self, service: ArtifactService, audit: AuditLog
        ) -> None:
            published = service.create(_draft(), client=CLIENT)
            service.update(published.id, VersionDraft(files={"report.html": b"2"}), client=CLIENT)
            assert _events(audit, published.id)[0].event is AuditEventName.VERSION_ADDED

        def it_records_whether_the_files_merged(
            self, service: ArtifactService, audit: AuditLog
        ) -> None:
            published = service.create(_draft(), client=CLIENT)
            service.update(
                published.id, VersionDraft(files={"report.html": b"2"}, merge=True), client=CLIENT
            )
            assert _events(audit, published.id)[0].merge is True

    class WhenTheUpdateMerges:
        def it_keeps_the_previous_files(self, service: ArtifactService) -> None:
            published = service.create(_draft(), client=CLIENT)
            updated = service.update(
                published.id, VersionDraft(files={"report.html": b"2"}, merge=True), client=CLIENT
            )
            detail = service.get(updated.id)
            assert [entry.path for entry in detail.files] == [ENTRY, "report.html"]

    class WhenTheArtifactHasExpired:
        def it_refuses_the_update(
            self, service: ArtifactService, clock: Clock, advance: Callable[[timedelta], None]
        ) -> None:
            published = service.create(
                _draft(expires_at=clock.now() + timedelta(minutes=1)), client=CLIENT
            )
            advance(timedelta(minutes=2))
            with pytest.raises(Expired):
                service.update(published.id, VersionDraft(files={"a.html": b"x"}), client=CLIENT)


class DescribeGet:
    class WhenTheArtifactExists:
        def it_returns_the_metadata_and_the_files(self, service: ArtifactService) -> None:
            published = service.create(_draft(), client=CLIENT)
            detail = service.get(published.id)
            assert (detail.version, [entry.path for entry in detail.files]) == (1, [ENTRY])

    class WhenAVersionIsPinned:
        def it_returns_that_version(self, service: ArtifactService) -> None:
            published = service.create(_draft(), client=CLIENT)
            service.update(published.id, VersionDraft(files={"report.html": b"2"}), client=CLIENT)
            detail = service.get(published.id, version=1)
            assert [entry.path for entry in detail.files] == [ENTRY]

    class WhenTheExpiryHasPassed:
        def it_reports_the_artifact_as_expired(
            self, service: ArtifactService, clock: Clock, advance: Callable[[timedelta], None]
        ) -> None:
            published = service.create(
                _draft(expires_at=clock.now() + timedelta(minutes=1)), client=CLIENT
            )
            advance(timedelta(minutes=2))
            with pytest.raises(Expired):
                service.get(published.id)

    class WhenTheArtifactIsUnknown:
        def it_is_not_found(self, service: ArtifactService) -> None:
            with pytest.raises(NotFound):
                service.get(new_id())


class DescribeList:
    class WhenItIsCalled:
        def it_returns_the_page_the_store_built(self, service: ArtifactService) -> None:
            service.create(_draft(), client=CLIENT)
            assert len(service.list(ArtifactQuery()).items) == 1

        def it_filters_as_asked(self, service: ArtifactService) -> None:
            service.create(_draft(), client=CLIENT)
            assert service.list(ArtifactQuery(source="ci-bot")).items == ()

        def it_records_nothing(self, service: ArtifactService, audit: AuditLog) -> None:
            service.list(ArtifactQuery())
            assert audit.query() == ()


class DescribeDelete:
    class WhenTheArtifactExists:
        def it_removes_it(self, service: ArtifactService) -> None:
            published = service.create(_draft(), client=CLIENT)
            service.delete(published.id, client=CLIENT)
            with pytest.raises(NotFound):
                service.get(published.id)

        def it_records_the_deletion(self, service: ArtifactService, audit: AuditLog) -> None:
            published = service.create(_draft(), client=CLIENT)
            service.delete(published.id, client=CLIENT)
            assert _events(audit, published.id)[0].event is AuditEventName.DELETED

    class WhenTheAuditFails:
        def it_keeps_the_artifact(self, broken: ArtifactService, store: FileStore) -> None:
            artifact_id = new_id()
            store.create(artifact_id, _draft(), source=CLIENT)
            with pytest.raises(AuditUnavailable):
                broken.delete(artifact_id, client=CLIENT)
            assert store.get(artifact_id).version == 1


class DescribeSetVisibility:
    class WhenTheGateApproved:
        def it_goes_public(self, service: ArtifactService) -> None:
            published = service.create(_draft(), client=CLIENT)
            changed = service.set_visibility(
                published.id, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.APPROVED
            )
            assert changed.visibility is Visibility.PUBLIC

        def it_moves_the_link_epoch(self, service: ArtifactService) -> None:
            published = service.create(_draft(), client=CLIENT)
            changed = service.set_visibility(
                published.id, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.APPROVED
            )
            assert changed.link_epoch == 1

        def it_records_the_change_and_the_gate(
            self, service: ArtifactService, audit: AuditLog
        ) -> None:
            published = service.create(_draft(), client=CLIENT)
            service.set_visibility(
                published.id, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.APPROVED
            )
            event = _events(audit, published.id)[0]
            assert (event.event, event.gate, event.to_visibility) == (
                AuditEventName.VISIBILITY_CHANGED,
                GateDecision.APPROVED,
                Visibility.PUBLIC,
            )

    class WhenItGoesBackToPrivate:
        def it_records_that_the_gate_was_not_needed(
            self, service: ArtifactService, audit: AuditLog
        ) -> None:
            published = service.create(_draft(), client=CLIENT)
            service.set_visibility(
                published.id, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.APPROVED
            )
            service.set_visibility(
                published.id, Visibility.PRIVATE, client=CLIENT, gate=GateDecision.DECLINED
            )
            assert _events(audit, published.id)[0].gate is GateDecision.NOT_REQUIRED

        def it_still_revokes_every_private_link(self, service: ArtifactService) -> None:
            published = service.create(_draft(), client=CLIENT)
            service.set_visibility(
                published.id, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.APPROVED
            )
            changed = service.set_visibility(
                published.id, Visibility.PRIVATE, client=CLIENT, gate=GateDecision.DECLINED
            )
            assert changed.link_epoch == 2

    class WhenTheGateDeclined:
        def it_refuses_the_call(self, service: ArtifactService) -> None:
            published = service.create(_draft(), client=CLIENT)
            with pytest.raises(Refused):
                service.set_visibility(
                    published.id, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.DECLINED
                )

        def it_leaves_the_artifact_private(self, service: ArtifactService) -> None:
            published = service.create(_draft(), client=CLIENT)
            with pytest.raises(Refused):
                service.set_visibility(
                    published.id, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.DECLINED
                )
            assert service.get(published.id).artifact.visibility is Visibility.PRIVATE

        def it_records_the_refusal(self, service: ArtifactService, audit: AuditLog) -> None:
            published = service.create(_draft(), client=CLIENT)
            with pytest.raises(Refused):
                service.set_visibility(
                    published.id, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.DECLINED
                )
            assert _events(audit, published.id)[0].event is AuditEventName.PUBLISH_REFUSED

    class WhenTheVisibilityIsAlreadyTheOneAskedFor:
        def it_writes_no_event(self, service: ArtifactService, audit: AuditLog) -> None:
            published = service.create(_draft(), client=CLIENT)
            service.set_visibility(
                published.id, Visibility.PRIVATE, client=CLIENT, gate=GateDecision.NOT_REQUIRED
            )
            assert len(_events(audit, published.id)) == 1


class DescribeRevokeLinks:
    class WhenLinksAreRevoked:
        def it_moves_the_epoch(self, service: ArtifactService) -> None:
            published = service.create(_draft(), client=CLIENT)
            assert service.revoke_links(published.id, client=CLIENT).link_epoch == 1

        def it_records_the_new_epoch(self, service: ArtifactService, audit: AuditLog) -> None:
            published = service.create(_draft(), client=CLIENT)
            service.revoke_links(published.id, client=CLIENT)
            event = _events(audit, published.id)[0]
            assert (event.event, event.link_epoch) == (AuditEventName.LINKS_REVOKED, 1)


class DescribeSweep:
    class WhenAnArtifactHasExpired:
        def it_deletes_it(
            self, service: ArtifactService, clock: Clock, advance: Callable[[timedelta], None]
        ) -> None:
            published = service.create(
                _draft(expires_at=clock.now() + timedelta(minutes=1)), client=CLIENT
            )
            advance(timedelta(minutes=2))
            assert service.sweep().expired == 1
            with pytest.raises(NotFound):
                service.get(published.id)

        def it_records_the_expiry(
            self,
            service: ArtifactService,
            audit: AuditLog,
            clock: Clock,
            advance: Callable[[timedelta], None],
        ) -> None:
            published = service.create(
                _draft(expires_at=clock.now() + timedelta(minutes=1)), client=CLIENT
            )
            advance(timedelta(minutes=2))
            service.sweep()
            assert _events(audit, published.id)[0].event is AuditEventName.EXPIRED

        def it_leaves_live_artifacts_alone(self, service: ArtifactService) -> None:
            service.create(_draft(), client=CLIENT)
            assert service.sweep() == SweepReport()

    class WhenTheSweepHasOtherWork:
        def it_clears_the_staging_directory(
            self, service: ArtifactService, store_config: StoreConfig
        ) -> None:
            leftover = store_config.root / "tmp" / "abandoned"
            leftover.mkdir()
            assert service.sweep().staging == 1

        def it_drops_an_old_audit_month(self, service: ArtifactService, tmp_path: Path) -> None:
            old = AuditEvent(
                ts=datetime(2025, 1, 5, 9, 0, tzinfo=UTC),
                event=AuditEventName.CREATED,
                artifact="k7m2q9x4tz3wbn6r",
                client=CLIENT,
                version=1,
                files=1,
                bytes=1,
            )
            (tmp_path / "audit" / "2025-01.jsonl").write_text(old.model_dump_json() + "\n")
            assert service.sweep().audit_months == 1


class DescribeAuditFailures:
    class WhenACreateCannotBeAudited:
        def it_leaves_no_artifact(self, broken: ArtifactService, store: FileStore) -> None:
            with pytest.raises(AuditUnavailable):
                broken.create(_draft(), client=CLIENT)
            assert store.list(ArtifactQuery()).items == ()

    class WhenAnUpdateCannotBeAudited:
        def it_restores_the_previous_version(
            self, broken: ArtifactService, store: FileStore
        ) -> None:
            artifact_id = new_id()
            store.create(artifact_id, _draft(), source=CLIENT)
            with pytest.raises(AuditUnavailable):
                broken.update(artifact_id, VersionDraft(files={"report.html": b"2"}), client=CLIENT)
            assert store.get(artifact_id).version == 1

        def it_hides_the_file_the_failed_update_wrote(
            self, broken: ArtifactService, store: FileStore
        ) -> None:
            artifact_id = new_id()
            store.create(artifact_id, _draft(), source=CLIENT)
            with pytest.raises(AuditUnavailable):
                broken.update(artifact_id, VersionDraft(files={"report.html": b"2"}), client=CLIENT)
            with pytest.raises(NotFound):
                store.read(artifact_id, "report.html")

    class WhenAVisibilityChangeCannotBeAudited:
        def it_leaves_the_artifact_private(self, broken: ArtifactService, store: FileStore) -> None:
            artifact_id = new_id()
            store.create(artifact_id, _draft(), source=CLIENT)
            with pytest.raises(AuditUnavailable):
                broken.set_visibility(
                    artifact_id, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.APPROVED
                )
            assert store.get(artifact_id).visibility is Visibility.PRIVATE

    class WhenARevocationCannotBeAudited:
        def it_keeps_the_epoch(self, broken: ArtifactService, store: FileStore) -> None:
            artifact_id = new_id()
            store.create(artifact_id, _draft(), source=CLIENT)
            with pytest.raises(AuditUnavailable):
                broken.revoke_links(artifact_id, client=CLIENT)
            assert store.get(artifact_id).link_epoch == 0


class DescribeRacedCreates:
    class WhenTheSameClientRefIsRaced:
        def it_produces_one_artifact(
            self, service: ArtifactService, store: FileStore, audit: AuditLog
        ) -> None:
            draft = _draft(client_ref="weekly")
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: _create_swallowing_refusal(service, draft), range(4)))
            assert len(store.list(ArtifactQuery()).items) == 1
            assert len(audit.query()) == 1


def _create_swallowing_refusal(service: ArtifactService, draft: ArtifactDraft) -> object:
    """One racing create; a caller that arrives before the artifact lands is refused."""
    try:
        return service.create(draft, client=CLIENT)
    except NotFound:
        return None


class DescribeRetention:
    class WhenMoreVersionsThanTheRetentionAreWritten:
        def it_keeps_only_retain_versions(self, retaining: ArtifactService) -> None:
            published = retaining.create(_draft(), client=CLIENT)
            for _ in range(2):
                retaining.update(
                    published.id, VersionDraft(files={"report.html": b"2"}), client=CLIENT
                )
            assert len(retaining.get(published.id).artifact.versions) == 2

        def it_removes_the_dropped_version_directory(self, retaining: ArtifactService) -> None:
            published = retaining.create(_draft(), client=CLIENT)
            for _ in range(2):
                retaining.update(
                    published.id, VersionDraft(files={"report.html": b"2"}), client=CLIENT
                )
            with pytest.raises(NotFound):
                retaining.get(published.id, version=1)


class DescribeAFailingDeletion:
    class WhenTheDiskRefusesIt:
        def it_answers_with_a_typed_error(
            self, service: ArtifactService, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            published = service.create(_draft(), client=CLIENT)

            def refuse(*args: object, **kwargs: object) -> None:
                raise OSError(28, "No space left on device")

            monkeypatch.setattr(shutil, "rmtree", refuse)
            with pytest.raises(StorageUnavailable):
                service.delete(published.id, client=CLIENT)

    class WhenASweepRecordsAnExpiryItCannotFinish:
        def it_does_not_record_the_same_expiry_twice(
            self,
            service: ArtifactService,
            audit: AuditLog,
            clock: Clock,
            advance: Callable[[timedelta], None],
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            published = service.create(
                _draft(expires_at=clock.now() + timedelta(minutes=1)), client=CLIENT
            )
            advance(timedelta(minutes=2))

            def refuse(*args: object, **kwargs: object) -> None:
                raise OSError(28, "No space left on device")

            with monkeypatch.context() as patched:
                patched.setattr(shutil, "rmtree", refuse)
                with pytest.raises(StorageUnavailable):
                    service.sweep()
            service.sweep()
            expired = [event for event in audit.query() if event.event is AuditEventName.EXPIRED]
            assert [event.artifact for event in expired] == [published.id]
            with pytest.raises(NotFound):
                service.get(published.id)


class DescribeTheRequestHash:
    class WhenTheRequestIsTheSame:
        def it_matches(self) -> None:
            assert request_hash(_draft()) == request_hash(_draft())

    class WhenTheTitleChanges:
        def it_differs(self) -> None:
            assert request_hash(_draft()) != request_hash(_draft(title="Other"))

    class WhenTheEntryChanges:
        def it_differs(self) -> None:
            assert request_hash(_draft()) != request_hash(_draft(entry=ENTRY))

    class WhenAFileChanges:
        def it_differs(self) -> None:
            assert request_hash(_draft()) != request_hash(_draft(files={ENTRY: b"other"}))

    class WhenAFilePathChanges:
        def it_differs(self) -> None:
            assert request_hash(_draft()) != request_hash(
                _draft(files={"other.html": b"<h1>hi</h1>"})
            )
