"""``ArtifactService``: every artifact change, in the order design § 6 fixes."""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Callable
from functools import partial

from pydantic import BaseModel, ConfigDict

from mcp_drop.domain.clock import Clock
from mcp_drop.domain.errors import AuditUnavailable, DropError, Expired, Invalid, Refused
from mcp_drop.domain.ids import new_id, validate_client_ref
from mcp_drop.domain.models import (
    Artifact,
    ArtifactDetail,
    ArtifactDraft,
    ArtifactPage,
    ArtifactQuery,
    GateDecision,
    PublishedArtifact,
    VersionDraft,
    Visibility,
)
from mcp_drop.services.audit import AuditEvent, AuditEventName, AuditLog
from mcp_drop.services.links import LinkService
from mcp_drop.storage.base import Store


class SweepReport(BaseModel):
    """What one sweep did (spec § 4.3, § 11.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expired: int = 0
    staging: int = 0
    refs: int = 0
    audit_months: int = 0


class ArtifactService:
    """The pipeline every mutation follows: validate, write, audit, and undo if the audit fails."""

    def __init__(self, *, store: Store, links: LinkService, audit: AuditLog, clock: Clock) -> None:
        """Wire the seams; nothing is global."""
        self._store = store
        self._links = links
        self._audit = audit
        self._clock = clock

    def create(self, draft: ArtifactDraft, *, client: str) -> PublishedArtifact:
        """Publish a private artifact; a repeated ``client_ref`` returns the first one (§ 4.5)."""
        if not draft.files:
            raise Invalid("an artifact needs at least one file")
        artifact_id = new_id()
        if draft.client_ref is not None:
            validate_client_ref(draft.client_ref)
            claim = self._store.claim_ref(
                client=client,
                ref=draft.client_ref,
                request_hash=request_hash(draft),
                artifact_id=artifact_id,
            )
            artifact_id = claim.id
            if claim.reused:
                return self._published(self._store.get(claim.id), created=False)
        artifact = self._store.create(artifact_id, draft, source=client)
        self._record(
            AuditEvent(
                ts=self._clock.now(),
                event=AuditEventName.CREATED,
                artifact=artifact.id,
                client=client,
                version=artifact.version,
                files=artifact.versions[-1].files,
                bytes=artifact.versions[-1].bytes,
                client_ref=artifact.client_ref,
            ),
            rollback=partial(self._store.delete, artifact.id),
        )
        return self._published(artifact)

    def update(self, artifact_id: str, draft: VersionDraft, *, client: str) -> PublishedArtifact:
        """Write the next version; the id and its links stay the same (spec § 5.1)."""
        previous = self._usable(artifact_id)
        artifact = self._store.add_version(artifact_id, draft)
        self._record(
            AuditEvent(
                ts=self._clock.now(),
                event=AuditEventName.VERSION_ADDED,
                artifact=artifact.id,
                client=client,
                version=artifact.version,
                files=artifact.versions[-1].files,
                bytes=artifact.versions[-1].bytes,
                merge=draft.merge,
            ),
            rollback=partial(self._undo_version, previous, artifact.version),
        )
        return self._published(self._store.prune(artifact_id))

    def get(self, artifact_id: str, *, version: int | None = None) -> ArtifactDetail:
        """Read one version's metadata and file list; an expired artifact answers ``Expired``."""
        artifact = self._usable(artifact_id)
        number = artifact.version if version is None else version
        return ArtifactDetail(
            artifact=artifact, version=number, files=self._store.files(artifact_id, number)
        )

    def list(self, query: ArtifactQuery) -> ArtifactPage:
        """List artifacts, newest first; reads are not audited (spec § 11.3)."""
        return self._store.list(query)

    def delete(self, artifact_id: str, *, client: str) -> None:
        """Delete from any state; the audit comes first, so a deletion is never unrecorded."""
        artifact = self._store.get(artifact_id)
        self._audit.append(
            AuditEvent(
                ts=self._clock.now(),
                event=AuditEventName.DELETED,
                artifact=artifact.id,
                client=client,
            )
        )
        self._store.delete(artifact_id)

    def set_visibility(
        self,
        artifact_id: str,
        visibility: Visibility,
        *,
        client: str,
        gate: GateDecision,
    ) -> Artifact:
        """Apply the gate's decision; this is the only path that can widen access (spec § 6)."""
        artifact = self._store.get(artifact_id)
        if artifact.visibility is visibility:
            return artifact
        if visibility is Visibility.PUBLIC and not gate.approves():
            self._audit.append(
                AuditEvent(
                    ts=self._clock.now(),
                    event=AuditEventName.PUBLISH_REFUSED,
                    artifact=artifact.id,
                    client=client,
                    gate=gate,
                )
            )
            raise Refused(f"the public gate answered {gate.value}: the artifact stays private")
        updated = self._store.set_visibility(artifact_id, visibility)
        self._record(
            AuditEvent(
                ts=self._clock.now(),
                event=AuditEventName.VISIBILITY_CHANGED,
                artifact=artifact.id,
                client=client,
                gate=gate if visibility is Visibility.PUBLIC else GateDecision.NOT_REQUIRED,
                from_visibility=artifact.visibility,
                to_visibility=visibility,
            ),
            rollback=partial(self._store.replace_meta, artifact),
        )
        return updated

    def revoke_links(self, artifact_id: str, *, client: str) -> Artifact:
        """Increment the link epoch, so every private link issued before stops working."""
        artifact = self._store.get(artifact_id)
        updated = self._store.revoke_links(artifact_id)
        self._record(
            AuditEvent(
                ts=self._clock.now(),
                event=AuditEventName.LINKS_REVOKED,
                artifact=artifact.id,
                client=client,
                link_epoch=updated.link_epoch,
            ),
            rollback=partial(self._store.replace_meta, artifact),
        )
        return updated

    def sweep(self) -> SweepReport:
        """Delete expired artifacts, then clear staging, stale records and old audit months."""
        expired = 0
        for artifact_id in self._store.expired_ids():
            artifact = self._store.get(artifact_id)
            self._audit.append(
                AuditEvent(
                    ts=self._clock.now(),
                    event=AuditEventName.EXPIRED,
                    artifact=artifact.id,
                    client=artifact.source,
                )
            )
            self._store.delete(artifact_id)
            expired += 1
        return SweepReport(
            expired=expired,
            staging=self._store.sweep_staging(),
            refs=self._store.sweep_refs(),
            audit_months=self._audit.sweep(),
        )

    def _usable(self, artifact_id: str) -> Artifact:
        """Read an artifact, refusing one whose expiry has passed (spec § 4.3)."""
        artifact = self._store.get(artifact_id)
        if artifact.is_expired(self._clock.now()):
            raise Expired()
        return artifact

    def _published(self, artifact: Artifact, *, created: bool = True) -> PublishedArtifact:
        """Every result carries a fresh private link; nothing here can make it public."""
        link = self._links.view_url(artifact.id, epoch=artifact.link_epoch)
        return PublishedArtifact(
            id=artifact.id,
            url=link.url,
            version=artifact.version,
            expires_at=link.expires_at,
            created=created,
        )

    def _record(self, event: AuditEvent, *, rollback: Callable[[], None]) -> None:
        """Audit a change that already landed, undoing it when the log cannot be written."""
        try:
            self._audit.append(event)
        except AuditUnavailable:
            _roll_back(rollback)
            raise

    def _undo_version(self, previous: Artifact, version: int) -> None:
        """Put an artifact back the way it was before a version was added."""
        self._store.drop_version(previous.id, version)
        self._store.replace_meta(previous)


def request_hash(draft: ArtifactDraft) -> str:
    """Hash what makes a create the same request: the title, the entry and the files (§ 4.5)."""
    digest = hashlib.sha256()
    digest.update(draft.title.encode())
    digest.update(b"|")
    digest.update((draft.entry or "").encode())
    for path in sorted(draft.files):
        digest.update(b"|")
        digest.update(path.encode())
        digest.update(b":")
        digest.update(hashlib.sha256(draft.files[path]).hexdigest().encode())
    return digest.hexdigest()


def _roll_back(rollback: Callable[[], None]) -> None:
    """Undo a change, keeping the audit failure as the error the caller sees."""
    with contextlib.suppress(DropError):
        rollback()
