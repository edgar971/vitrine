"""The domain models: ``meta.json``, the drafts, the links and their claims (spec § 2-4)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from mcp_drop.domain.errors import ConfigError, SchemaMismatch

SCHEMA_VERSION = 1
TAG_LIMIT = 200


class Visibility(StrEnum):
    """Who may read an artifact without a signature (spec § 2)."""

    PRIVATE = "private"
    PUBLIC = "public"


class PublicMode(StrEnum):
    """How ``artifact_set_visibility`` behaves (spec § 6)."""

    CONFIRM = "confirm"
    DISABLED = "disabled"
    UNRESTRICTED = "unrestricted"


class GateDecision(StrEnum):
    """The public gate's verdict, the only thing that can make an artifact public (spec § 6.1)."""

    NOT_REQUIRED = "not_required"
    APPROVED = "approved"
    DECLINED = "declined"
    UNSUPPORTED = "unsupported"
    DISABLED = "disabled"

    def approves(self) -> bool:
        """Whether this verdict lets a publish through."""
        return self in (GateDecision.NOT_REQUIRED, GateDecision.APPROVED)


class Scope(StrEnum):
    """A token capability; a tool needs one to exist for that token at all (spec § 5.0)."""

    READ = "drop:read"
    WRITE = "drop:write"
    SHARE = "drop:share"
    PUBLISH = "drop:publish"

    @classmethod
    def of(cls, names: str) -> frozenset[Scope]:
        """Parse a ``DROP_TOKENS`` scope list (``read+write``); empty means all four."""
        if not names:
            return frozenset(cls)
        scopes: set[Scope] = set()
        for name in names.split("+"):
            try:
                scopes.add(cls(f"drop:{name}"))
            except ValueError:
                raise ConfigError(f"unknown scope {name!r}") from None
        return frozenset(scopes)


class LinkKind(StrEnum):
    """The two signed-link kinds; the kind is inside the MAC (spec § 3)."""

    VIEW = "view"
    UPLOAD = "upload"


class Version(BaseModel):
    """One immutable snapshot of an artifact's files (spec § 2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n: int = Field(ge=1)
    at: AwareDatetime
    note: str = ""
    bytes: int = Field(ge=0)
    files: int = Field(ge=0)


class FileEntry(BaseModel):
    """A file in one version: its relative path and its size in bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    bytes: int = Field(ge=0)


class Artifact(BaseModel):
    """An artifact exactly as ``meta.json`` holds it (spec § 4.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = SCHEMA_VERSION
    id: str
    title: str
    visibility: Visibility = Visibility.PRIVATE
    entry: str
    version: int = Field(ge=1)
    versions: tuple[Version, ...]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    source: str
    client_ref: str | None = None
    tags: tuple[str, ...] = ()
    link_epoch: int = 0
    used_upload_nonces: tuple[str, ...] = ()

    @field_validator("schema_version")
    @classmethod
    def _must_be_known(cls, value: int) -> int:
        """Refuse data this build would misread instead of reinterpreting it (spec § 4.2)."""
        if value != SCHEMA_VERSION:
            raise SchemaMismatch(f"meta.json schema_version {value} is not {SCHEMA_VERSION}")
        return value

    @model_validator(mode="after")
    def _versions_agree_with_the_current_one(self) -> Artifact:
        """Keep ``version`` and ``versions`` from drifting apart; pruning drops the oldest."""
        numbers = tuple(version.n for version in self.versions)
        if numbers != tuple(range(numbers[0], numbers[0] + len(numbers))):
            raise ValueError("versions must be consecutively numbered")
        if numbers[-1] != self.version:
            raise ValueError("version must be the last entry of versions")
        return self

    @field_validator("tags")
    @classmethod
    def _bounded_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Keep a listing row small enough to scan (spec § 8.2)."""
        if len(value) > TAG_LIMIT:
            raise ValueError(f"an artifact holds at most {TAG_LIMIT} tags")
        return value

    def is_expired(self, now: datetime) -> bool:
        """Whether the artifact is past ``expires_at`` (spec § 4.3)."""
        return self.expires_at is not None and now >= self.expires_at

    def summary(self) -> ArtifactSummary:
        """The listing projection: no URLs, no file lists (spec § 5.1)."""
        return ArtifactSummary(
            id=self.id,
            title=self.title,
            visibility=self.visibility,
            version=self.version,
            updated_at=self.updated_at,
            source=self.source,
            tags=self.tags,
        )


class ArtifactSummary(BaseModel):
    """One listing row (spec § 5.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    title: str
    visibility: Visibility
    version: int = Field(ge=1)
    updated_at: AwareDatetime
    source: str
    tags: tuple[str, ...] = ()


class ArtifactDraft(BaseModel):
    """What a caller publishes: the files plus the metadata of a new artifact (spec § 4).

    The shape rules (path, size, count, entry) are enforced where the write happens, so one
    implementation of them serves both text over MCP and multipart over HTTP.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    files: dict[str, bytes]
    entry: str | None = None
    tags: tuple[str, ...] = ()
    note: str = ""
    expires_at: AwareDatetime | None = None
    client_ref: str | None = None


class VersionDraft(BaseModel):
    """What an update changes; ``None`` leaves a field alone (spec § 5.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    files: dict[str, bytes] | None = None
    merge: bool = False
    entry: str | None = None
    title: str | None = None
    tags: tuple[str, ...] | None = None
    note: str = ""
    expires_at: AwareDatetime | None = None
    clear_expiry: bool = False

    @model_validator(mode="after")
    def _expiry_is_not_ambiguous(self) -> VersionDraft:
        """An update either clears the expiry or moves it, never both."""
        if self.clear_expiry and self.expires_at is not None:
            raise ValueError("an update cannot clear and set expires_at at once")
        return self


class ArtifactDetail(BaseModel):
    """``artifact_get``: the metadata plus the file list of one version (spec § 5.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact: Artifact
    version: int = Field(ge=1)
    files: tuple[FileEntry, ...]


class PublishedArtifact(BaseModel):
    """The result of a create or an update, always private (spec § 5.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    url: str
    version: int = Field(ge=1)
    expires_at: AwareDatetime | None = None
    created: bool = True


class ArtifactQuery(BaseModel):
    """The filters and the page ``artifact_list`` accepts (spec § 5.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str | None = None
    tag: str | None = None
    visibility: Visibility | None = None
    source: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = None


class ArtifactPage(BaseModel):
    """One page of listings, newest first (spec § 5.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[ArtifactSummary, ...]
    next_cursor: str | None = None


class LinkClaims(BaseModel):
    """What a verified signature proves (spec § 3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: LinkKind
    id: str
    expires_at: AwareDatetime
    epoch: int | None = None
    nonce: str | None = None

    @model_validator(mode="after")
    def _payload_matches_the_kind(self) -> LinkClaims:
        """A view link carries an epoch, an upload link a nonce, never both."""
        if self.kind is LinkKind.VIEW and (self.epoch is None or self.nonce is not None):
            raise ValueError("a view link carries an epoch and no nonce")
        if self.kind is LinkKind.UPLOAD and (self.nonce is None or self.epoch is not None):
            raise ValueError("an upload link carries a nonce and no epoch")
        return self


class SignedLink(BaseModel):
    """A signed URL and the moment it stops working (spec § 3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    expires_at: AwareDatetime


class ArtifactLink(BaseModel):
    """What ``artifact_link`` hands out: a private link, or the public URL, which never expires."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    expires_at: AwareDatetime | None = None


class UploadGrant(BaseModel):
    """An issued upload URL; the id is pre-allocated when the caller named none (spec § 7.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    put_url: str
    expires_at: AwareDatetime


class UploadLink(BaseModel):
    """A single-use upload URL, with the nonce the store records (spec § 7.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    expires_at: AwareDatetime
    nonce: str
