"""Tool results of spec § 5.1 that no domain model already is; FastMCP derives the schemas."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from mcp_vitrine.domain.models import Visibility


class _Result(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class UpdatedArtifact(_Result):
    """``artifact_update``: the same id, its next version and a fresh private link."""

    id: str
    url: str
    version: int = Field(ge=1)


class ChangedMeta(_Result):
    """``artifact_set_meta``: the metadata as it is now; the version and the links are unchanged."""

    id: str
    title: str
    tags: tuple[str, ...]
    project: str | None


class VisibilityChange(_Result):
    """``artifact_set_visibility``: the visibility now, and the URL that opens it."""

    id: str
    visibility: Visibility
    url: str


class RevokedLinks(_Result):
    """``artifact_revoke_links``: the new link epoch; every older private link is dead."""

    id: str
    link_epoch: int = Field(ge=0)


class UploadUrl(_Result):
    """``artifact_upload_url``: a single-use PUT URL and the command that uses it."""

    id: str
    put_url: str
    expires_at: AwareDatetime
    curl: str


class Deleted(_Result):
    """``artifact_delete``: the id that no longer exists."""

    deleted: str
