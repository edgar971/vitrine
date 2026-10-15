"""``build_app``: one ASGI app serving ``/mcp`` and the web routes behind one middleware stack.

This is the composition root of the two edges (design § 2-3). It sits above ``mcp`` and ``web``,
which never import each other, and is what ``mcp-drop serve`` hands to uvicorn.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from mcp_drop.config import Settings
from mcp_drop.mcp.server import build_mcp
from mcp_drop.services.container import Services
from mcp_drop.web.middleware import MCP_PATH, AccessLog, BodyLimit, HostGuard
from mcp_drop.web.routes import build_routes

UPLOAD_PREFIXES = ("/api", "/u")


def build_app(settings: Settings, services: Services | None = None) -> Starlette:
    """The whole server: the MCP endpoint, the views, uploads, the API and ``/healthz``.

    Serve it with uvicorn's own access log and proxy-header handling off: both are done here, so
    the log is redacted and ``X-Forwarded-For`` is trusted from ``forwarded_allow_ips`` only.
    """
    services = services or Services.from_settings(settings)
    # drop's HostGuard covers every route, /mcp included; FastMCP's own guard would allow only
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
        lifespan=mcp_app.lifespan,
    )
