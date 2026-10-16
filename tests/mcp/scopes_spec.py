"""Least privilege: a token neither sees nor calls what its scopes do not grant (§ 5.0, T12)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp import MCPError
from mcp_vitrine.domain.models import ArtifactQuery, Scope
from mcp_vitrine.services.audit import AuditEvent, AuditEventName, DenialReason
from mcp_vitrine.services.container import Services
from tests.mcp.conftest import CLIENT, Call

Events = Callable[[], tuple[AuditEvent, ...]]


class DescribeScopes:
    class WhenTheTokenOnlyReads:
        @pytest.fixture
        def scopes(self) -> frozenset[Scope]:
            return frozenset({Scope.READ})

        async def it_lists_only_the_read_tools(self, client: Client[Any]) -> None:
            names = sorted(tool.name for tool in await client.list_tools())
            assert names == ["artifact_get", "artifact_list"]

        async def it_fails_a_write_as_an_unknown_tool(self, call: Call) -> None:
            with pytest.raises(ToolError, match="Unknown tool"):
                await call("artifact_create", title="x", files={"index.html": "x"})

        async def it_writes_nothing(self, call: Call, services: Services) -> None:
            with pytest.raises(ToolError):
                await call("artifact_create", title="x", files={"index.html": "x"})
            assert services.artifacts.list(ArtifactQuery()).items == ()

        async def it_records_the_denial(self, call: Call, events: Events) -> None:
            with pytest.raises(ToolError):
                await call("artifact_create", title="x", files={"index.html": "x"})
            denial = events()[0]
            assert (denial.event, denial.reason, denial.tool, denial.client) == (
                AuditEventName.ACCESS_DENIED,
                DenialReason.SCOPE,
                "artifact_create",
                CLIENT,
            )

    class WhenTheTokenCannotPublish:
        @pytest.fixture
        def scopes(self) -> frozenset[Scope]:
            return frozenset({Scope.READ, Scope.WRITE, Scope.SHARE})

        async def it_does_not_list_set_visibility(self, client: Client[Any]) -> None:
            names = {tool.name for tool in await client.list_tools()}
            assert "artifact_set_visibility" not in names

        async def it_fails_set_visibility_as_an_unknown_tool(
            self, call: Call, published: dict[str, Any]
        ) -> None:
            with pytest.raises(ToolError, match="Unknown tool"):
                await call("artifact_set_visibility", id=published["id"], visibility="public")

        async def it_names_the_artifact_in_the_denial(
            self, call: Call, published: dict[str, Any], events: Events
        ) -> None:
            with pytest.raises(ToolError):
                await call("artifact_set_visibility", id=published["id"], visibility="public")
            assert events()[0].artifact == published["id"]

    class WhenTheTokenOnlyWrites:
        @pytest.fixture
        def scopes(self) -> frozenset[Scope]:
            return frozenset({Scope.WRITE})

        async def it_lists_no_resource_templates(self, client: Client[Any]) -> None:
            assert await client.list_resource_templates() == []

        async def it_cannot_read_a_resource(self, client: Client[Any], call: Call) -> None:
            created = await call("artifact_create", title="x", files={"index.html": "x"})
            with pytest.raises(MCPError):
                await client.read_resource(f"artifact://{created['id']}")

        async def it_records_the_resource_denial(
            self, client: Client[Any], call: Call, events: Events
        ) -> None:
            created = await call("artifact_create", title="x", files={"index.html": "x"})
            with pytest.raises(MCPError):
                await client.read_resource(f"artifact://{created['id']}")
            assert (events()[0].reason, events()[0].tool) == (DenialReason.SCOPE, "resources/read")

    class WhenTheTokenHasNoScopes:
        @pytest.fixture
        def scopes(self) -> frozenset[Scope]:
            return frozenset()

        async def it_lists_no_tools(self, client: Client[Any]) -> None:
            assert await client.list_tools() == []

        async def it_still_offers_the_share_prompt(self, client: Client[Any]) -> None:
            assert [prompt.name for prompt in await client.list_prompts()] == ["share"]
