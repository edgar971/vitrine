"""``Depends()`` providers: the services come from the server's lifespan, never from a global."""

from __future__ import annotations

from typing import cast

from fastmcp.server.dependencies import get_context

from mcp_drop.services.artifacts import ArtifactService
from mcp_drop.services.container import Services
from mcp_drop.services.gate import PublicGate

SERVICES_KEY = "drop"


def services() -> Services:
    """The services the running server was built with."""
    return cast(Services, get_context().lifespan_context[SERVICES_KEY])


def artifact_service() -> ArtifactService:
    """``ArtifactService``, injected and hidden from the tool's schema."""
    return services().artifacts


def public_gate() -> PublicGate:
    """``PublicGate``, injected into the one tool that can make an artifact public."""
    return services().gate
