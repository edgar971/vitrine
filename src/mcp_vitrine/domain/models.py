"""The domain models: ``meta.json``, the drafts, the links and their claims (spec § 2-4)."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from mcp_vitrine.domain.errors import ConfigError, Invalid, SchemaMismatch

SCHEMA_VERSION = 2
TAG_LIMIT = 200
TAG_MAX = 64
TITLE_MAX = 200
NONCES_KEPT = 50
PROJECT_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,63}$"

type Project = Annotated[str, Field(pattern=PROJECT_PATTERN)]
type Tag = Annotated[str, Field(max_length=TAG_MAX)]


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
    # Not a verdict anyone gives: the sweep ending a public window that ran out (spec § 4.3).
    SCHEDULED = "scheduled"

    def approves(self) -> bool:
        """Whether this verdict lets a publish through."""
        return self in (GateDecision.NOT_REQUIRED, GateDecision.APPROVED)


class Scope(StrEnum):
    """A token capability; a tool needs one to exist for that token at all (spec § 5.0)."""

    READ = "vitrine:read"
    WRITE = "vitrine:write"
    SHARE = "vitrine:share"
    PUBLISH = "vitrine:publish"

    @classmethod
    def of(cls, names: str) -> frozenset[Scope]:
        """Parse a ``VITRINE_TOKENS`` scope list (``read+write``); empty means all four."""
        if not names:
            return frozenset(cls)
        scopes: set[Scope] = set()
        for name in names.split("+"):
            try:
                scopes.add(cls(f"vitrine:{name}"))
            except ValueError:
                raise ConfigError(f"unknown scope {name!r}") from None
        return frozenset(scopes)


class LinkKind(StrEnum):
    """The signed kinds; the kind is inside the MAC, so one can never pass for another (§ 3)."""

    VIEW = "view"
    UPLOAD = "upload"
    # A passcode link whose passcode was given; never accepted where a view is, nor the reverse.
    UNLOCKED = "unlocked"
    # Not a link: the state a gate question carries to its answer (security T20).
    GATE = "gate"


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


class Passcode(BaseModel):
    """The scrypt hash of a link's passcode and its parameters; never the passcode itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hash: str
    salt: str
    n: int = Field(ge=2)
    r: int = Field(ge=1)
    p: int = Field(ge=1)


class LinkRecord(BaseModel):
    """One live private link that ``artifact_link`` minted (spec § 4.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lid: str
    label: str = Field(default="", max_length=TITLE_MAX)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    passcode: Passcode | None = None

    def summary(self) -> LinkSummary:
        """What a reader is shown: whether there is a passcode, never its hash."""
        return LinkSummary(
            lid=self.lid,
            label=self.label,
            created_at=self.created_at,
            expires_at=self.expires_at,
            passcode=self.passcode is not None,
        )


class LinkSummary(BaseModel):
    """One live link as ``artifact_get`` lists it: never the URL, never the hash (spec § 5.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lid: str
    label: str
    created_at: AwareDatetime
    expires_at: AwareDatetime
    passcode: bool


class ArtifactMeta(BaseModel):
    """``meta.json`` as a reader is shown it: every field but the link records (spec § 5.1).

    ``Artifact`` adds those. A field typed ``ArtifactMeta`` that holds an ``Artifact`` serialises
    as this class, so no passcode hash leaves through a result by accident.
    A field whose feature is not enabled keeps its default and has no effect.
    """

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
    fallback: str | None = None
    public_until: AwareDatetime | None = None
    project: Project | None = None

    @field_validator("schema_version")
    @classmethod
    def _must_be_known(cls, value: int) -> int:
        """Refuse data this build would misread instead of reinterpreting it (spec § 4.2)."""
        if value != SCHEMA_VERSION:
            raise SchemaMismatch(f"meta.json schema_version {value} is not {SCHEMA_VERSION}")
        return value

    @model_validator(mode="after")
    def _versions_agree_with_the_current_one(self) -> Self:
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

    def is_public_at(self, now: datetime) -> bool:
        """Whether ``/a/`` serves it now: public, and its public window has not run out (§ 4.3)."""
        return self.visibility is Visibility.PUBLIC and (
            self.public_until is None or now < self.public_until
        )

    def has_lapsed(self, now: datetime) -> bool:
        """Public on disk, but past ``public_until``: the sweep makes it private (spec § 4.3)."""
        return self.visibility is Visibility.PUBLIC and not self.is_public_at(now)

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
            project=self.project,
            public_until=self.public_until,
        )

    def text(self) -> tuple[str, ...]:
        """What a search term may match: title, tags, project, version notes, entry (§ 5.1)."""
        notes = tuple(version.note for version in self.versions if version.note)
        return (
            self.title,
            *self.tags,
            *((self.project,) if self.project else ()),
            *notes,
            self.entry,
        )


class Artifact(ArtifactMeta):
    """An artifact exactly as ``meta.json`` holds it (spec § 4.2), link records included."""

    links: tuple[LinkRecord, ...] = ()

    def link(self, lid: str) -> LinkRecord | None:
        """The live record of one link, if it still exists."""
        return next((record for record in self.links if record.lid == lid), None)


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
    project: str | None = None
    public_until: AwareDatetime | None = None


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
    project: Project | None = None
    note: str = ""
    expires_at: AwareDatetime | None = None
    client_ref: str | None = None
    upload_nonce: str | None = None


class VersionDraft(BaseModel):
    """What an update changes; ``None`` leaves a field alone (spec § 5.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    files: dict[str, bytes] | None = None
    merge: bool = False
    entry: str | None = None
    title: str | None = None
    tags: tuple[str, ...] | None = None
    # ``""`` clears the project; ``None`` leaves it alone.
    project: Project | Literal[""] | None = None
    note: str = ""
    expires_at: AwareDatetime | None = None
    clear_expiry: bool = False
    upload_nonce: str | None = None

    @model_validator(mode="after")
    def _expiry_is_not_ambiguous(self) -> VersionDraft:
        """An update either clears the expiry or moves it, never both."""
        if self.clear_expiry and self.expires_at is not None:
            raise ValueError("an update cannot clear and set expires_at at once")
        return self


class MetaChange(BaseModel):
    """What ``artifact_set_meta`` changes; ``None`` leaves a field alone (spec § 5.1).

    Nothing here is served, so a change needs no version, no gate and no new link epoch.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: Annotated[str, Field(max_length=TITLE_MAX)] | None = None
    tags: tuple[Tag, ...] | None = None
    add_tags: tuple[Tag, ...] = ()
    remove_tags: tuple[Tag, ...] = ()
    # ``""`` clears the project.
    project: Project | Literal[""] | None = None

    @model_validator(mode="after")
    def _says_one_clear_thing(self) -> MetaChange:
        """Name something to change, and either replace the tags or add and remove some.

        ``Invalid`` rather than ``ValueError``: pydantic lets it through, so a tool's caller
        gets the message instead of a masked validation error.
        """
        if self.tags is not None and (self.add_tags or self.remove_tags):
            raise Invalid("tags replaces every tag: do not combine it with add_tags or remove_tags")
        if (self.title, self.tags, self.project) == (None, None, None) and not (
            self.add_tags or self.remove_tags
        ):
            raise Invalid("name something to change: title, tags, add_tags, remove_tags or project")
        return self

    def applied_to(self, artifact: Artifact) -> dict[str, object]:
        """The fields of ``artifact`` this change gives new values, by name; empty if none."""
        tags = artifact.tags if self.tags is None else self.tags
        tags = (*tags, *_new_tags(tags, self.add_tags))
        removed = {tag.casefold() for tag in self.remove_tags}
        tags = tuple(tag for tag in tags if tag.casefold() not in removed)
        if len(tags) > TAG_LIMIT:
            raise Invalid(f"an artifact holds at most {TAG_LIMIT} tags")
        wanted: dict[str, object] = {
            "title": artifact.title if self.title is None else self.title,
            "tags": tags,
            "project": artifact.project if self.project is None else (self.project or None),
        }
        return {name: value for name, value in wanted.items() if getattr(artifact, name) != value}


def _new_tags(tags: tuple[str, ...], added: tuple[str, ...]) -> tuple[str, ...]:
    """The tags of ``added`` not already present, compared as the tag filter compares them."""
    seen = {tag.casefold() for tag in tags}
    fresh: list[str] = []
    for tag in added:
        if tag.casefold() not in seen:
            seen.add(tag.casefold())
            fresh.append(tag)
    return tuple(fresh)


class LinkViews(BaseModel):
    """How often one recorded link was opened (spec § 11.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lid: str
    label: str = ""
    views: int = Field(ge=0)
    first_viewed_at: AwareDatetime | None = None
    last_viewed_at: AwareDatetime | None = None


class Views(BaseModel):
    """An artifact's view counts, to the hour; a visitor is counted once per hour (spec § 11.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int = Field(default=0, ge=0)
    visitors: int = Field(default=0, ge=0)
    bots: int = Field(default=0, ge=0)
    first_viewed_at: AwareDatetime | None = None
    last_viewed_at: AwareDatetime | None = None
    links: tuple[LinkViews, ...] = ()


class ArtifactDetail(BaseModel):
    """``artifact_get``: the metadata, one version's files, the live links and the views (§ 5.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact: ArtifactMeta
    version: int = Field(ge=1)
    files: tuple[FileEntry, ...]
    links: tuple[LinkSummary, ...] = ()
    # ``None`` when ``view_log`` is off.
    views: Views | None = None


class PublishedArtifact(BaseModel):
    """The result of a create or an update, always private (spec § 5.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    url: str
    version: int = Field(ge=1)
    expires_at: AwareDatetime | None = None
    created: bool = True


class ListSort(StrEnum):
    """The orders ``artifact_list`` pages in (spec § 5.1)."""

    UPDATED = "updated"
    CREATED = "created"
    TITLE = "title"


class ArtifactQuery(BaseModel):
    """The filters and the page ``artifact_list`` accepts (spec § 5.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str | None = None
    tag: str | None = None
    project: str | None = None
    visibility: Visibility | None = None
    source: str | None = None
    updated_after: AwareDatetime | None = None
    expiring_within: timedelta | None = None
    sort: ListSort = ListSort.UPDATED
    facets: bool = False
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = None

    @property
    def terms(self) -> tuple[str, ...]:
        """The query's terms, case-folded; every one of them must match (spec § 5.1)."""
        return tuple(term.casefold() for term in (self.query or "").split())


class Facets(BaseModel):
    """How many artifacts of the whole match carry each project and each tag (spec § 5.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    projects: dict[str, int]
    tags: dict[str, int]


class ArtifactPage(BaseModel):
    """One page of listings, in the query's order (spec § 5.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[ArtifactSummary, ...]
    next_cursor: str | None = None
    facets: Facets | None = None


class LinkClaims(BaseModel):
    """What a verified signature proves (spec § 3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: LinkKind
    id: str
    expires_at: AwareDatetime
    epoch: int | None = None
    nonce: str | None = None
    client: str | None = None
    lid: str | None = None

    @model_validator(mode="after")
    def _payload_matches_the_kind(self) -> LinkClaims:
        """Each kind carries exactly its payload (spec § 3).

        A view link carries an epoch and maybe a link id; an unlocked one both; an upload one a
        nonce and the client that asked for it.
        """
        upload_fields = (self.nonce, self.client)
        if self.kind is LinkKind.VIEW and (
            self.epoch is None or any(field is not None for field in upload_fields)
        ):
            raise ValueError("a view link carries an epoch, an optional link id and nothing else")
        if self.kind is LinkKind.UNLOCKED and (
            self.epoch is None or self.lid is None or any(f is not None for f in upload_fields)
        ):
            raise ValueError("an unlocked link carries an epoch and a link id, and nothing else")
        if self.kind is LinkKind.UPLOAD and (
            any(field is None for field in upload_fields)
            or self.epoch is not None
            or self.lid is not None
        ):
            raise ValueError("an upload link carries a nonce and a client, and no epoch")
        return self


class SignedLink(BaseModel):
    """A signed URL and the moment it stops working (spec § 3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    expires_at: AwareDatetime


class ArtifactLink(BaseModel):
    """What ``artifact_link`` hands out: a private link, or the public URL (spec § 5.1).

    A recorded link carries its ``lid``; a generated passcode is here once and never again.
    The public URL carries ``public_until`` as its expiry, or none.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    expires_at: AwareDatetime | None = None
    lid: str | None = None
    passcode: str | None = None


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
