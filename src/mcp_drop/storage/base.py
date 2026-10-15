"""The ``Store`` seam: one protocol, one filesystem implementation (``docs/design.md`` § 5)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field

from mcp_drop.domain.models import (
    Artifact,
    ArtifactDraft,
    ArtifactPage,
    ArtifactQuery,
    FileEntry,
    VersionDraft,
    Visibility,
)

if TYPE_CHECKING:
    from mcp_drop.config import Settings


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
        """Write version 1 and its ``meta.json``; ``Conflict`` if the id is taken (spec § 4.3)."""
        ...

    def add_version(self, artifact_id: str, draft: VersionDraft) -> Artifact:
        """Write the next version and replace ``meta.json``; ``NotFound`` for an unknown id."""
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

    def list(self, query: ArtifactQuery) -> ArtifactPage:
        """Scan the ``meta.json`` files, newest first, one page at a time (spec § 5.1)."""
        ...

    def delete(self, artifact_id: str) -> None:
        """Remove the artifact and free its ``client_ref`` (spec § 4.5)."""
        ...

    def set_visibility(self, artifact_id: str, visibility: Visibility) -> Artifact:
        """Persist a visibility change the gate has already decided (spec § 6)."""
        ...

    def revoke_links(self, artifact_id: str) -> Artifact:
        """Increment ``link_epoch``, revoking every private link (spec § 2)."""
        ...

    def replace_meta(self, artifact: Artifact) -> None:
        """Write ``meta.json`` as given, to compensate a mutation whose audit write failed."""
        ...

    def drop_version(self, artifact_id: str, version: int) -> None:
        """Remove one version directory, leaving ``meta.json`` alone (compensation only)."""
        ...

    def claim_ref(self, *, client: str, ref: str, request_hash: str, artifact_id: str) -> RefClaim:
        """Claim ``client_ref`` exclusively, or report the artifact an earlier call made (§ 4.5)."""
        ...

    def usage_bytes(self) -> int:
        """Bytes on disk under ``artifacts/``, hard links counted once (spec § 8.2)."""
        ...

    def expired_ids(self) -> tuple[str, ...]:
        """Ids past ``expires_at``; ``ArtifactService`` deletes and audits them (spec § 4.3)."""
        ...

    def sweep_staging(self) -> int:
        """Remove ``tmp/`` and every version directory no ``meta.json`` references."""
        ...

    def sweep_refs(self) -> int:
        """Remove idempotency records past ``ref_ttl`` or pointing at nothing (§ 4.5)."""
