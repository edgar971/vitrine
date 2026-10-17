"""Recorded links, passcodes, per-link revoke and the public window over MCP (spec § 5.1, § 6.1)."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.types import TextResourceContents

from mcp import MCPError
from mcp_vitrine.services.audit import AuditEventName
from mcp_vitrine.services.container import Services
from tests.conftest import FrozenClock
from tests.mcp.conftest import HANDSHAKE, MODERN, Call


@pytest.fixture
async def locked(call: Call, published: dict[str, Any]) -> dict[str, Any]:
    """A recorded link with a generated passcode, minted through the tool."""
    return await call("artifact_link", id=published["id"], label="sent to finance", passcode=True)


class DescribeArtifactLink:
    class WhenAPasscodeIsGenerated:
        async def it_returns_the_passcode(self, locked: dict[str, Any]) -> None:
            assert len(locked["passcode"]) == len("xxxxx-xxxxx")

        async def it_returns_the_lid(self, locked: dict[str, Any]) -> None:
            assert len(locked["lid"]) == 11

        async def it_lists_the_link_without_its_hash(
            self, call: Call, published: dict[str, Any], locked: dict[str, Any]
        ) -> None:
            detail = await call("artifact_get", id=published["id"])
            assert detail["links"][0] | {"created_at": None, "expires_at": None} == {
                "lid": locked["lid"],
                "label": "sent to finance",
                "created_at": None,
                "expires_at": None,
                "passcode": True,
            }

        async def it_never_returns_a_url_from_get(
            self, call: Call, published: dict[str, Any], locked: dict[str, Any]
        ) -> None:
            detail = await call("artifact_get", id=published["id"])
            assert locked["url"] not in json.dumps(detail)

        async def it_keeps_the_passcode_out_of_the_audit_log(
            self, locked: dict[str, Any], services: Services
        ) -> None:
            assert locked["passcode"] not in json.dumps(
                [event.model_dump(mode="json") for event in services.audit.query()]
            )

    class WhenASuppliedPasscodeIsTooShort:
        async def it_fails_without_repeating_it(
            self, client: Client[Any], published: dict[str, Any]
        ) -> None:
            with pytest.raises(ToolError) as refusal:
                await client.call_tool(
                    "artifact_link", {"id": published["id"], "passcode": "hunter2"}
                )
            assert "hunter2" not in str(refusal.value)

    class WhenTheArtifactIsPublic:
        async def it_refuses_a_passcode(self, call: Call, published: dict[str, Any]) -> None:
            await call("artifact_set_visibility", id=published["id"], visibility="public")
            with pytest.raises(ToolError, match="make it private"):
                await call("artifact_link", id=published["id"], passcode=True)


class DescribeArtifactRevokeLinks:
    class WhenOneLinkIsNamed:
        async def it_revokes_that_link(
            self, call: Call, published: dict[str, Any], locked: dict[str, Any]
        ) -> None:
            await call("artifact_revoke_links", id=published["id"], link=locked["lid"])
            assert (await call("artifact_get", id=published["id"]))["links"] == []

        async def it_names_it_in_the_result(
            self, call: Call, published: dict[str, Any], locked: dict[str, Any]
        ) -> None:
            result = await call("artifact_revoke_links", id=published["id"], link=locked["lid"])
            assert (result["link"], result["link_epoch"]) == (locked["lid"], 0)

        async def it_audits_its_lid(
            self,
            call: Call,
            published: dict[str, Any],
            locked: dict[str, Any],
            services: Services,
        ) -> None:
            await call("artifact_revoke_links", id=published["id"], link=locked["lid"])
            event = services.audit.query()[0]
            assert (event.event, event.lid) == (AuditEventName.LINKS_REVOKED, locked["lid"])


class DescribeThePublicGate:
    @pytest.fixture(params=[HANDSHAKE, MODERN])
    def mode(self, request: pytest.FixtureRequest) -> str:
        """The end date is named on both protocol eras."""
        era: str = request.param
        return era

    class WhenPublicForIsGiven:
        async def it_names_the_end_date_in_the_question(
            self, call: Call, published: dict[str, Any], asked: list[str]
        ) -> None:
            await call(
                "artifact_set_visibility", id=published["id"], visibility="public", public_for="9d"
            )
            assert "PUBLIC until 24 Oct 2026 10:00 UTC" in asked[0]

        async def it_returns_the_end(
            self, call: Call, published: dict[str, Any], clock: FrozenClock
        ) -> None:
            result = await call(
                "artifact_set_visibility", id=published["id"], visibility="public", public_for="1d"
            )
            assert result["public_until"] == (clock.now() + timedelta(days=1)).isoformat().replace(
                "+00:00", "Z"
            )

    class WhenThePublicWindowIsMovedLater:
        async def it_asks_again(
            self, call: Call, published: dict[str, Any], asked: list[str]
        ) -> None:
            await call(
                "artifact_set_visibility", id=published["id"], visibility="public", public_for="1d"
            )
            await call(
                "artifact_set_visibility", id=published["id"], visibility="public", public_for="7d"
            )
            assert len(asked) == 2

    class WhenItIsAskedForAWindowItAlreadyHas:
        async def it_does_not_ask(
            self, call: Call, published: dict[str, Any], asked: list[str]
        ) -> None:
            await call("artifact_set_visibility", id=published["id"], visibility="public")
            await call("artifact_set_visibility", id=published["id"], visibility="public")
            assert len(asked) == 1


class DescribePublicFor:
    class WhenItComesWithPrivate:
        async def it_is_refused(self, call: Call, published: dict[str, Any]) -> None:
            with pytest.raises(ToolError, match="public_for applies to visibility=public"):
                await call(
                    "artifact_set_visibility",
                    id=published["id"],
                    visibility="private",
                    public_for="1d",
                )


class DescribeTheViewsResource:
    class WhenItIsRead:
        async def it_lists_the_hours_as_json(
            self, client: Client[Any], published: dict[str, Any], services: Services
        ) -> None:
            services.views.record(
                published["id"], route="s", lid=None, address="203.0.113.7", user_agent="spec"
            )
            contents = await client.read_resource(f"artifact://{published['id']}/views")
            assert isinstance(contents[0], TextResourceContents)
            assert json.loads(contents[0].text)[0]["views"] == 1

    class WhenTheArtifactIsUnknown:
        async def it_is_not_found(self, client: Client[Any]) -> None:
            with pytest.raises(MCPError, match="not found"):
                await client.read_resource("artifact://k7m2q9x4tz3wbn6r/views")
