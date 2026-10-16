"""``build_app``: one ASGI app serving ``/mcp`` and the web routes behind one middleware stack.

This is the composition root of the two edges (design § 2-3). It sits above ``mcp`` and ``web``,
which never import each other, and is what ``mcp-vitrine serve`` hands to uvicorn.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta
from typing import Any

from anyio import to_thread
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from mcp_vitrine.config import Settings
from mcp_vitrine.mcp.server import build_mcp
from mcp_vitrine.services.container import Services
from mcp_vitrine.web.middleware import MCP_PATH, AccessLog, BodyLimit, HostGuard
from mcp_vitrine.web.routes import build_routes

UPLOAD_PREFIXES = ("/api", "/u")
# A periodic sweep spares staging younger than this: a live write stages and places in seconds.
STAGING_GRACE = timedelta(minutes=10)

logger = logging.getLogger(__name__)

type Lifespan = Callable[[Any], AbstractAsyncContextManager[Any]]


def build_app(settings: Settings, services: Services | None = None) -> Starlette:
    """The whole server: the MCP endpoint, the views, uploads, the API and ``/healthz``.

    Serve it with uvicorn's own access log and proxy-header handling off: both are done here, so
    the log is redacted and ``X-Forwarded-For`` is trusted from ``forwarded_allow_ips`` only.
    """
    services = services or Services.from_settings(settings)
    # vitrine's HostGuard covers every route, /mcp included; FastMCP's own guard would allow only
    # the bind address, which is wrong behind a proxy.
    mcp_app = build_mcp(settings, services).http_app(path=MCP_PATH, host_origin_protection=False)
    limits = {MCP_PATH: settings.max_mcp_body} | dict.fromkeys(UPLOAD_PREFIXES, settings.max_upload)
    return Starlette(
        routes=[*build_routes(settings, services), Route(MCP_PATH, mcp_app, name="mcp")],
        middleware=[
            # Outermost first: the client address is settled before anything logs or limits it.
            Middleware(ProxyHeadersMiddleware, trusted_hosts=settings.forwarded_allow_ips),
            Middleware(AccessLog),
            Middleware(
                HostGuard,
                base_url=settings.public_base_url,
                allowed_hosts=settings.allowed_hosts,
            ),
            Middleware(BodyLimit, limits=limits, default=settings.max_mcp_body),
        ],
        lifespan=_maintained(mcp_app.lifespan, services, settings.sweep_interval),
    )


def _maintained(inner: Lifespan, services: Services, interval: timedelta) -> Lifespan:
    """Wrap FastMCP's lifespan with the sweep: once before serving, then every ``interval``.

    The first sweep runs before the first request, so it can clear every half-staged file a crash
    left behind (operations § 2); later ones run beside writers and spare fresh staging.
    """

    @asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[Any]:  # noqa: ANN401 - Starlette's own types
        await _sweep(services, timedelta(0))
        async with inner(app) as state:
            task = asyncio.create_task(_sweep_every(services, interval))
            try:
                yield state
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    return lifespan


async def _sweep_every(services: Services, interval: timedelta) -> None:
    """Sweep forever; a failed sweep is logged and retried next time, never fatal to serving."""
    while True:
        await asyncio.sleep(interval.total_seconds())
        await _sweep(services, STAGING_GRACE)


async def _sweep(services: Services, grace: timedelta) -> None:
    """One sweep off the event loop (it walks the disk and fsyncs audit lines)."""
    try:
        report = await to_thread.run_sync(lambda: services.artifacts.sweep(grace=grace))
    except Exception:
        logger.exception("sweep failed; retrying at the next interval")
        return
    logger.info(
        "sweep: expired=%d staging=%d refs=%d audit_months=%d",
        report.expired,
        report.staging,
        report.refs,
        report.audit_months,
    )
