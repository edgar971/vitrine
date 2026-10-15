"""``ArtifactService``: every artifact change, in the order design § 6 fixes."""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict

from mcp_drop.domain.clock import Clock
from mcp_drop.domain.errors import (
    AuditUnavailable,
    DropError,
    Expired,
    Invalid,
    NotFound,
    Refused,
    TooLarge,
)
from mcp_drop.domain.ids import new_id, validate_client_ref
from mcp_drop.domain.media import content_type, is_text
from mcp_drop.domain.models import (
    Artifact,
    ArtifactDetail,
    ArtifactDraft,
    ArtifactLink,
    ArtifactPage,
    ArtifactQuery,
    GateDecision,
    PublishedArtifact,
    UploadGrant,
    VersionDraft,
    Visibility,
)
from mcp_drop.domain.paths import validate_path
from mcp_drop.services.audit import AuditEvent, AuditEventName, AuditLog
from mcp_drop.services.links import LinkService
from mcp_drop.storage.base import Store

TEXT_READ_LIMIT = 1024 * 1024


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
                return self._published(self._usable(claim.id), created=False)
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

    def read_text(self, artifact_id: str, path: str) -> str:
        """One text file of the current version, up to 1 MB, for reading back (spec § 5.2)."""
        self._usable(artifact_id)
        validate_path(path)
        entry = next((file for file in self._store.files(artifact_id) if file.path == path), None)
        if entry is None:
            raise NotFound()
        kind = content_type(path)
        if not is_text(path):
            raise Invalid(f"{path} is {kind} ({entry.bytes} bytes): only text files read back")
        if entry.bytes > TEXT_READ_LIMIT:
            raise TooLarge(f"{path} is {kind} ({entry.bytes} bytes): text reads back up to 1 MB")
        try:
            return self._store.read(artifact_id, path).decode()
        except UnicodeDecodeError:
            raise Invalid(f"{path} is {kind} ({entry.bytes} bytes) but not UTF-8") from None

    def expiry(self, after: timedelta) -> datetime:
        """The moment that lies ``after`` from now on the service's clock."""
        return self._clock.now() + after

    def link(self, artifact_id: str, *, ttl: timedelta | None = None) -> ArtifactLink:
        """A fresh private link, or the public URL when the artifact is public (spec § 5.1)."""
        return self.url_for(self._usable(artifact_id), ttl=ttl)

    def url_for(self, artifact: Artifact, *, ttl: timedelta | None = None) -> ArtifactLink:
        """The URL that opens ``artifact`` as it is now; a public one carries no expiry."""
        if artifact.visibility is Visibility.PUBLIC:
            return ArtifactLink(url=self._links.public_url(artifact.id))
        link = self._links.view_url(artifact.id, epoch=artifact.link_epoch, ttl=ttl)
        return ArtifactLink(url=link.url, expires_at=link.expires_at)

    def upload_url(
        self,
        filename: str,
        *,
        client: str,
        artifact_id: str | None = None,
        title: str | None = None,
        ttl: timedelta | None = None,
    ) -> UploadGrant:
        """Issue a single-use upload URL; a new artifact's id is fixed now (spec § 7.3)."""
        validate_path(filename)
        if artifact_id is None:
            artifact_id = new_id()
        else:
            self._usable(artifact_id)
        link = self._links.upload_url(artifact_id, ttl=ttl)
        # The signature fixes the id, the expiry and the nonce; the name and the title are only
        # what the holder of that capability chose to call the upload, so they ride unsigned.
        query = {"filename": filename} | ({"title": title} if title else {})
        self._audit.append(
            AuditEvent(
                ts=self._clock.now(),
                event=AuditEventName.UPLOAD_URL_ISSUED,
                artifact=artifact_id,
                client=client,
                expires_at=link.expires_at,
            )
        )
        return UploadGrant(
            id=artifact_id, put_url=f"{link.url}?{urlencode(query)}", expires_at=link.expires_at
        )

    def history(self, artifact_id: str) -> tuple[AuditEvent, ...]:
        """The audit events of one existing artifact, newest first (spec § 5.2)."""
        artifact = self._store.get(artifact_id)
        return self._audit.query(artifact=artifact.id)

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
        already_recorded = {
            event.artifact for event in self._audit.query() if event.event is AuditEventName.EXPIRED
        }
        expired = 0
        for artifact_id in self._store.expired_ids():
            if artifact_id in already_recorded:
                # An earlier sweep recorded this expiry but could not finish the deletion.
                self._store.delete(artifact_id)
            else:
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
