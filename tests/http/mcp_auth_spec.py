"""``/mcp`` over real HTTP: the bearer check and per-token scopes on the wire (T2, T12)."""

from __future__ import annotations

import hashlib
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from mcp_vitrine.config import Settings, load_settings
from mcp_vitrine.mcp.server import build_mcp
from mcp_vitrine.services.container import Services
from tests.conftest import FrozenClock

FULL_TOKEN = "full-token-for-hermes"
READ_TOKEN = "read-token-for-viewer"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "spec", "version": "0"},
    },
}
STARTUP_TIMEOUT = 10.0


def _hash(token: str) -> str:
    """The ``VITRINE_TOKENS`` form of a token."""
    return hashlib.sha256(token.encode()).hexdigest()


def _free_port() -> int:
    """A port nothing listens on right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture
def settings(environment: None, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Two clients: one with every scope, one that only reads."""
    monkeypatch.setenv(
        "VITRINE_TOKENS", f"hermes:{_hash(FULL_TOKEN)},viewer:{_hash(READ_TOKEN)}:read"
    )
    return load_settings()


@pytest.fixture
def endpoint(settings: Settings, clock: FrozenClock) -> Iterator[str]:
    """``/mcp`` served by uvicorn on a free port, for the length of one spec."""
    app = build_mcp(settings, Services.from_settings(settings, clock=clock)).http_app(path="/mcp")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(STARTUP_TIMEOUT)


def _post(endpoint: str, headers: dict[str, str]) -> int:
    """POST an ``initialize`` request and return the status."""
    response = httpx2.post(
        endpoint,
        json=INITIALIZE,
        headers={"Accept": "application/json, text/event-stream", **headers},
    )
    return response.status_code


async def _tool_names(endpoint: str, token: str) -> list[str]:
    """The tools a token sees over HTTP."""
    transport = StreamableHttpTransport(endpoint, headers={"Authorization": f"Bearer {token}"})
    client: Client[Any] = Client(transport)
    async with client:
        return sorted(tool.name for tool in await client.list_tools())


class DescribeTheMcpEndpoint:
    class WhenNoTokenIsSent:
        def it_answers_401(self, endpoint: str) -> None:
            assert _post(endpoint, {}) == 401

    class WhenTheTokenIsWrong:
        def it_answers_401(self, endpoint: str) -> None:
            assert _post(endpoint, {"Authorization": "Bearer guess"}) == 401

    class WhenTheTokenHashIsSentInstead:
        def it_answers_401(self, endpoint: str) -> None:
            assert _post(endpoint, {"Authorization": f"Bearer {_hash(FULL_TOKEN)}"}) == 401

    class WhenAFullScopeTokenConnects:
        async def it_sees_all_ten_tools(self, endpoint: str) -> None:
            assert len(await _tool_names(endpoint, FULL_TOKEN)) == 10

    class WhenAReadOnlyTokenConnects:
        async def it_sees_only_the_read_tools(self, endpoint: str) -> None:
            assert await _tool_names(endpoint, READ_TOKEN) == ["artifact_get", "artifact_list"]
