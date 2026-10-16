"""The nine ``artifact_*`` tools (spec § 5.1, design § 4). Each one translates and calls a service.

A docstring here is the description the model reads, so it carries the policy of spec § 5.3.
"""

from __future__ import annotations

import json
import logging
import shlex
from functools import partial
from typing import Annotated

import mcp.types as mt
from anyio import to_thread
from fastmcp import Context
from fastmcp.dependencies import CurrentAccessToken, CurrentContext, Depends
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, require_scopes
from fastmcp.server.providers import LocalProvider
from pydantic import Field

from mcp_vitrine.config import requested_duration
from mcp_vitrine.domain.ids import ALPHABET, CLIENT_REF_MAX, ID_LENGTH
from mcp_vitrine.domain.models import (
    TAG_LIMIT,
    ArtifactDetail,
    ArtifactDraft,
    ArtifactLink,
    ArtifactPage,
    ArtifactQuery,
    GateDecision,
    PublishedArtifact,
    Scope,
    VersionDraft,
    Visibility,
)
from mcp_vitrine.domain.paths import MAX_PATH_BYTES
from mcp_vitrine.mcp import gate as public_gate_flow
from mcp_vitrine.mcp.deps import artifact_service, public_gate
from mcp_vitrine.mcp.errors import domain_errors
from mcp_vitrine.mcp.policy import tags
from mcp_vitrine.mcp.results import (
    Deleted,
    RevokedLinks,
    UpdatedArtifact,
    UploadUrl,
    VisibilityChange,
)
from mcp_vitrine.services.artifacts import ArtifactService
from mcp_vitrine.services.gate import PublicGate
from mcp_vitrine.services.limits import Bucket

# Tools that touch the disk give up rather than pin a worker on a stuck filesystem.
DISK_TIMEOUT = 30.0
NEVER = "never"
DURATION_PATTERN = r"^\d+[smhd]?$"

logger = logging.getLogger(__name__)

tools = LocalProvider()

ArtifactId = Annotated[
    str,
    Field(
        description="The artifact id, as artifact_create or artifact_list returned it.",
        pattern=f"^[{ALPHABET}]{{{ID_LENGTH}}}$",
    ),
]
Title = Annotated[
    str, Field(description="Shown in listings and in the confirmation prompt.", max_length=200)
]
Files = Annotated[
    dict[str, str],
    Field(
        description=(
            'Relative POSIX path -> UTF-8 text, e.g. {"index.html": "<h1>Hi</h1>"}. '
            "No absolute paths, no '.' or '..' segments, no dot-files."
        ),
        min_length=1,
    ),
]
Entry = Annotated[
    str | None,
    Field(
        description="The file the link opens. Defaults to index.html, or the only file.",
        max_length=MAX_PATH_BYTES,
    ),
]
Tags = Annotated[
    list[Annotated[str, Field(max_length=64)]] | None,
    Field(description="Labels to find the artifact by with artifact_list.", max_length=TAG_LIMIT),
]
Note = Annotated[
    str, Field(description="What changed in this version, kept in its history.", max_length=1000)
]
Ttl = Annotated[
    str | None,
    Field(
        description="How long the link works: 30m, 12h, 7d. Defaults and ceiling are the server's.",
        pattern=DURATION_PATTERN,
    ),
]


def _request(*arguments: object) -> str:
    """One canonical string for a call's arguments, which a gate answer is bound to."""
    return json.dumps(arguments, sort_keys=True, default=str)


def _log_gate(artifact_id: str, client: str, decision: GateDecision) -> None:
    """Log a gate verdict; the audit log has the durable record."""
    logger.info(
        "public gate: artifact=%s client=%s outcome=%s", artifact_id, client, decision.value
    )


def _caller(token: AccessToken) -> str:
    """The client name a token stands for, recorded as the artifact's source."""
    return token.client_id


@tools.tool(
    title="Create artifact",
    annotations=mt.ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
    tags=tags(Scope.WRITE, Bucket.WRITES),
    auth=require_scopes(Scope.WRITE),
    timeout=DISK_TIMEOUT,
)
@domain_errors(ToolError)
def artifact_create(
    title: Title,
    files: Files,
    entry: Entry = None,
    expires_in: Annotated[
        str | None,
        Field(
            description="Delete the artifact after this long: 30m, 12h, 7d. Default: never.",
            pattern=DURATION_PATTERN,
        ),
    ] = None,
    tags: Tags = None,
    note: Note = "",
    client_ref: Annotated[
        str | None,
        Field(
            description=(
                "Your own key for this publish. A retry with the same key and content returns "
                "the same artifact (created=false) instead of a duplicate."
            ),
            max_length=CLIENT_REF_MAX,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
    ] = None,
    service: ArtifactService = Depends(artifact_service),
    caller: AccessToken = CurrentAccessToken(),
) -> PublishedArtifact:
    """Publish text files as a PRIVATE artifact and return a signed link that expires.

    Everything published is private. To revise it, call artifact_update with the same id, so the
    link stays the same. For binary files, use artifact_upload_url instead.
    """
    draft = ArtifactDraft(
        title=title,
        files={path: text.encode() for path, text in files.items()},
        entry=entry,
        tags=tuple(tags or ()),
        note=note,
        expires_at=None if expires_in is None else service.expiry(requested_duration(expires_in)),
        client_ref=client_ref,
    )
    return service.create(draft, client=_caller(caller))


@tools.tool(
    title="Update artifact",
    annotations=mt.ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
    tags=tags(Scope.WRITE, Bucket.WRITES),
    auth=require_scopes(Scope.WRITE),
    timeout=public_gate_flow.GATE_TIMEOUT + DISK_TIMEOUT,
)
@domain_errors(ToolError)
async def artifact_update(
    id: ArtifactId,
    files: Annotated[
        dict[str, str] | None,
        Field(
            description=(
                "Relative POSIX path -> UTF-8 text. Replaces the files, or overlays them with "
                "merge=true. Omit to change only the metadata."
            )
        ),
    ] = None,
    merge: Annotated[
        bool, Field(description="Keep the current files and overlay these on top.")
    ] = False,
    entry: Entry = None,
    title: Annotated[str | None, Field(description="A new title.", max_length=200)] = None,
    expires_in: Annotated[
        str | None,
        Field(
            description="Delete the artifact after this long (30m, 12h, 7d), or 'never'.",
            pattern=rf"{DURATION_PATTERN}|^{NEVER}$",
        ),
    ] = None,
    tags: Tags = None,
    note: Note = "",
    ctx: Context = CurrentContext(),
    service: ArtifactService = Depends(artifact_service),
    gate: PublicGate = Depends(public_gate),
    caller: AccessToken = CurrentAccessToken(),
) -> UpdatedArtifact | mt.InputRequiredResult:
    """Revise a PRIVATE artifact in place: a new version under the same id and the same link.

    Always reuse the id of an artifact you published before rather than creating a new one.
    If the user made it public, they are asked to confirm first, because the new version is
    public at once; if they decline, nothing changes.
    """
    draft = VersionDraft(
        files=None if files is None else {path: text.encode() for path, text in files.items()},
        merge=merge,
        entry=entry,
        title=title,
        tags=None if tags is None else tuple(tags),
        note=note,
        expires_at=None
        if expires_in in (None, NEVER)
        else service.expiry(requested_duration(expires_in)),
        clear_expiry=expires_in == NEVER,
    )
    client = _caller(caller)
    detail = await to_thread.run_sync(partial(service.get, id))
    approval = None
    # Only a token that may publish is asked; any other is refused by the service, unasked.
    if detail.artifact.visibility is Visibility.PUBLIC and Scope.PUBLISH in caller.scopes:
        request = _request("update", files, merge, entry, title, expires_in, tags, note)
        answer = await public_gate_flow.confirm(
            ctx,
            gate,
            detail,
            question=gate.update_question(detail, files=None if files is None else len(files)),
            binding=service.gate_binding(id, client=client, request=request),
        )
        if isinstance(answer, mt.InputRequiredResult):
            return answer
        approval = answer
        _log_gate(id, client, approval.decision)
    published = await to_thread.run_sync(
        partial(
            service.update,
            id,
            draft,
            client=client,
            gate=None if approval is None else approval.decision,
            seen_version=None if approval is None else approval.version,
        )
    )
    return UpdatedArtifact(id=published.id, url=published.url, version=published.version)


@tools.tool(
    title="Get artifact",
    annotations=mt.ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
    tags=tags(Scope.READ, Bucket.READS),
    auth=require_scopes(Scope.READ),
    timeout=DISK_TIMEOUT,
)
@domain_errors(ToolError)
def artifact_get(
    id: ArtifactId,
    version: Annotated[
        int | None, Field(description="A retained version number. Default: the current one.", ge=1)
    ] = None,
    service: ArtifactService = Depends(artifact_service),
) -> ArtifactDetail:
    """Read an artifact's metadata and the file list of one version. Returns no link."""
    return service.get(id, version=version)


@tools.tool(
    title="List artifacts",
    annotations=mt.ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
    tags=tags(Scope.READ, Bucket.READS),
    auth=require_scopes(Scope.READ),
    timeout=DISK_TIMEOUT,
)
@domain_errors(ToolError)
def artifact_list(
    query: Annotated[
        str | None, Field(description="Matches a substring of the title or a tag.", max_length=200)
    ] = None,
    tag: Annotated[
        str | None, Field(description="Only artifacts with this tag.", max_length=64)
    ] = None,
    visibility: Annotated[
        Visibility | None, Field(description="Only private or only public artifacts.")
    ] = None,
    source: Annotated[
        str | None, Field(description="Only artifacts this client published.", max_length=64)
    ] = None,
    limit: Annotated[int, Field(description="Page size.", ge=1, le=100)] = 20,
    cursor: Annotated[
        str | None, Field(description="next_cursor from the previous page.", max_length=512)
    ] = None,
    service: ArtifactService = Depends(artifact_service),
) -> ArtifactPage:
    """List artifacts, newest first. No links: mint one on purpose with artifact_link."""
    return service.list(
        ArtifactQuery(
            query=query, tag=tag, visibility=visibility, source=source, limit=limit, cursor=cursor
        )
    )


@tools.tool(
    title="Create private link",
    annotations=mt.ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
    tags=tags(Scope.SHARE, Bucket.LINKS),
    auth=require_scopes(Scope.SHARE),
    timeout=DISK_TIMEOUT,
)
@domain_errors(ToolError)
def artifact_link(
    id: ArtifactId,
    ttl: Ttl = None,
    service: ArtifactService = Depends(artifact_service),
) -> ArtifactLink:
    """Mint a fresh signed private link that expires, or return the URL of a public artifact."""
    return service.link(id, ttl=None if ttl is None else requested_duration(ttl))


@tools.tool(
    title="Change visibility",
    annotations=mt.ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
    ),
    tags=tags(Scope.PUBLISH, Bucket.LINKS),
    auth=require_scopes(Scope.PUBLISH),
    timeout=public_gate_flow.GATE_TIMEOUT + DISK_TIMEOUT,
)
@domain_errors(ToolError)
async def artifact_set_visibility(
    id: ArtifactId,
    visibility: Annotated[
        Visibility, Field(description="public: anyone with the link can view. private: links only.")
    ],
    ctx: Context = CurrentContext(),
    service: ArtifactService = Depends(artifact_service),
    gate: PublicGate = Depends(public_gate),
    caller: AccessToken = CurrentAccessToken(),
) -> VisibilityChange | mt.InputRequiredResult:
    """Make an artifact public, or private again. Use public only when the user asked for it.

    The user is asked to confirm before anything becomes public; if they decline, it stays private.
    When a link is public, tell the user clearly. Every change revokes the existing private links.
    """
    client = _caller(caller)
    detail = await to_thread.run_sync(partial(service.get, id))
    decision = GateDecision.NOT_REQUIRED
    seen_version = None
    if visibility is Visibility.PUBLIC and detail.artifact.visibility is not Visibility.PUBLIC:
        answer = await public_gate_flow.confirm(
            ctx,
            gate,
            detail,
            question=gate.question(detail),
            binding=service.gate_binding(id, client=client, request=_request(visibility)),
        )
        if isinstance(answer, mt.InputRequiredResult):
            return answer
        decision, seen_version = answer.decision, answer.version
        _log_gate(id, client, decision)
    artifact = await to_thread.run_sync(
        partial(
            service.set_visibility,
            id,
            visibility,
            client=client,
            gate=decision,
            seen_version=seen_version,
        )
    )
    link = service.url_for(artifact)
    return VisibilityChange(id=artifact.id, visibility=artifact.visibility, url=link.url)


@tools.tool(
    title="Revoke private links",
    annotations=mt.ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
    ),
    tags=tags(Scope.SHARE, Bucket.LINKS),
    auth=require_scopes(Scope.SHARE),
    timeout=DISK_TIMEOUT,
)
@domain_errors(ToolError)
def artifact_revoke_links(
    id: ArtifactId,
    service: ArtifactService = Depends(artifact_service),
    caller: AccessToken = CurrentAccessToken(),
) -> RevokedLinks:
    """Stop every private link issued so far for this artifact. A public URL is not affected."""
    artifact = service.revoke_links(id, client=_caller(caller))
    return RevokedLinks(id=artifact.id, link_epoch=artifact.link_epoch)


@tools.tool(
    title="Create upload URL",
    annotations=mt.ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
    tags=tags(Scope.WRITE, Bucket.WRITES),
    auth=require_scopes(Scope.WRITE),
    timeout=DISK_TIMEOUT,
)
@domain_errors(ToolError)
def artifact_upload_url(
    filename: Annotated[
        str,
        Field(
            description="The file's relative path inside the artifact, e.g. report.pdf.",
            max_length=MAX_PATH_BYTES,
        ),
    ],
    id: Annotated[
        str | None,
        Field(
            description="Add a version to this artifact. Omit to create a new PRIVATE one.",
            pattern=f"^[{ALPHABET}]{{{ID_LENGTH}}}$",
        ),
    ] = None,
    title: Annotated[
        str | None,
        Field(description="Title of a new artifact. Default: the filename.", max_length=200),
    ] = None,
    ttl: Annotated[
        str | None,
        Field(description="How long the URL works: 5m, 15m, 1h.", pattern=DURATION_PATTERN),
    ] = None,
    service: ArtifactService = Depends(artifact_service),
    caller: AccessToken = CurrentAccessToken(),
) -> UploadUrl:
    """Get a single-use URL to PUT one binary file (PDF, image, …) into a PRIVATE artifact.

    Run the returned curl command with the local file. The id is fixed now, so reuse it.
    """
    grant = service.upload_url(
        filename,
        client=_caller(caller),
        artifact_id=id,
        title=title,
        ttl=None if ttl is None else requested_duration(ttl),
    )
    command = f"curl --fail -T {shlex.quote(filename)} {shlex.quote(grant.put_url)}"
    return UploadUrl(id=grant.id, put_url=grant.put_url, expires_at=grant.expires_at, curl=command)


@tools.tool(
    title="Delete artifact",
    annotations=mt.ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
    ),
    tags=tags(Scope.WRITE, Bucket.WRITES),
    auth=require_scopes(Scope.WRITE),
    timeout=DISK_TIMEOUT,
)
@domain_errors(ToolError)
def artifact_delete(
    id: ArtifactId,
    service: ArtifactService = Depends(artifact_service),
    caller: AccessToken = CurrentAccessToken(),
) -> Deleted:
    """Delete an artifact and every version of it; all its links stop working at once."""
    service.delete(id, client=_caller(caller))
    return Deleted(deleted=id)
