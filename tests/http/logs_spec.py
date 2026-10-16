"""The access log (spec § 11.1, security T10): one line per request, no credentials in it."""

from __future__ import annotations

import logging
import time

import httpx2
import pytest

from mcp_vitrine.domain.models import PublishedArtifact
from mcp_vitrine.services.container import Services
from mcp_vitrine.web.middleware import ACCESS_LOGGER
from tests.http.conftest import CLIENT, FULL_TOKEN, signature


def _server_log(caplog: pytest.LogCaptureFixture) -> str:
    """Everything the server logged, once its access line is in.

    The access line is written after the response is sent, on uvicorn's thread, so the client
    can hold the response before the line exists. Waiting for it also keeps the "never logs"
    specs from passing on an empty log. The HTTP client's own request log is not the server's.
    """
    deadline = time.monotonic() + 5
    while not any(record.name == ACCESS_LOGGER for record in caplog.records):
        if time.monotonic() > deadline:
            pytest.fail("the server wrote no access line")
        time.sleep(0.01)
    return "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith(("mcp_vitrine", "uvicorn", "starlette", "fastmcp", "mcp."))
    )


@pytest.fixture
def logged(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """The log captured at INFO, where the access lines are written."""
    caplog.set_level(logging.INFO)
    return caplog


class DescribeTheAccessLog:
    class WhenASignedLinkIsViewed:
        def it_logs_the_request(
            self,
            http: httpx2.Client,
            published: PublishedArtifact,
            logged: pytest.LogCaptureFixture,
        ) -> None:
            http.get(f"{published.url}notes.md")
            assert '"GET /s/[redacted]/notes.md" 200' in _server_log(logged)

        def it_never_logs_the_signature(
            self,
            http: httpx2.Client,
            published: PublishedArtifact,
            logged: pytest.LogCaptureFixture,
        ) -> None:
            http.get(f"{published.url}notes.md")
            assert signature(published.url) not in _server_log(logged)

    class WhenAnUploadUrlIsUsed:
        def it_never_logs_the_signature(
            self, http: httpx2.Client, services: Services, logged: pytest.LogCaptureFixture
        ) -> None:
            grant = services.artifacts.upload_url("a.pdf", client=CLIENT)
            http.put(grant.put_url, content=b"%PDF")
            assert signature(grant.put_url, "/u/") not in _server_log(logged)

    class WhenABearerTokenIsSent:
        def it_never_logs_the_token(
            self, http: httpx2.Client, bearer: dict[str, str], logged: pytest.LogCaptureFixture
        ) -> None:
            http.get("/api/artifacts", headers=bearer)
            assert FULL_TOKEN not in _server_log(logged)

    class WhenARequestIsRefusedAtTheEdge:
        def it_is_still_logged(self, http: httpx2.Client, logged: pytest.LogCaptureFixture) -> None:
            http.get("/healthz", headers={"Host": "evil.example"})
            assert '"GET /healthz" 421' in _server_log(logged)
