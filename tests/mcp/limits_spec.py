"""Per-client buckets at the MCP edge: a rejected call does no work (spec § 7.5, T16)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp import MCPError
from mcp_drop.config import Settings, load_settings
from mcp_drop.domain.models import ArtifactQuery
from mcp_drop.services.audit import AuditEvent, DenialReason
from mcp_drop.services.container import Services
from tests.mcp.conftest import Call

Events = Callable[[], tuple[AuditEvent, ...]]
FILES = {"index.html": "x"}


class DescribeRateLimits:
    class WhenTheWritesBucketIsEmpty:
        @pytest.fixture
        def settings(self, environment: None, monkeypatch: pytest.MonkeyPatch) -> Settings:
            monkeypatch.setenv("DROP_RATE_WRITES", "1/h")
            return load_settings()

        async def it_fails_with_the_wait(self, call: Call) -> None:
            await call("artifact_create", title="T", files=FILES)
            with pytest.raises(ToolError, match=r"^rate limited: retry after \d+s$"):
                await call("artifact_create", title="T", files=FILES)

        async def it_writes_nothing(self, call: Call, services: Services) -> None:
            await call("artifact_create", title="T", files=FILES)
            with pytest.raises(ToolError):
                await call("artifact_create", title="T", files=FILES)
            assert len(services.artifacts.list(ArtifactQuery()).items) == 1

        async def it_records_the_denial(self, call: Call, events: Events) -> None:
            await call("artifact_create", title="T", files=FILES)
            with pytest.raises(ToolError):
                await call("artifact_create", title="T", files=FILES)
            assert (events()[0].reason, events()[0].tool) == (
                DenialReason.RATE_LIMITED,
                "artifact_create",
            )

        async def it_leaves_the_reads_bucket_alone(self, call: Call) -> None:
            await call("artifact_create", title="T", files=FILES)
            assert (await call("artifact_list"))["items"] != []

    class WhenTheReadsBucketIsEmpty:
        @pytest.fixture
        def settings(self, environment: None, monkeypatch: pytest.MonkeyPatch) -> Settings:
            monkeypatch.setenv("DROP_RATE_READS", "1/h")
            return load_settings()

        async def it_limits_resources_too(
            self, call: Call, client: Client[Any], published: dict[str, Any]
        ) -> None:
            await call("artifact_get", id=published["id"])
            with pytest.raises(MCPError, match="rate limited"):
                await client.read_resource(f"artifact://{published['id']}")

    class WhenALimitIsOff:
        @pytest.fixture
        def settings(self, environment: None, monkeypatch: pytest.MonkeyPatch) -> Settings:
            monkeypatch.setenv("DROP_RATE_LINKS", "off")
            return load_settings()

        async def it_never_refuses(self, call: Call, published: dict[str, Any]) -> None:
            await call("artifact_link", id=published["id"])
            assert (await call("artifact_link", id=published["id"]))["url"]
