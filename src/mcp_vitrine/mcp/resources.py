"""``artifact://`` resources: read an artifact back before revising it (spec § 5.2)."""

from __future__ import annotations

from typing import Annotated

from fastmcp.dependencies import Depends
from fastmcp.exceptions import ResourceError
from fastmcp.server.auth import require_scopes
from fastmcp.server.providers import LocalProvider
from pydantic import Field, TypeAdapter

from mcp_vitrine.domain.models import Scope
from mcp_vitrine.mcp.deps import artifact_service
from mcp_vitrine.mcp.errors import domain_errors
from mcp_vitrine.mcp.policy import tags
from mcp_vitrine.services.artifacts import ArtifactService
from mcp_vitrine.services.audit import AuditEvent
from mcp_vitrine.services.limits import Bucket

JSON = "application/json"

resources = LocalProvider()

_EVENTS = TypeAdapter(tuple[AuditEvent, ...])

ArtifactId = Annotated[str, Field(description="The artifact id.")]


# Registered before the file template, so a request for history never reads a file by that name.
@resources.resource(
    "artifact://{id}/history",
    name="artifact_history",
    title="Artifact history",
    description="This artifact's audit events, newest first: who changed what, and when.",
    mime_type=JSON,
    tags=tags(Scope.READ, Bucket.READS),
    auth=require_scopes(Scope.READ),
)
@domain_errors(ResourceError)
def artifact_history(
    id: ArtifactId,
    service: ArtifactService = Depends(artifact_service),
) -> str:
    """The audit events of one artifact, as JSON."""
    return _EVENTS.dump_json(service.history(id), by_alias=True, exclude_none=True).decode()


@resources.resource(
    "artifact://{id}/{path*}",
    name="artifact_file",
    title="Artifact file",
    description="The text of one file in the current version: text types only, up to 1 MB.",
    mime_type="text/plain",
    tags=tags(Scope.READ, Bucket.READS),
    auth=require_scopes(Scope.READ),
)
@domain_errors(ResourceError)
def artifact_file(
    id: ArtifactId,
    path: Annotated[str, Field(description="The file's relative path in the artifact.")],
    service: ArtifactService = Depends(artifact_service),
) -> str:
    """One text file of the current version."""
    return service.read_text(id, path)


@resources.resource(
    "artifact://{id}",
    name="artifact",
    title="Artifact",
    description="meta.json plus the current version's file list.",
    mime_type=JSON,
    tags=tags(Scope.READ, Bucket.READS),
    auth=require_scopes(Scope.READ),
)
@domain_errors(ResourceError)
def artifact(
    id: ArtifactId,
    service: ArtifactService = Depends(artifact_service),
) -> str:
    """The metadata and the file list, as JSON."""
    return service.get(id).model_dump_json()
