"""``ArtifactService``: every artifact change, in the order design § 6 fixes."""

from __future__ import annotations

import contextlib
import hashlib
import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
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
    Locked,
    NotFound,
    Refused,
    TooLarge,
    VitrineError,
)
from mcp_vitrine.domain.ids import new_id, new_lid, validate_client_ref
from mcp_vitrine.domain.media import content_type, is_asset, is_text
from mcp_vitrine.domain.models import (
    Artifact,
    ArtifactDetail,
    ArtifactDraft,
    ArtifactLink,
    ArtifactMeta,
    ArtifactPage,
    ArtifactQuery,
    Content,
    GateDecision,
    LinkClaims,
    LinkKind,
    LinkRecord,
    MetaChange,
    PublishedArtifact,
    Spooled,
    UploadGrant,
    VersionDraft,
    Visibility,
)
from mcp_vitrine.domain.paths import choose_entry, validate_path
from mcp_vitrine.services import passcodes
from mcp_vitrine.services.archives import UNPACK_ZIP
from mcp_vitrine.services.audit import SWEEP, AuditEvent, AuditEventName, AuditLog, MetaField
from mcp_vitrine.services.gate import GateBinding
from mcp_vitrine.services.links import LinkService
from mcp_vitrine.services.metrics import Metrics, NoMetrics
from mcp_vitrine.services.views import HourViews, ViewLog
from mcp_vitrine.storage.base import PUBLIC_UPDATE, Store

TEXT_READ_LIMIT = 1024 * 1024
SERIES_DAYS = 30
UPLOAD_REUSED = "this upload URL was already used"
SITE_TITLE = "site"
# An id that once held an artifact is never handed to a new one: its old private links (epoch 0)
# would open the new content.
GONE = frozenset({AuditEventName.DELETED, AuditEventName.EXPIRED})
PUBLIC_LINK = (
    "this artifact is public, so its one URL takes no passcode or label: make it private to "
    "share it with a passcode"
)
# The fields each kind of change writes; a compensation puts back only these (see ``_restore``).
VISIBILITY_FIELDS = frozenset({"visibility", "public_until", "link_epoch", "links"})
EPOCH_FIELDS = frozenset({"link_epoch", "links"})
LINK_FIELDS = frozenset({"links"})
META_FIELDS = frozenset(MetaField)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Opened:
    """An artifact a view may serve, and the recorded link it was opened through, if any."""

    artifact: Artifact
    lid: str | None = None


class SweepReport(BaseModel):
    """What one sweep did (spec § 4.3, § 11.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expired: int = 0
    unpublished: int = 0
    links: int = 0
    staging: int = 0
    refs: int = 0
    audit_months: int = 0
    view_months: int = 0


class ArtifactService:
    """The pipeline every mutation follows: validate, write, audit, and undo if the audit fails."""

    def __init__(  # noqa: PLR0913 - keyword-only seams, one per collaborator
        self,
        *,
        store: Store,
        links: LinkService,
        audit: AuditLog,
        clock: Clock,
        metrics: Metrics | None = None,
        views: ViewLog | None = None,
    ) -> None:
        """Wire the seams; nothing is global."""
        self._views = views
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
            rollback=partial(self._undo_version, previous, artifact, draft),
        )
        self._metrics.version_created(client)
        return self._published(self._store.prune(artifact_id))

    def set_meta(self, artifact_id: str, change: MetaChange, *, client: str) -> Artifact:
        """Change the title, the tags or the project, in ``meta.json`` alone (spec § 5.1).

        Nothing that is served changes, so there is no version, no gate and no new link epoch,
        and the existing links keep working. The audit names the fields, never their values.
        """
        self._usable(artifact_id)
        before, after = self._store.set_meta(artifact_id, change)
        changed = tuple(
            field for field in MetaField if getattr(before, field) != getattr(after, field)
        )
        if not changed:
            return after
        self._record(
            AuditEvent(
                ts=self._clock.now(),
                event=AuditEventName.METADATA_CHANGED,
                artifact=artifact_id,
                client=client,
                fields=changed,
            ),
            rollback=partial(self._restore, before, after, META_FIELDS),
        )
        return after

    def get(self, artifact_id: str, *, version: int | None = None) -> ArtifactDetail:
        """Read one version's metadata and file list; an expired artifact answers ``Expired``."""
        artifact = self._usable(artifact_id)
        number = artifact.version if version is None else version
        now = self._clock.now()
        live = tuple(record for record in artifact.links if record.expires_at > now)
        views = None
        if self._views is not None:
            labels = {record.lid: record.label for record in artifact.links}
            views = self._views.totals(artifact.id, labels=labels)
        return ArtifactDetail(
            artifact=artifact,
            version=number,
            files=self._store.files(artifact_id, number),
            links=tuple(record.summary() for record in live),
            views=views,
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

    def link(
        self,
        artifact_id: str,
        *,
        client: str,
        ttl: timedelta | None = None,
        label: str = "",
        passcode: str | bool | None = None,
    ) -> ArtifactLink:
        """Mint a recorded private link, or return the public URL when public (spec § 5.1).

        A recorded link carries a ``lid``, so it can be revoked on its own and its views
        counted. ``passcode=True`` generates one, a string supplies one; it is returned once
        and only its scrypt hash is kept.
        """
        artifact = self._usable(artifact_id)
        if artifact.visibility is Visibility.PUBLIC:
            if label or passcode:
                raise Invalid(PUBLIC_LINK)
            return self.url_for(artifact)
        shown = passcodes.generate() if passcode is True else passcode or None
        if isinstance(passcode, str):
            passcodes.check_supplied(passcode)
        expires_at = self._links.link_expiry(ttl)
        record = LinkRecord(
            lid=new_lid(),
            label=label,
            created_at=self._clock.now(),
            expires_at=expires_at,
            passcode=None if shown is None else passcodes.digest(shown),
        )
        after = self._store.add_link(artifact_id, record, limit=self._links.policy.max_links)
        self._record(
            AuditEvent(
                ts=self._clock.now(),
                event=AuditEventName.LINK_ISSUED,
                artifact=artifact_id,
                client=client,
                lid=record.lid,
                expires_at=expires_at,
                passcode=shown is not None,
            ),
            rollback=partial(self._drop_link, artifact_id, record.lid),
        )
        # Signed at the epoch the record landed under, so the two cannot disagree.
        signed = self._links.view_url(
            artifact_id, epoch=after.link_epoch, expires_at=expires_at, lid=record.lid
        )
        return ArtifactLink(
            url=signed.url,
            expires_at=expires_at,
            lid=record.lid,
            passcode=shown if passcode is True else None,
        )

    def url_for(self, artifact: Artifact, *, ttl: timedelta | None = None) -> ArtifactLink:
        """The URL that opens ``artifact`` as it is now: unrecorded, or the public URL.

        The public URL expires with the public window (``public_until``), or never.
        """
        if artifact.visibility is Visibility.PUBLIC:
            return ArtifactLink(
                url=self._links.public_url(artifact.id), expires_at=artifact.public_until
            )
        link = self._links.view_url(artifact.id, epoch=artifact.link_epoch, ttl=ttl)
        return ArtifactLink(url=link.url, expires_at=link.expires_at)

    def public_window(self, public_for: timedelta | None) -> datetime | None:
        """When a publish asked to last ``public_for`` stops being public, after clamping.

        ``public_ttl_max`` caps every window, so with it set nothing stays public for ever.
        """
        ceiling = self._links.policy.public_ttl_max
        lengths = [length for length in (public_for, ceiling) if length is not None]
        return self._clock.now() + min(lengths) if lengths else None

    def gate_binding(self, artifact_id: str, *, client: str, request: str) -> GateBinding:
        """What ties a gate question about ``request`` to the answer that comes back for it."""
        return GateBinding(self._links, artifact_id, client, request)

    def upload_url(  # noqa: PLR0913 - keyword-only: what the URL is for
        self,
        filename: str | None,
        *,
        client: str,
        artifact_id: str | None = None,
        title: str | None = None,
        ttl: timedelta | None = None,
        unpack: bool = False,
    ) -> UploadGrant:
        """Issue a single-use upload URL; a new artifact's id is fixed now (spec § 7.3).

        With ``unpack``, the body is a zip archive whose files become the version.
        """
        if filename is not None:
            validate_path(filename)
        elif not unpack:
            raise Invalid("name the file the URL takes, e.g. report.pdf")
        if artifact_id is None:
            artifact_id = new_id()
        else:
            self._usable(artifact_id)
        link = self._links.upload_url(artifact_id, client=client, ttl=ttl)
        # The signature fixes the id, the expiry and the nonce; the name and the title are only
        # what the holder of that capability chose to call the upload, so they ride unsigned.
        query = (
            ({"filename": filename} if filename else {})
            | ({"unpack": UNPACK_ZIP} if unpack else {})
            | ({"title": title} if title else {})
        )
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
        self, claims: LinkClaims, filename: str, content: Content, *, title: str | None = None
    ) -> PublishedArtifact:
        """Land one ``PUT /u/{sig}``: create the pre-allocated artifact, or add a version (§ 7.3).

        A version overlays the file on the current ones, so adding an image keeps the page that
        shows it. The nonce is spent in the same write, so a second ``PUT`` is a ``Conflict``.
        """
        validate_path(filename)
        return self._land(claims, {filename: content}, title=title or filename, merge=True)

    def upload_site(
        self,
        claims: LinkClaims,
        files: Mapping[str, Content],
        *,
        title: str | None = None,
        merge: bool = False,
    ) -> PublishedArtifact:
        """Land an unpacked archive through an upload URL: a site replaces the files (§ 7.3)."""
        return self._land(claims, files, title=title or SITE_TITLE, merge=merge)

    def _land(
        self, claims: LinkClaims, files: Mapping[str, Content], *, title: str, merge: bool
    ) -> PublishedArtifact:
        """Create the pre-allocated artifact, or add a version, spending the URL's nonce."""
        if claims.client is None or claims.nonce is None:
            raise NotFound()
        try:
            self._store.get(claims.id)
        except NotFound:
            if any(event.event in GONE for event in self._audit.query(artifact=claims.id)):
                raise NotFound() from None
            draft = ArtifactDraft(title=title, files=files, upload_nonce=claims.nonce)
            try:
                return self._create(claims.id, draft, client=claims.client)
            except Conflict:
                # Another PUT of the same URL created it first.
                raise Conflict(UPLOAD_REUSED) from None
        version = VersionDraft(files=files, merge=merge, upload_nonce=claims.nonce)
        return self.update(claims.id, version, client=claims.client)

    def open_public(self, artifact_id: str, *, version: int | None = None) -> Opened:
        """The artifact behind ``/a/{id}``: a private id is as unknown as a missing one (§ 7.1).

        So is one whose public window ran out, before the sweep has made it private (§ 4.3).
        """
        artifact = self._store.get(artifact_id)
        if not artifact.is_public_at(self._clock.now()):
            raise NotFound()
        return Opened(self._viewable(artifact, version))

    def open_signed(self, signature: str, *, version: int | None = None) -> Opened:
        """The artifact behind ``/s/{sig}``; any bad link answers like a missing artifact (§ 3).

        The signature is a view link or an unlocked one. A view link whose record carries a
        passcode is ``Locked``: the route answers with the unlock form, never the content.
        """
        claims = self._signed_claims(signature)
        artifact = self._store.get(claims.id)
        if not self._links.accepts(claims, artifact):
            raise NotFound()
        self._viewable(artifact, version)
        record = None if claims.lid is None else artifact.link(claims.lid)
        if claims.kind is LinkKind.VIEW and record is not None and record.passcode is not None:
            raise Locked()
        return Opened(artifact, claims.lid)

    def view_claims(self, signature: str) -> LinkClaims:
        """Verify a view link's signature, so the edge can key the unlock buckets by its link."""
        return self._links.verify_view(signature)

    def unlock(self, signature: str, passcode: str) -> str | None:
        """The unlocked signature a right passcode earns for a view link (spec § 7.1).

        ``None`` when the link has no passcode, so the caller sends the viewer to the link
        itself; ``Locked`` for a wrong one. An unlocked signature cannot be unlocked again.
        """
        claims = self._links.verify_view(signature)
        artifact = self._store.get(claims.id)
        if not self._links.accepts(claims, artifact):
            raise NotFound()
        self._viewable(artifact, None)
        record = None if claims.lid is None else artifact.link(claims.lid)
        if record is None or record.passcode is None:
            return None
        if not passcodes.matches(passcode, record.passcode):
            raise Locked()
        return self._links.unlocked(claims, link_expires_at=claims.expires_at)

    def count_view(self, opened: Opened, *, route: str, address: str, user_agent: str) -> None:
        """Count one view of an opened artifact, attributed to its link (spec § 11.4)."""
        if self._views is not None:
            self._views.record(
                opened.artifact.id,
                route=route,
                lid=opened.lid,
                address=address,
                user_agent=user_agent,
            )

    def entry_of(self, artifact: ArtifactMeta, *, version: int | None = None) -> str:
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

    def fallback_for(self, artifact: ArtifactMeta, path: str) -> str | None:
        """The file a missing ``path`` serves instead, or ``None`` (spec § 4.3).

        Never for an asset: a missing script answering with HTML would fail quietly.
        """
        if artifact.fallback is None or is_asset(path):
            return None
        return artifact.fallback

    def locate(self, artifact: ArtifactMeta, path: str, *, version: int | None = None) -> Path:
        """The file a view streams, from the version the artifact was opened at."""
        return self._store.locate(artifact.id, path, version=version or artifact.version)

    def view_series(self, artifact_id: str) -> tuple[HourViews, ...]:
        """One existing artifact's views, hour by hour, over ``SERIES_DAYS`` (spec § 11.4)."""
        artifact = self._store.get(artifact_id)
        if self._views is None:
            return ()
        since = self._clock.now() - timedelta(days=SERIES_DAYS)
        return self._views.series(artifact.id, since=since)

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

    def set_visibility(  # noqa: PLR0913 - keyword-only: the gate's verdict and what it approved
        self,
        artifact_id: str,
        visibility: Visibility,
        *,
        client: str,
        gate: GateDecision,
        seen_version: int | None = None,
        public_until: datetime | None = None,
    ) -> Artifact:
        """Apply the gate's decision; this is the only path that can widen access (spec § 6).

        ``seen_version`` is the version the human approved: a later one is a ``Conflict``.
        Going public sets ``public_until``; asking a public artifact to be public again moves
        it, which needs the gate like any publish (spec § 4.3).
        """
        artifact = self._store.get(artifact_id)
        if artifact.visibility is visibility and (
            visibility is Visibility.PRIVATE or artifact.public_until == public_until
        ):
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
            public_until=public_until if visibility is Visibility.PUBLIC else None,
        )
        self._record(
            AuditEvent(
                ts=self._clock.now(),
                event=AuditEventName.VISIBILITY_CHANGED,
                artifact=artifact.id,
                client=client,
                gate=_audited_gate(visibility, gate),
                from_visibility=artifact.visibility,
                to_visibility=visibility,
            ),
            rollback=partial(self._restore, artifact, updated, VISIBILITY_FIELDS),
        )
        return updated

    def revoke_links(self, artifact_id: str, *, client: str, lid: str | None = None) -> Artifact:
        """Revoke one recorded link (``lid``), or every private link by incrementing the epoch."""
        artifact = self._store.get(artifact_id)
        if lid is not None:
            updated = self._store.remove_link(artifact_id, lid)
            fields = LINK_FIELDS
        else:
            updated = self._store.revoke_links(artifact_id)
            fields = EPOCH_FIELDS
        self._record(
            AuditEvent(
                ts=self._clock.now(),
                event=AuditEventName.LINKS_REVOKED,
                artifact=artifact.id,
                client=client,
                link_epoch=updated.link_epoch if lid is None else None,
                lid=lid,
            ),
            rollback=partial(self._restore, artifact, updated, fields),
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
            unpublished=self._unpublish_lapsed(),
            links=self._store.sweep_links(),
            staging=self._store.sweep_staging(grace=grace),
            refs=self._store.sweep_refs(),
            audit_months=self._audit.sweep(),
            view_months=0 if self._views is None else self._views.sweep(),
        )

    def _unpublish_lapsed(self) -> int:
        """Make every artifact whose public window ran out private again (spec § 4.3).

        ``/a/`` already answers 404 for them; this ends the private links issued while public
        too, and records who did it: the sweep, under ``gate = scheduled``.
        """
        unpublished = 0
        for artifact_id in self._store.lapsed_ids():
            try:
                self.set_visibility(
                    artifact_id, Visibility.PRIVATE, client=SWEEP, gate=GateDecision.SCHEDULED
                )
            except NotFound:
                # Deleted since the scan.
                continue
            unpublished += 1
        return unpublished

    def _signed_claims(self, signature: str) -> LinkClaims:
        """The claims of a view or an unlocked signature; ``NotFound`` for anything else."""
        try:
            return self._links.verify_view(signature)
        except NotFound:
            return self._links.verify_unlocked(signature)

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

    def _record(self, event: AuditEvent, *, rollback: Callable[[], object]) -> None:
        """Audit a change that already landed, undoing it when the log cannot be written."""
        try:
            self._audit.append(event)
        except AuditUnavailable:
            _roll_back(rollback)
            raise

    def _undo_version(self, previous: Artifact, after: Artifact, draft: VersionDraft) -> None:
        """Put an artifact back the way it was before a version was added, if nothing moved."""
        if after.version != previous.version + 1:
            logger.warning("not undoing %s v%d: another version landed", after.id, after.version)
            return
        if self._restore(previous, after, _version_fields(draft)):
            self._store.vitrine_version(previous.id, after.version)

    def _drop_link(self, artifact_id: str, lid: str) -> None:
        """Remove a record whose ``link_issued`` could not be written; its URL was never seen."""
        self._store.remove_link(artifact_id, lid)

    def _restore(self, before: Artifact, after: Artifact, fields: Collection[str]) -> bool:
        """Undo one change, and only that change, whose audit write failed (spec § 11.3).

        ``before`` may have been read before the store took its lock, so a change that landed in
        between would be undone with it: that is refused when ``before`` and ``after`` differ
        outside the fields this kind of change writes. Then the store puts ``before`` back only
        if ``meta.json`` is still ``after``, so a later change, audited on its own, survives.
        """
        moved = [
            name
            for name in type(after).model_fields
            if name not in fields and name != "updated_at"
            if getattr(before, name) != getattr(after, name)
        ]
        if moved:
            logger.warning("not undoing a change to %s: %s moved meanwhile", after.id, moved)
            return False
        restored = self._store.replace_meta(before, if_current=after)
        if not restored:
            logger.warning("not undoing a change to %s: it changed again since", after.id)
        return restored


def _audited_gate(visibility: Visibility, gate: GateDecision) -> GateDecision:
    """The gate ``visibility_changed`` records: the verdict, or why it went private."""
    if visibility is Visibility.PUBLIC or gate is GateDecision.SCHEDULED:
        return gate
    return GateDecision.NOT_REQUIRED


def _version_fields(draft: VersionDraft) -> frozenset[str]:
    """The fields an update writes: the version's own, and the metadata the draft names."""
    named = {
        "title": draft.title is not None,
        "tags": draft.tags is not None,
        "project": draft.project is not None,
        "expires_at": draft.expires_at is not None or draft.clear_expiry,
    }
    return frozenset(
        {"version", "versions", "entry", "fallback", "used_upload_nonces"}
        | {name for name, written in named.items() if written}
    )


def request_hash(draft: ArtifactDraft) -> str:
    """Hash what makes a create the same request: the title, the entry and the files (§ 4.5)."""
    digest = hashlib.sha256()
    digest.update(draft.title.encode())
    digest.update(b"|")
    digest.update((draft.entry or "").encode())
    digest.update(b"|")
    digest.update((draft.fallback or "").encode())
    for path in sorted(draft.files):
        digest.update(b"|")
        digest.update(path.encode())
        digest.update(b":")
        digest.update(_content_digest(draft.files[path]).encode())
    return digest.hexdigest()


def _content_digest(content: Content) -> str:
    """The SHA-256 of one file, read in blocks when it is already on disk."""
    if not isinstance(content, Spooled):
        return hashlib.sha256(content).hexdigest()
    with content.path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _roll_back(rollback: Callable[[], object]) -> None:
    """Undo a change, keeping the audit failure as the error the caller sees."""
    with contextlib.suppress(VitrineError):
        rollback()
