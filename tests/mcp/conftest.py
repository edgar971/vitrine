"""MCP givens: a server built from real services, and an in-memory client per protocol era."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.elicitation import ElicitResult
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from mcp_drop.config import Settings
from mcp_drop.domain.models import Scope
from mcp_drop.mcp.server import build_mcp
from mcp_drop.services.audit import AuditEvent
from mcp_drop.services.container import Services
from tests.conftest import FrozenClock

CLIENT = "hermes"
HANDSHAKE = "legacy"
MODERN = "2026-07-28"
SPEC_TOKEN = "in-memory"

Handler = Callable[[str, object, object, object], Awaitable[ElicitResult]]
Call = Callable[..., Awaitable[dict[str, Any]]]


@pytest.fixture
def services(settings: Settings, clock: FrozenClock) -> Services:
    """The real services over a temporary ``data_dir``, on the spec's clock."""
    return Services.from_settings(settings, clock=clock)


@pytest.fixture
def server(settings: Settings, services: Services) -> FastMCP[dict[str, Services]]:
    """The server under spec."""
    return build_mcp(settings, services)


@pytest.fixture
def scopes() -> frozenset[Scope]:
    """The caller's scopes; a nested class narrows them."""
    return frozenset(Scope)


@pytest.fixture
def mode() -> str:
    """The protocol era the client negotiates; the gate specs run both."""
    return HANDSHAKE


@pytest.fixture
def asked() -> list[str]:
    """Every question the human was shown."""
    return []


@pytest.fixture
def answer() -> ElicitResult | None:
    """What the human answers; ``None`` is a client that cannot elicit at all."""
    return ElicitResult(action="accept", content={})


@pytest.fixture
def handler(answer: ElicitResult | None, asked: list[str]) -> Handler | None:
    """An elicitation handler giving ``answer``, or none for a client without support."""
    if answer is None:
        return None

    async def respond(
        message: str, response_type: object, params: object, context: object
    ) -> ElicitResult:
        asked.append(message)
        return answer

    return respond


@pytest.fixture
async def client(
    server: FastMCP[dict[str, Services]],
    scopes: frozenset[Scope],
    mode: str,
    handler: Handler | None,
) -> AsyncIterator[Client[Any]]:
    """A connected in-memory client, authenticated as ``CLIENT`` with ``scopes``.

    The in-memory transport skips the HTTP bearer check, so the token the verifier would produce
    is placed where FastMCP reads it; the HTTP specs cover the verifier on the wire.
    """
    token = AccessToken(
        token=SPEC_TOKEN, client_id=CLIENT, scopes=sorted(scope.value for scope in scopes)
    )
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        async with Client(server, mode=mode, elicitation_handler=handler) as connected:
            yield connected
    finally:
        auth_context_var.reset(reset)


@pytest.fixture
def call(client: Client[Any]) -> Call:
    """Call a tool and return its structured result, raising on a tool error."""

    async def invoke(name: str, **arguments: object) -> dict[str, Any]:
        result = await client.call_tool(name, arguments)
        assert result.structured_content is not None
        return result.structured_content

    return invoke


@pytest.fixture
async def published(call: Call) -> dict[str, Any]:
    """One private artifact with two files, published through the tool."""
    return await call(
        "artifact_create",
        title="Energy, September",
        files={"index.html": "<h1>hi</h1>", "notes/readme.md": "# notes"},
    )


@pytest.fixture
def events(services: Services) -> Callable[[], tuple[AuditEvent, ...]]:
    """The audit log as it stands, newest first."""
    return services.audit.query
