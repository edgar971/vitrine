"""New content on a public artifact goes through the gate, on both eras (security T19, T20)."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

import mcp.types as mt
import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.exceptions import ToolError

from mcp import MCPError
from mcp_drop.config import Settings
from mcp_drop.domain.models import ArtifactDraft, GateDecision, Scope, VersionDraft, Visibility
from mcp_drop.services.audit import AuditEvent, AuditEventName
from mcp_drop.services.container import Services
from tests.mcp.conftest import CLIENT, HANDSHAKE, MODERN, Call, Handler

Events = Callable[[], tuple[AuditEvent, ...]]


@pytest.fixture(params=[HANDSHAKE, MODERN])
def mode(request: pytest.FixtureRequest) -> str:
    """Every spec here runs once per protocol era."""
    era: str = request.param
    return era


@pytest.fixture
def public(services: Services) -> str:
    """A public artifact at version 1, made public through the gate."""
    created = services.artifacts.create(
        ArtifactDraft(title="Launch notes", files={"index.html": b"<p>v1</p>"}), client=CLIENT
    )
    services.artifacts.set_visibility(
        created.id, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.APPROVED
    )
    return created.id


async def _update(call: Call, artifact_id: str) -> dict[str, Any]:
    """Add a version with new content."""
    return await call("artifact_update", id=artifact_id, files={"index.html": "<p>v2</p>"})


def _version(services: Services, artifact_id: str) -> int:
    """The artifact's current version on disk."""
    return services.artifacts.get(artifact_id).artifact.version


def _refusal(events: Events) -> GateDecision | None:
    """The gate value of the newest ``publish_refused`` event."""
    refused = [event for event in events() if event.event is AuditEventName.PUBLISH_REFUSED]
    return refused[0].gate if refused else None


class DescribeUpdatingAPublicArtifact:
    class WhenTheHumanApproves:
        async def it_writes_the_version(self, call: Call, public: str, services: Services) -> None:
            await _update(call, public)
            assert _version(services, public) == 2

        async def it_asks_once(self, call: Call, public: str, asked: list[str]) -> None:
            await _update(call, public)
            assert len(asked) == 1

        async def it_says_the_new_version_is_public_at_once(
            self, call: Call, public: str, asked: list[str]
        ) -> None:
            await _update(call, public)
            assert "PUBLIC" in asked[0]

        async def it_answers_with_the_public_url(
            self, call: Call, public: str, settings: Settings
        ) -> None:
            result = await _update(call, public)
            assert result["url"] == f"{settings.public_base_url}/a/{public}/"

    class WhenTheHumanDeclines:
        @pytest.fixture
        def answer(self) -> ElicitResult:
            return ElicitResult(action="decline")

        async def it_writes_nothing(self, call: Call, public: str, services: Services) -> None:
            with pytest.raises(ToolError):
                await _update(call, public)
            assert _version(services, public) == 1

        async def it_records_the_refusal(self, call: Call, public: str, events: Events) -> None:
            with pytest.raises(ToolError):
                await _update(call, public)
            assert _refusal(events) is GateDecision.DECLINED

    class WhenTheClientCannotElicit:
        @pytest.fixture
        def answer(self) -> None:
            return None

        async def it_writes_nothing(self, call: Call, public: str, services: Services) -> None:
            with pytest.raises(ToolError):
                await _update(call, public)
            assert _version(services, public) == 1

    class WhenTheTokenCannotPublish:
        @pytest.fixture
        def scopes(self) -> frozenset[Scope]:
            return frozenset({Scope.READ, Scope.WRITE})

        async def it_refuses_saying_the_artifact_is_public(self, call: Call, public: str) -> None:
            with pytest.raises(ToolError, match="is public"):
                await _update(call, public)

        async def it_does_not_ask(self, call: Call, public: str, asked: list[str]) -> None:
            with pytest.raises(ToolError):
                await _update(call, public)
            assert asked == []

        async def it_writes_nothing(self, call: Call, public: str, services: Services) -> None:
            with pytest.raises(ToolError):
                await _update(call, public)
            assert _version(services, public) == 1

        async def it_records_the_refusal(self, call: Call, public: str, events: Events) -> None:
            with pytest.raises(ToolError):
                await _update(call, public)
            assert _refusal(events) is GateDecision.UNSUPPORTED

    class WhenAnotherVersionLandsWhileTheHumanDecides:
        @pytest.fixture
        def handler(self, services: Services, public: str) -> Handler:
            async def race(*_: object) -> ElicitResult:
                services.artifacts.update(
                    public,
                    VersionDraft(files={"index.html": b"<p>sneaked</p>"}),
                    client=CLIENT,
                    gate=GateDecision.APPROVED,
                )
                return ElicitResult(action="accept", content={})

            return race

        async def it_refuses_saying_it_changed(self, call: Call, public: str) -> None:
            with pytest.raises(ToolError, match="changed"):
                await _update(call, public)

        async def it_writes_only_the_other_version(
            self, call: Call, public: str, services: Services
        ) -> None:
            with pytest.raises(ToolError):
                await _update(call, public)
            assert _version(services, public) == 2


class DescribeMakingPublicWhileTheContentChanges:
    class WhenAVersionLandsWhileTheHumanDecides:
        @pytest.fixture
        def private(self, services: Services) -> str:
            return services.artifacts.create(
                ArtifactDraft(title="Draft", files={"index.html": b"<p>v1</p>"}), client=CLIENT
            ).id

        @pytest.fixture
        def handler(self, services: Services, private: str) -> Handler:
            async def race(*_: object) -> ElicitResult:
                services.artifacts.update(
                    private, VersionDraft(files={"index.html": b"<p>unseen</p>"}), client=CLIENT
                )
                return ElicitResult(action="accept", content={})

            return race

        async def it_refuses_saying_it_changed(self, call: Call, private: str) -> None:
            with pytest.raises(ToolError, match="changed"):
                await call("artifact_set_visibility", id=private, visibility="public")

        async def it_leaves_the_artifact_private(
            self, call: Call, private: str, services: Services
        ) -> None:
            with pytest.raises(ToolError):
                await call("artifact_set_visibility", id=private, visibility="public")
            assert services.artifacts.get(private).artifact.visibility is Visibility.PRIVATE


class DescribeAnAnswerTheServerNeverAskedFor:
    @pytest.fixture
    def mode(self) -> str:
        return MODERN

    @staticmethod
    async def _answer(
        client: Client[Any], artifact_id: str, *, state: str | None
    ) -> mt.CallToolResult | mt.InputRequiredResult | mt.Result:
        """Call the tool once, with an approval and ``state``, as a forging client would."""
        return await client.session.call_tool(
            name="artifact_set_visibility",
            arguments={"id": artifact_id, "visibility": "public"},
            input_responses={"confirm_public": mt.ElicitResult(action="accept", content={})},
            request_state=state,
            allow_input_required=True,
        )

    class WhenItCarriesNoState:
        async def it_leaves_the_artifact_private(
            self, client: Client[Any], published: dict[str, Any], services: Services
        ) -> None:
            await DescribeAnAnswerTheServerNeverAskedFor._answer(
                client, published["id"], state=None
            )
            assert services.artifacts.get(published["id"]).artifact.visibility is (
                Visibility.PRIVATE
            )

        async def it_records_the_refusal_as_declined(
            self, client: Client[Any], published: dict[str, Any], events: Events
        ) -> None:
            await DescribeAnAnswerTheServerNeverAskedFor._answer(
                client, published["id"], state=None
            )
            assert _refusal(events) is GateDecision.DECLINED

    class WhenItCarriesTheStateOfAnotherRequest:
        @pytest.fixture
        async def other_state(self, client: Client[Any], call: Call) -> str | None:
            """The state the server minted for a question about a different artifact."""
            other = await call("artifact_create", title="Other", files={"a.txt": "a"})
            asked = await client.session.call_tool(
                name="artifact_set_visibility",
                arguments={"id": other["id"], "visibility": "public"},
                allow_input_required=True,
            )
            assert isinstance(asked, mt.InputRequiredResult)
            return asked.request_state

        async def it_is_rejected(
            self, client: Client[Any], published: dict[str, Any], other_state: str | None
        ) -> None:
            with pytest.raises(MCPError):
                await DescribeAnAnswerTheServerNeverAskedFor._answer(
                    client, published["id"], state=other_state
                )

        async def it_leaves_the_artifact_private(
            self,
            client: Client[Any],
            published: dict[str, Any],
            other_state: str | None,
            services: Services,
        ) -> None:
            with contextlib.suppress(MCPError):
                await DescribeAnAnswerTheServerNeverAskedFor._answer(
                    client, published["id"], state=other_state
                )
            assert services.artifacts.get(published["id"]).artifact.visibility is (
                Visibility.PRIVATE
            )
