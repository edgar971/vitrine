"""The MCP edge: record refusals and rate-limit, once for every tool and resource (spec § 7.5).

FastMCP already hides a component from a token without its scope; this layer records that refusal
and spends the caller's bucket before any work is done, so a rejected call writes nothing.
"""

from __future__ import annotations

from collections.abc import Collection
from functools import partial
from typing import Any
from urllib.parse import urlsplit

import mcp.types as mt
from anyio import to_thread
from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.providers import LocalProvider

from mcp_vitrine.domain.errors import RateLimited
from mcp_vitrine.domain.ids import is_valid_id
from mcp_vitrine.domain.models import Scope
from mcp_vitrine.mcp.policy import bucket_of, scope_of
from mcp_vitrine.services.audit import DenialReason
from mcp_vitrine.services.container import Services
from mcp_vitrine.services.limits import Bucket

# Resource reads are audited under the MCP method, never under a URI that could name a file.
RESOURCE_READ = "resources/read"


class EdgeMiddleware(Middleware):
    """Audit refusals and apply the per-client buckets before a call runs."""

    def __init__(self, services: Services, tools: LocalProvider) -> None:
        """Hold the services and the full tool catalogue, scopes ignored."""
        self._services = services
        self._tools = tools

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, Any],
    ) -> Any:  # noqa: ANN401 - whatever the tool returns, untouched
        """Check the caller against the tool's scope and bucket, then run it."""
        name = context.message.name
        tool = await self._tools.get_tool(name)
        if tool is not None:
            artifact = _artifact_argument(context.message.arguments)
            await self._admit(tool.tags, call=name, artifact=artifact, error=ToolError)
        return await call_next(context)

    async def on_read_resource(
        self,
        context: MiddlewareContext[mt.ReadResourceRequestParams],
        call_next: CallNext[mt.ReadResourceRequestParams, Any],
    ) -> Any:  # noqa: ANN401 - whatever the resource returns, untouched
        """Every resource needs ``vitrine:read`` and spends the reads bucket."""
        artifact = urlsplit(str(context.message.uri)).netloc
        await self._admit(
            {Scope.READ.value, f"bucket:{Bucket.READS.value}"},
            call=RESOURCE_READ,
            artifact=artifact if is_valid_id(artifact) else None,
            error=ResourceError,
        )
        return await call_next(context)

    async def _admit(
        self,
        tags: Collection[str],
        *,
        call: str,
        artifact: str | None,
        error: type[ToolError] | type[ResourceError],
    ) -> None:
        """Record a scope refusal, or spend one token from the caller's bucket."""
        caller = get_access_token()
        if caller is None:
            return
        scope = scope_of(tags)
        if scope is not None and scope.value not in caller.scopes:
            # FastMCP answers "unknown" itself, so the refusal looks like a missing tool.
            await self._deny(caller.client_id, DenialReason.SCOPE, call, artifact)
            return
        bucket = bucket_of(tags)
        if bucket is None:
            return
        try:
            self._services.limits.check(bucket, caller.client_id)
        except RateLimited as limited:
            await self._deny(caller.client_id, DenialReason.RATE_LIMITED, call, artifact)
            raise error(str(limited)) from None

    async def _deny(
        self, client: str, reason: DenialReason, call: str, artifact: str | None
    ) -> None:
        """Append one ``access_denied`` event, off the event loop (it fsyncs)."""
        await to_thread.run_sync(
            partial(
                self._services.audit.deny,
                client=client,
                reason=reason,
                tool=call,
                artifact=artifact,
            )
        )


def _artifact_argument(arguments: dict[str, Any] | None) -> str | None:
    """The artifact a call names, when it names a well-formed one; never anything else."""
    candidate = (arguments or {}).get("id")
    return candidate if isinstance(candidate, str) and is_valid_id(candidate) else None
