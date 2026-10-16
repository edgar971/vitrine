"""``build_mcp``: the FastMCP server assembled from the feature providers (design § 4)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from mcp_vitrine import __version__
from mcp_vitrine.config import Settings
from mcp_vitrine.domain.models import PublicMode
from mcp_vitrine.mcp.auth import HashedTokenVerifier
from mcp_vitrine.mcp.deps import SERVICES_KEY
from mcp_vitrine.mcp.instructions import INSTRUCTIONS
from mcp_vitrine.mcp.middleware import EdgeMiddleware
from mcp_vitrine.mcp.prompts import prompts
from mcp_vitrine.mcp.resources import resources
from mcp_vitrine.mcp.tools import tools
from mcp_vitrine.services.container import Services

NAME = "vitrine"

logger = logging.getLogger(__name__)


def build_mcp(settings: Settings, services: Services) -> FastMCP[dict[str, Services]]:
    """One server per process: hashed-token auth, the edge middleware and three providers."""
    if settings.public_mode is PublicMode.UNRESTRICTED:
        logger.warning(
            "public_mode is unrestricted: artifact_set_visibility makes artifacts public without "
            "asking anyone. Use it only on a single-user, fully trusted setup (spec § 6)."
        )

    @asynccontextmanager
    async def lifespan(_server: FastMCP[dict[str, Services]]) -> AsyncIterator[dict[str, Services]]:
        yield {SERVICES_KEY: services}

    return FastMCP(
        NAME,
        instructions=INSTRUCTIONS,
        version=__version__,
        providers=[tools, resources, prompts],
        auth=HashedTokenVerifier(settings.tokens),
        middleware=[EdgeMiddleware(services, tools)],
        lifespan=lifespan,
        # Anything that is not a VitrineError is a bug; its details (paths, internals) stay inside.
        mask_error_details=True,
    )
