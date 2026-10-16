"""The edge in front of every route: Host/Origin (spec § 7.4) and streaming body limits (§ 7.3).

DNS rebinding (security T8) and the streaming body limits (T9), against the whole app rather
than ``/mcp`` alone.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx2
import pytest

from tests.http.conftest import FULL_TOKEN

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
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Authorization": f"Bearer {FULL_TOKEN}",
}


def _chunks(total: int) -> Iterator[bytes]:
    """A body with no ``Content-Length``, sent chunked."""
    sent = 0
    while sent < total:
        yield b" " * 1024
        sent += 1024


class DescribeTheHostGuard:
    class WhenTheHostIsAllowed:
        def it_lets_the_request_through(self, http: httpx2.Client) -> None:
            assert http.get("/healthz").status_code == 200

    class WhenTheHostIsForeign:
        def it_answers_421(self, http: httpx2.Client) -> None:
            assert http.get("/healthz", headers={"Host": "evil.example"}).status_code == 421

    class WhenARebindingPageSendsAMatchingOrigin:
        def it_still_answers_421(self, http: httpx2.Client) -> None:
            headers = {"Host": "evil.example", "Origin": "http://evil.example"}
            assert http.post("/mcp", json=INITIALIZE, headers=headers).status_code == 421

    class WhenTheHostIsTheProbingPodIp:
        def it_answers_421(self, http: httpx2.Client) -> None:
            assert http.get("/healthz", headers={"Host": "10.0.0.7:8080"}).status_code == 421

    class WhenTheOperatorAllowedAnotherHost:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            return {"VITRINE_ALLOWED_HOSTS": "vitrine.internal"}

        def it_lets_that_host_through(self, http: httpx2.Client) -> None:
            assert (
                http.get("/healthz", headers={"Host": "vitrine.internal:8080"}).status_code == 200
            )


class DescribeTheOriginCheck:
    class WhenABrowserOnAnotherOriginCallsMcp:
        def it_answers_403(self, http: httpx2.Client) -> None:
            headers = MCP_HEADERS | {"Origin": "http://evil.example"}
            assert http.post("/mcp", json=INITIALIZE, headers=headers).status_code == 403

    class WhenABrowserOnAnotherPortOfTheSameHostCallsMcp:
        def it_answers_403(self, http: httpx2.Client) -> None:
            headers = MCP_HEADERS | {"Origin": "http://127.0.0.1:1"}
            assert http.post("/mcp", json=INITIALIZE, headers=headers).status_code == 403

    class WhenASandboxedPageCallsMcp:
        def it_answers_403(self, http: httpx2.Client) -> None:
            headers = MCP_HEADERS | {"Origin": "null"}
            assert http.post("/mcp", json=INITIALIZE, headers=headers).status_code == 403

    class WhenTheOriginIsVitrineItself:
        def it_lets_the_request_through(self, http: httpx2.Client, base: str) -> None:
            headers = MCP_HEADERS | {"Origin": base}
            assert http.post("/mcp", json=INITIALIZE, headers=headers).status_code == 200

    class WhenAForeignOriginLoadsAView:
        def it_is_not_refused(self, http: httpx2.Client) -> None:
            assert (
                http.get("/healthz", headers={"Origin": "http://evil.example"}).status_code == 200
            )


class DescribeTheMcpBodyLimit:
    @pytest.fixture
    def overrides(self) -> dict[str, str]:
        return {"VITRINE_MAX_MCP_BODY": "16KB"}

    class WhenTheDeclaredLengthIsTooLarge:
        def it_answers_413(self, http: httpx2.Client) -> None:
            response = http.post("/mcp", content=b" " * (17 * 1024), headers=MCP_HEADERS)
            assert response.status_code == 413

    class WhenAChunkedBodyGrowsTooLarge:
        def it_answers_413(self, http: httpx2.Client) -> None:
            response = http.post("/mcp", content=_chunks(64 * 1024), headers=MCP_HEADERS)
            assert response.status_code == 413

    class WhenTheBodyFits:
        def it_reaches_the_server(self, http: httpx2.Client) -> None:
            assert http.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 200
