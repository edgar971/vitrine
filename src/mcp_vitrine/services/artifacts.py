"""``ArtifactService``: every artifact change, in the order design § 6 fixes."""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict

from mcp_vitrine.domain.clock import Clock
from mcp_vitrine.domain.errors import (
    AuditUnavailable,
    Conflict,
    Expired,
    Invalid,
    NotFound,
    Refused,
    TooLarge,
    VitrineError,
)
from mcp_vitrine.domain.ids import new_id, validate_client_ref
from mcp_vitrine.domain.media import content_type, is_text
from mcp_vitrine.domain.models import (
    Artifact,
    ArtifactDetail,
    ArtifactDraft,
    ArtifactLink,
    ArtifactPage,
    ArtifactQuery,
    GateDecision,
    LinkClaims,
    PublishedArtifact,
    UploadGrant,
    VersionDraft,
    Visibility,
)
from mcp_vitrine.domain.paths import choose_entry, validate_path
from mcp_vitrine.services.audit import AuditEvent, AuditEventName, AuditLog
from mcp_vitrine.services.gate import GateBinding
from mcp_vitrine.services.links import LinkService
from mcp_vitrine.services.metrics import Metrics, NoMetrics
from mcp_vitrine.storage.base import PUBLIC_UPDATE, Store

TEXT_READ_LIMIT = 1024 * 1024
UPLOAD_REUSED = "this upload URL was already used"
# An id that once held an artifact is never handed to a new one: its old private links (epoch 0)
# would open the new content.
GONE = frozenset({AuditEventName.DELETED, AuditEventName.EXPIRED})


class SweepReport(BaseModel):
    """What one sweep did (spec § 4.3, § 11.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expired: int = 0
    staging: int = 0
    refs: int = 0
    audit_months: int = 0


class ArtifactService:
    """The pipeline every mutation follows: validate, write, audit, and undo if the audit fails."""

    def __init__(
        self,
        *,
        store: Store,
        links: LinkService,
        audit: AuditLog,
        clock: Clock,
        metrics: Metrics | None = None,
    ) -> None:
        """Wire the seams; nothing is global."""
        self._metrics = metrics or NoMetrics()
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
            try:
                return self._create(artifact_id, draft, client=client)
            except VitrineError:
                # Nothing was created, so a retry (or a corrected request) must find the key free.
                self._store.release_ref(
                    client=client, ref=draft.client_ref, artifact_id=artifact_id
                )
                raise
        return self._create(artifact_id, draft, client=client)

    def _create(self, artifact_id: str, draft: ArtifactDraft, *, client: str) -> PublishedArtifact:
        """Write version 1 under ``artifact_id`` and audit it, undoing the write if it can't."""
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
        self._metrics.version_created(client)
        return self._published(artifact)

    def update(
        self,
        artifact_id: str,
        draft: VersionDraft,
        *,
        client: str,
        gate: GateDecision | None = None,
        seen_version: int | None = None,
    ) -> PublishedArtifact:
        """Write the next version; the id and its links stay the same (spec § 5.1).

        New content on a public artifact is public at once, so it needs the gate's approval
        (``gate``), given for ``seen_version``. Without one, only a private artifact is updated,
        and the check runs under the store's lock, so a concurrent publish cannot slip between.
        """
        previous = self._usable(artifact_id)
        if gate is not None:
            self._metrics.gate(gate)
            if not gate.approves():
                self._refuse_publish(
                    previous,
                    client=client,
                    gate=gate,
                    reason=f"the public gate answered {gate.value}: no version was written",
                )
        elif previous.visibility is Visibility.PUBLIC:
            self._refuse_publish(previous, client=client, gate=GateDecision.UNSUPPORTED)
        try:
            artifact = self._store.add_version(
                artifact_id,
                draft,
                if_version=None if gate is None else seen_version,
                if_private=gate is None,
            )
        except Refused:
            # It became public between the read above and the write.
            self._refuse_publish(previous, client=client, gate=GateDecision.UNSUPPORTED)
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
        self._metrics.version_created(client)
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

    def gate_binding(self, artifact_id: str, *, client: str, request: str) -> GateBinding:
        """What ties a gate question about ``request`` to the answer that comes back for it."""
        return GateBinding(self._links, artifact_id, client, request)

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
        link = self._links.upload_url(artifact_id, client=client, ttl=ttl)
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

    def upload_claims(self, signature: str) -> LinkClaims:
        """Verify an upload URL's signature, so the edge can spend its client's bucket first."""
        return self._links.verify_upload(signature)

    def upload(
        self, claims: LinkClaims, filename: str, content: bytes, *, title: str | None = None
    ) -> PublishedArtifact:
        """Land one ``PUT /u/{sig}``: create the pre-allocated artifact, or add a version (§ 7.3).

        A version overlays the file on the current ones, so adding an image keeps the page that
        shows it. The nonce is spent in the same write, so a second ``PUT`` is a ``Conflict``.
        """
        validate_path(filename)
        if claims.client is None or claims.nonce is None:
            raise NotFound()
        try:
            self._store.get(claims.id)
        except NotFound:
            if any(event.event in GONE for event in self._audit.query(artifact=claims.id)):
                raise NotFound() from None
            draft = ArtifactDraft(
                title=title or filename, files={filename: content}, upload_nonce=claims.nonce
            )
            try:
                return self._create(claims.id, draft, client=claims.client)
            except Conflict:
                # Another PUT of the same URL created it first.
                raise Conflict(UPLOAD_REUSED) from None
        version = VersionDraft(files={filename: content}, merge=True, upload_nonce=claims.nonce)
        return self.update(claims.id, version, client=claims.client)

    def open_public(self, artifact_id: str, *, version: int | None = None) -> Artifact:
        """The artifact behind ``/a/{id}``: a private id is as unknown as a missing one (§ 7.1)."""
        artifact = self._store.get(artifact_id)
        if artifact.visibility is not Visibility.PUBLIC:
            raise NotFound()
        return self._viewable(artifact, version)

    def open_signed(self, signature: str, *, version: int | None = None) -> Artifact:
        """The artifact behind ``/s/{sig}``; any bad link answers like a missing artifact (§ 3)."""
        claims = self._links.verify_view(signature)
        artifact = self._store.get(claims.id)
        if not self._links.accepts(claims, artifact):
            raise NotFound()
        return self._viewable(artifact, version)

    def entry_of(self, artifact: Artifact, *, version: int | None = None) -> str:
        """The file a link opens; a pinned older version opens its own entry (spec § 4.3).

        Only the current entry is stored, so an older version keeps it while it has that file,
        and otherwise falls back the way a new version would.
        """
        if version is None or version == artifact.version:
            return artifact.entry
        files = [entry.path for entry in self._store.files(artifact.id, version)]
        if artifact.entry in files:
            return artifact.entry
        try:
            return choose_entry(files=files)
        except Invalid:
            raise NotFound() from None

    def locate(self, artifact: Artifact, path: str, *, version: int | None = None) -> Path:
        """The file a view streams, from the version the artifact was opened at."""
        return self._store.locate(artifact.id, path, version=version or artifact.version)

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
        seen_version: int | None = None,
    ) -> Artifact:
        """Apply the gate's decision; this is the only path that can widen access (spec § 6).

        ``seen_version`` is the version the human approved: a later one is a ``Conflict``.
        """
        artifact = self._store.get(artifact_id)
        if artifact.visibility is visibility:
            return artifact
        if visibility is Visibility.PUBLIC:
            self._metrics.gate(gate)
        if visibility is Visibility.PUBLIC and not gate.approves():
            self._refuse_publish(
                artifact,
                client=client,
                gate=gate,
                reason=f"the public gate answered {gate.value}: the artifact stays private",
            )
        updated = self._store.set_visibility(
            artifact_id,
            visibility,
            if_version=seen_version if visibility is Visibility.PUBLIC else None,
        )
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

    def usage(self) -> tuple[int, int]:
        """How many artifacts exist and the bytes their versions hold, for the gauges (§ 11.2)."""
        return self._store.count(), self._store.usage_bytes()

    def sweep(self, *, grace: timedelta = timedelta(0)) -> SweepReport:
        """Delete expired artifacts, then clear staging, stale records and old audit months.

        ``grace`` spares staging younger than it, for a sweep beside a running server.
        """
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
            staging=self._store.sweep_staging(grace=grace),
            refs=self._store.sweep_refs(),
            audit_months=self._audit.sweep(),
        )

    def _viewable(self, artifact: Artifact, version: int | None) -> Artifact:
        """Refuse an expired artifact (410) and a version that is not retained (404)."""
        if artifact.is_expired(self._clock.now()):
            raise Expired()
        if version is not None and version not in {kept.n for kept in artifact.versions}:
            raise NotFound()
        return artifact

    def _usable(self, artifact_id: str) -> Artifact:
        """Read an artifact, refusing one whose expiry has passed (spec § 4.3)."""
        artifact = self._store.get(artifact_id)
        if artifact.is_expired(self._clock.now()):
            raise Expired()
        return artifact

    def _published(self, artifact: Artifact, *, created: bool = True) -> PublishedArtifact:
        """A fresh private link, or the public URL of an artifact the gate made public."""
        link = self.url_for(artifact)
        return PublishedArtifact(
            id=artifact.id,
            url=link.url,
            version=artifact.version,
            expires_at=link.expires_at,
            created=created,
        )

    def _refuse_publish(
        self, artifact: Artifact, *, client: str, gate: GateDecision, reason: str = PUBLIC_UPDATE
    ) -> NoReturn:
        """Audit a refused publish and refuse it; the content stays where it was (spec § 6)."""
        self._audit.append(
            AuditEvent(
                ts=self._clock.now(),
                event=AuditEventName.PUBLISH_REFUSED,
                artifact=artifact.id,
                client=client,
                gate=gate,
            )
        )
        raise Refused(reason)

    def _record(self, event: AuditEvent, *, rollback: Callable[[], None]) -> None:
        """Audit a change that already landed, undoing it when the log cannot be written."""
        try:
            self._audit.append(event)
        except AuditUnavailable:
            _roll_back(rollback)
            raise

    def _undo_version(self, previous: Artifact, version: int) -> None:
        """Put an artifact back the way it was before a version was added."""
        self._store.vitrine_version(previous.id, version)
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
    with contextlib.suppress(VitrineError):
        rollback()
