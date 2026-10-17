"""``ArtifactService``: recorded links, the public window and safe compensation (spec § 3, § 4.3).

Also the #15 rollback fix: a change whose audit failed is undone without undoing anything that
landed beside it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from mcp_vitrine.domain.clock import Clock
from mcp_vitrine.domain.errors import (
    AuditUnavailable,
    Invalid,
    Locked,
    NotFound,
    Refused,
    TooLarge,
)
from mcp_vitrine.domain.models import (
    Artifact,
    ArtifactDraft,
    ArtifactMeta,
    GateDecision,
    PublishedArtifact,
    Visibility,
)
from mcp_vitrine.services.artifacts import ArtifactService
from mcp_vitrine.services.audit import SWEEP, AuditEvent, AuditEventName, AuditLog
from mcp_vitrine.services.links import LinkPolicy, LinkService
from mcp_vitrine.storage.base import StoreConfig
from mcp_vitrine.storage.filesystem import FileStore

BASE = "https://vitrine.example.com"
SECRET = b"0123456789abcdef0123456789abcdef"
CLIENT = "hermes"


class FlakyAudit(AuditLog):
    """An audit log that refuses the events named in ``failing``, the way a full disk would."""

    failing: frozenset[AuditEventName] = frozenset()

    def append(self, event: AuditEvent) -> None:
        """Refuse a failing event; write any other."""
        if event.event in self.failing:
            raise AuditUnavailable("the disk went away")
        super().append(event)


@pytest.fixture
def store(store_config: StoreConfig, clock: Clock) -> FileStore:
    """The filesystem store the service writes through."""
    return FileStore(store_config, clock)


@pytest.fixture
def audit(tmp_path: Path, clock: Clock) -> FlakyAudit:
    """A real audit log, failing nothing until a spec says so."""
    return FlakyAudit(tmp_path, retention=timedelta(days=365), clock=clock)


@pytest.fixture
def policy() -> LinkPolicy:
    """The link settings; a nested class narrows one."""
    return LinkPolicy(base_url=BASE)


@pytest.fixture
def service(store: FileStore, audit: AuditLog, clock: Clock, policy: LinkPolicy) -> ArtifactService:
    """The service under spec."""
    return ArtifactService(
        store=store, links=LinkService(policy, SECRET, clock), audit=audit, clock=clock
    )


@pytest.fixture
def published(service: ArtifactService) -> PublishedArtifact:
    """One private artifact."""
    return service.create(
        ArtifactDraft(title="Energy", files={"index.html": b"<h1>hi</h1>"}), client=CLIENT
    )


def _signature(url: str, marker: str = "/s/") -> str:
    """The signature segment of a URL."""
    return url.split(marker)[1].split("/", maxsplit=1)[0]


def _publish(
    service: ArtifactService, artifact_id: str, *, until: timedelta | None, clock: Clock
) -> Artifact:
    """Make an artifact public through an approving gate, for ``until`` from now."""
    return service.set_visibility(
        artifact_id,
        Visibility.PUBLIC,
        client=CLIENT,
        gate=GateDecision.APPROVED,
        public_until=None if until is None else clock.now() + until,
    )


class DescribeMintingALink:
    class WhenItIsMinted:
        def it_records_the_link(
            self, service: ArtifactService, published: PublishedArtifact
        ) -> None:
            link = service.link(published.id, client=CLIENT, label="sent to finance")
            assert [summary.lid for summary in service.get(published.id).links] == [link.lid]

        def it_audits_link_issued_without_the_label(
            self, service: ArtifactService, published: PublishedArtifact, audit: AuditLog
        ) -> None:
            service.link(published.id, client=CLIENT, label="sent to finance")
            event = audit.query(artifact=published.id)[0]
            assert (event.event, "finance" in event.model_dump_json()) == (
                AuditEventName.LINK_ISSUED,
                False,
            )

    class WhenAPasscodeIsGenerated:
        def it_returns_it_once(
            self, service: ArtifactService, published: PublishedArtifact
        ) -> None:
            assert service.link(published.id, client=CLIENT, passcode=True).passcode

        def it_keeps_it_out_of_the_audit_log(
            self, service: ArtifactService, published: PublishedArtifact, audit: AuditLog
        ) -> None:
            link = service.link(published.id, client=CLIENT, passcode=True)
            assert link.passcode is not None
            assert link.passcode not in audit.query(artifact=published.id)[0].model_dump_json()

        def it_shows_only_that_there_is_one(
            self, service: ArtifactService, published: PublishedArtifact
        ) -> None:
            service.link(published.id, client=CLIENT, passcode=True)
            dumped = service.get(published.id).model_dump_json()
            assert ('"passcode":true' in dumped, '"hash"' in dumped) == (True, False)

    class WhenAPasscodeIsSupplied:
        def it_is_not_returned(
            self, service: ArtifactService, published: PublishedArtifact
        ) -> None:
            link = service.link(published.id, client=CLIENT, passcode="correct horse")
            assert link.passcode is None

        def it_unlocks_the_link(
            self, service: ArtifactService, published: PublishedArtifact
        ) -> None:
            link = service.link(published.id, client=CLIENT, passcode="correct horse")
            assert service.unlock(_signature(link.url), "correct horse") is not None

    class WhenTheWrongPasscodeIsSent:
        def it_is_locked(self, service: ArtifactService, published: PublishedArtifact) -> None:
            link = service.link(published.id, client=CLIENT, passcode="correct horse")
            with pytest.raises(Locked):
                service.unlock(_signature(link.url), "wrong horse")

    class WhenTheArtifactIsPublic:
        def it_refuses_a_passcode(
            self, service: ArtifactService, published: PublishedArtifact, clock: Clock
        ) -> None:
            _publish(service, published.id, until=None, clock=clock)
            with pytest.raises(Invalid):
                service.link(published.id, client=CLIENT, passcode=True)

    class WhenTheArtifactHoldsMaxLinks:
        @pytest.fixture
        def policy(self) -> LinkPolicy:
            return LinkPolicy(base_url=BASE, max_links=2)

        def it_asks_to_revoke_links_first(
            self, service: ArtifactService, published: PublishedArtifact
        ) -> None:
            service.link(published.id, client=CLIENT)
            service.link(published.id, client=CLIENT)
            with pytest.raises(TooLarge, match="revoke links first"):
                service.link(published.id, client=CLIENT)

        def it_does_not_count_expired_links(
            self,
            service: ArtifactService,
            published: PublishedArtifact,
            advance: Callable[[timedelta], None],
        ) -> None:
            service.link(published.id, client=CLIENT, ttl=timedelta(hours=1))
            service.link(published.id, client=CLIENT, ttl=timedelta(hours=1))
            advance(timedelta(hours=2))
            assert service.link(published.id, client=CLIENT).lid

    class WhenTheAuditFails:
        def it_drops_the_record(
            self, service: ArtifactService, published: PublishedArtifact, audit: FlakyAudit
        ) -> None:
            audit.failing = frozenset({AuditEventName.LINK_ISSUED})
            with pytest.raises(AuditUnavailable):
                service.link(published.id, client=CLIENT)
            assert service.get(published.id).links == ()


class DescribeRevokingOneLink:
    class WhenItIsRevoked:
        def it_audits_its_lid(
            self, service: ArtifactService, published: PublishedArtifact, audit: AuditLog
        ) -> None:
            link = service.link(published.id, client=CLIENT)
            assert link.lid is not None
            service.revoke_links(published.id, client=CLIENT, lid=link.lid)
            event = audit.query(artifact=published.id)[0]
            assert (event.event, event.lid, event.link_epoch) == (
                AuditEventName.LINKS_REVOKED,
                link.lid,
                None,
            )

        def it_keeps_the_epoch(
            self, service: ArtifactService, published: PublishedArtifact
        ) -> None:
            link = service.link(published.id, client=CLIENT)
            assert link.lid is not None
            assert service.revoke_links(published.id, client=CLIENT, lid=link.lid).link_epoch == 0

    class WhenTheLidIsUnknown:
        def it_is_not_found(self, service: ArtifactService, published: PublishedArtifact) -> None:
            with pytest.raises(NotFound):
                service.revoke_links(published.id, client=CLIENT, lid="AbCdEfGh_-0")

    class WhenEveryLinkIsRevoked:
        def it_drops_every_record(
            self, service: ArtifactService, published: PublishedArtifact
        ) -> None:
            service.link(published.id, client=CLIENT)
            service.revoke_links(published.id, client=CLIENT)
            assert service.get(published.id).links == ()


class DescribeScheduledUnpublish:
    @pytest.fixture
    def windowed(
        self, service: ArtifactService, published: PublishedArtifact, clock: Clock
    ) -> Artifact:
        """The artifact, public for one day."""
        return _publish(service, published.id, until=timedelta(days=1), clock=clock)

    class WhenPublicUntilHasPassed:
        def it_is_not_found_on_the_public_route_before_the_sweep(
            self,
            service: ArtifactService,
            windowed: Artifact,
            advance: Callable[[timedelta], None],
        ) -> None:
            advance(timedelta(days=1))
            with pytest.raises(NotFound):
                service.open_public(windowed.id)

    class WhenTheSweepRuns:
        @pytest.fixture
        def swept(
            self,
            service: ArtifactService,
            windowed: Artifact,
            advance: Callable[[timedelta], None],
        ) -> ArtifactMeta:
            """The artifact after the sweep that follows its window."""
            advance(timedelta(days=1))
            service.sweep()
            return service.get(windowed.id).artifact

        def it_makes_the_artifact_private(self, swept: ArtifactMeta) -> None:
            assert (swept.visibility, swept.public_until) == (Visibility.PRIVATE, None)

        def it_revokes_private_links_issued_while_public(
            self, service: ArtifactService, windowed: Artifact, swept: ArtifactMeta
        ) -> None:
            assert swept.link_epoch == windowed.link_epoch + 1

        def it_audits_visibility_changed_with_gate_scheduled(
            self, audit: AuditLog, swept: ArtifactMeta
        ) -> None:
            event = audit.query(artifact=swept.id)[0]
            assert (event.event, event.client, event.gate) == (
                AuditEventName.VISIBILITY_CHANGED,
                SWEEP,
                GateDecision.SCHEDULED,
            )

        def it_reports_it(
            self,
            service: ArtifactService,
            windowed: Artifact,
            advance: Callable[[timedelta], None],
        ) -> None:
            advance(timedelta(days=1))
            assert service.sweep().unpublished == 1

    class WhenTheWindowIsStillOpen:
        def it_leaves_the_artifact_public(
            self, service: ArtifactService, windowed: Artifact
        ) -> None:
            service.sweep()
            assert service.get(windowed.id).artifact.visibility is Visibility.PUBLIC

    class WhenTheWindowIsMovedLater:
        def it_keeps_the_private_links(
            self, service: ArtifactService, windowed: Artifact, clock: Clock
        ) -> None:
            moved = _publish(service, windowed.id, until=timedelta(days=7), clock=clock)
            assert moved.link_epoch == windowed.link_epoch

        def it_needs_the_gate(
            self, service: ArtifactService, windowed: Artifact, clock: Clock
        ) -> None:
            with pytest.raises(Refused):
                service.set_visibility(
                    windowed.id,
                    Visibility.PUBLIC,
                    client=CLIENT,
                    gate=GateDecision.DECLINED,
                    public_until=clock.now() + timedelta(days=7),
                )


class DescribePublicTtlMax:
    @pytest.fixture
    def policy(self) -> LinkPolicy:
        return LinkPolicy(base_url=BASE, public_ttl_max=timedelta(days=7))

    class WhenPublicForIsLonger:
        def it_clamps_it(self, service: ArtifactService, clock: Clock) -> None:
            assert service.public_window(timedelta(days=30)) == clock.now() + timedelta(days=7)

    class WhenNoPublicForIsGiven:
        def it_still_ends(self, service: ArtifactService, clock: Clock) -> None:
            assert service.public_window(None) == clock.now() + timedelta(days=7)

    class WhenPublicForIsShorter:
        def it_is_kept(self, service: ArtifactService, clock: Clock) -> None:
            assert service.public_window(timedelta(days=1)) == clock.now() + timedelta(days=1)


class DescribeAFailedAuditBesideAnotherChange:
    class WhenAnotherChangeLandedAfterIt:
        def it_keeps_that_change(
            self,
            service: ArtifactService,
            store: FileStore,
            published: PublishedArtifact,
            audit: FlakyAudit,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            # The epoch revoke lands, then another writer records a link before the audit fails.
            original = store.revoke_links

            def revoke_then_race(artifact_id: str) -> Artifact:
                revoked = original(artifact_id)
                service.link(artifact_id, client=CLIENT)
                return revoked

            monkeypatch.setattr(store, "revoke_links", revoke_then_race)
            audit.failing = frozenset({AuditEventName.LINKS_REVOKED})
            with pytest.raises(AuditUnavailable):
                service.revoke_links(published.id, client=CLIENT)
            assert len(service.get(published.id).links) == 1

    class WhenAnotherChangeLandedBeforeItsLock:
        @pytest.fixture
        def public(self, service: ArtifactService, published: PublishedArtifact) -> str:
            """The published artifact, made public with no end."""
            service.set_visibility(
                published.id, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.APPROVED
            )
            return published.id

        def it_keeps_that_change(
            self,
            service: ArtifactService,
            store: FileStore,
            public: str,
            audit: FlakyAudit,
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            # The sweep ends the public window after the service read the artifact, before the
            # store takes its lock to revoke: undoing the revoke must not make it public again.
            original = store.revoke_links

            def race_then_revoke(artifact_id: str) -> Artifact:
                store.set_visibility(artifact_id, Visibility.PRIVATE)
                return original(artifact_id)

            monkeypatch.setattr(store, "revoke_links", race_then_revoke)
            audit.failing = frozenset({AuditEventName.LINKS_REVOKED})
            with pytest.raises(AuditUnavailable):
                service.revoke_links(public, client=CLIENT)
            assert service.get(public).artifact.visibility is Visibility.PRIVATE

    class WhenNothingElseMoved:
        def it_undoes_the_change(
            self, service: ArtifactService, published: PublishedArtifact, audit: FlakyAudit
        ) -> None:
            audit.failing = frozenset({AuditEventName.LINKS_REVOKED})
            with pytest.raises(AuditUnavailable):
                service.revoke_links(published.id, client=CLIENT)
            assert service.get(published.id).artifact.link_epoch == 0
