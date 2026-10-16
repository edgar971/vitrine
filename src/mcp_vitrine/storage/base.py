"""The ``Store`` seam: one protocol, one filesystem implementation (``docs/design.md`` § 5)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field

from mcp_vitrine.domain.models import (
    Artifact,
    ArtifactDraft,
    ArtifactPage,
    ArtifactQuery,
    FileEntry,
    VersionDraft,
    Visibility,
)

if TYPE_CHECKING:
    from mcp_vitrine.config import Settings

# Every store words these refusals the same way, so a caller sees one message whichever store ran.
PUBLIC_UPDATE = (
    "this artifact is public, so new content would be public at once: make it private first, "
    "or update it with artifact_update and a token that may publish, which asks the user"
)
CHANGED = "the artifact changed while waiting for confirmation; ask again"


class StoreConfig(BaseModel):
    """What a ``Store`` needs from the configuration (spec § 8.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root: Path
    retain_versions: int = Field(default=10, ge=1)
    quota: int = Field(default=10 * 1024**3, gt=0)
    max_artifact: int = Field(default=100 * 1024**2, gt=0)
    max_files: int = Field(default=200, ge=1)
    ref_ttl: timedelta = timedelta(hours=24)

    @classmethod
    def from_settings(cls, settings: Settings) -> StoreConfig:
        """Take the storage keys from the effective configuration."""
        return cls(
            root=settings.data_dir,
            retain_versions=settings.retain_versions,
            quota=settings.quota,
            max_artifact=settings.max_artifact,
            max_files=settings.max_files,
            ref_ttl=settings.ref_ttl,
        )


class RefClaim(BaseModel):
    """The outcome of claiming a ``client_ref`` (spec § 4.5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    request_hash: str
    reused: bool


class Store(Protocol):
    """Where artifacts live. Every implementation runs ``tests/contract/store_spec.py``."""

    def create(self, artifact_id: str, draft: ArtifactDraft, *, source: str) -> Artifact:
        """Write version 1 and its ``meta.json``; ``Conflict`` if the id is taken (spec § 4.3).

        A draft's ``upload_nonce`` is recorded as spent in the same write.
        """
        ...

    def add_version(
        self,
        artifact_id: str,
        draft: VersionDraft,
        *,
        if_version: int | None = None,
        if_private: bool = False,
    ) -> Artifact:
        """Write the next version and replace ``meta.json``; ``NotFound`` for an unknown id.

        ``files=None`` carries the current files over, so a metadata change is still a version.
        A draft's ``upload_nonce`` that was already spent is a ``Conflict``; otherwise it is
        recorded as spent in the same write, so one upload URL lands at most once (spec § 7.3).
        The preconditions are checked under the same lock as the write: ``if_version`` is a
        ``Conflict`` when another version landed first, and ``if_private`` is ``Refused`` when the
        artifact is public, since its new content would be public at once (security T19).
        """
        ...

    def prune(self, artifact_id: str) -> Artifact:
        """Drop versions beyond ``retain_versions``, from ``meta.json`` first (spec § 4.3)."""
        ...

    def get(self, artifact_id: str) -> Artifact:
        """Read one ``meta.json``; ``NotFound`` for a malformed or unknown id."""
        ...

    def files(self, artifact_id: str, version: int | None = None) -> tuple[FileEntry, ...]:
        """List a version's files; ``NotFound`` if that artifact or version is gone."""
        ...

    def read(self, artifact_id: str, path: str, *, version: int | None = None) -> bytes:
        """Read one file's bytes, applying the path rule when resolving (spec § 4.3)."""
        ...

    def locate(self, artifact_id: str, path: str, *, version: int | None = None) -> Path:
        """The local file behind one path, so a view can stream it with ``Range`` (spec § 7.2).

        Same rules as ``read``. A store that is not on local disk returns a spooled copy.
        """
        ...

    def list(self, query: ArtifactQuery) -> ArtifactPage:
        """Scan the ``meta.json`` files, newest first, one page at a time (spec § 5.1)."""
        ...

    def release_ref(self, *, client: str, ref: str, artifact_id: str) -> None:
        """Free a ``client_ref`` claimed for a create that failed, if it still names that id."""
        ...

    def delete(self, artifact_id: str) -> None:
        """Remove the artifact and free its ``client_ref`` (spec § 4.5)."""
        ...

    def set_visibility(
        self, artifact_id: str, visibility: Visibility, *, if_version: int | None = None
    ) -> Artifact:
        """Persist a visibility change the gate has already decided (spec § 6).

        ``if_version`` is the version the human was shown; a later one is a ``Conflict``, so
        content added while they decided is never published under their approval (security T20).
        """
        ...

    def revoke_links(self, artifact_id: str) -> Artifact:
        """Increment ``link_epoch``, revoking every private link (spec § 2)."""
        ...

    def replace_meta(self, artifact: Artifact) -> None:
        """Write ``meta.json`` as given, to compensate a mutation whose audit write failed."""
        ...

    def vitrine_version(self, artifact_id: str, version: int) -> None:
        """Remove one version directory, leaving ``meta.json`` alone (compensation only)."""
        ...

    def claim_ref(self, *, client: str, ref: str, request_hash: str, artifact_id: str) -> RefClaim:
        """Claim ``client_ref`` exclusively, or report the artifact an earlier call made (§ 4.5)."""
        ...

    def usage_bytes(self) -> int:
        """Bytes on disk under ``artifacts/``, hard links counted once (spec § 8.2)."""
        ...

    def count(self) -> int:
        """How many artifacts exist, expired ones the sweep has not reached included."""
        ...

    def expired_ids(self) -> tuple[str, ...]:
        """Ids past ``expires_at``; ``ArtifactService`` deletes and audits them (spec § 4.3)."""
        ...

    def sweep_staging(self, *, grace: timedelta = timedelta(0)) -> int:
        """Remove ``tmp/`` and every version directory no ``meta.json`` references."""
        ...

    def sweep_refs(self) -> int:
        """Remove idempotency records past ``ref_ttl`` or pointing at nothing (§ 4.5)."""
