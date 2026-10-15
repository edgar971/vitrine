"""The one path to public, on both protocol eras (spec § 6, ADR 0002, ADR 0004)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest
from fastmcp.client.elicitation import ElicitResult
from fastmcp.exceptions import ToolError

from mcp_drop.config import Settings, load_settings
from mcp_drop.domain.models import GateDecision, Visibility
from mcp_drop.mcp import gate
from mcp_drop.mcp.server import build_mcp
from mcp_drop.services.audit import AuditEvent, AuditEventName
from mcp_drop.services.container import Services
from tests.mcp.conftest import HANDSHAKE, MODERN, Call, Handler

Events = Callable[[], tuple[AuditEvent, ...]]


@pytest.fixture(params=[HANDSHAKE, MODERN])
def mode(request: pytest.FixtureRequest) -> str:
    """Every gate spec runs once per protocol era."""
    era: str = request.param
    return era


async def _go_public(call: Call, published: dict[str, Any]) -> dict[str, Any]:
    """Ask for the artifact to become public."""
    return await call("artifact_set_visibility", id=published["id"], visibility="public")


def _visibility(services: Services, published: dict[str, Any]) -> Visibility:
    """The artifact's visibility on disk."""
    return services.artifacts.get(published["id"]).artifact.visibility


def _refusal(events: Events) -> GateDecision | None:
    """The gate value of the newest ``publish_refused`` event."""
    refused = [event for event in events() if event.event is AuditEventName.PUBLISH_REFUSED]
    return refused[0].gate if refused else None


class DescribeThePublicGate:
    class WhenTheHumanApprovesWithoutData:
        async def it_makes_the_artifact_public(self, call: Call, published: dict[str, Any]) -> None:
            assert (await _go_public(call, published))["visibility"] == "public"

        async def it_answers_with_the_public_url(
            self, call: Call, published: dict[str, Any], settings: Settings
        ) -> None:
            result = await _go_public(call, published)
            assert result["url"] == f"{settings.public_base_url}/a/{published['id']}/"

        async def it_asks_exactly_once(
            self, call: Call, published: dict[str, Any], asked: list[str]
        ) -> None:
            await _go_public(call, published)
            assert len(asked) == 1

        async def it_names_the_title(
            self, call: Call, published: dict[str, Any], asked: list[str]
        ) -> None:
            await _go_public(call, published)
            assert '"Energy, September"' in asked[0]

        async def it_names_the_file_count(
            self, call: Call, published: dict[str, Any], asked: list[str]
        ) -> None:
            await _go_public(call, published)
            assert "2 files" in asked[0]

        async def it_says_anyone_with_the_link_can_view(
            self, call: Call, published: dict[str, Any], asked: list[str]
        ) -> None:
            await _go_public(call, published)
            assert "Anyone with the link" in asked[0]

        async def it_records_the_approval(
            self, call: Call, published: dict[str, Any], events: Events
        ) -> None:
            await _go_public(call, published)
            assert events()[0].gate is GateDecision.APPROVED

        async def it_revokes_the_private_links(
            self, call: Call, published: dict[str, Any], services: Services
        ) -> None:
            await _go_public(call, published)
            assert services.artifacts.get(published["id"]).artifact.link_epoch == 1

    class WhenTheHumanApprovesWithTheForm:
        @pytest.fixture
        def answer(self) -> ElicitResult:
            return ElicitResult(action="accept", content={"confirm": True})

        async def it_makes_the_artifact_public(self, call: Call, published: dict[str, Any]) -> None:
            assert (await _go_public(call, published))["visibility"] == "public"

    class WhenTheHumanDeclines:
        @pytest.fixture
        def answer(self) -> ElicitResult:
            return ElicitResult(action="decline")

        async def it_fails_saying_it_stays_private(
            self, call: Call, published: dict[str, Any]
        ) -> None:
            with pytest.raises(ToolError, match="stays private"):
                await _go_public(call, published)

        async def it_leaves_the_artifact_private(
            self, call: Call, published: dict[str, Any], services: Services
        ) -> None:
            with pytest.raises(ToolError):
                await _go_public(call, published)
            assert _visibility(services, published) is Visibility.PRIVATE

        async def it_records_the_refusal(
            self, call: Call, published: dict[str, Any], events: Events
        ) -> None:
            with pytest.raises(ToolError):
                await _go_public(call, published)
            assert _refusal(events) is GateDecision.DECLINED

    class WhenTheHumanAcceptsWithConfirmFalse:
        @pytest.fixture
        def answer(self) -> ElicitResult:
            return ElicitResult(action="accept", content={"confirm": False})

        async def it_leaves_the_artifact_private(
            self, call: Call, published: dict[str, Any], services: Services
        ) -> None:
            with pytest.raises(ToolError):
                await _go_public(call, published)
            assert _visibility(services, published) is Visibility.PRIVATE

    class WhenTheHumanCancels:
        @pytest.fixture
        def answer(self) -> ElicitResult:
            return ElicitResult(action="cancel")

        async def it_leaves_the_artifact_private(
            self, call: Call, published: dict[str, Any], services: Services
        ) -> None:
            with pytest.raises(ToolError):
                await _go_public(call, published)
            assert _visibility(services, published) is Visibility.PRIVATE

    class WhenTheClientCannotElicit:
        @pytest.fixture
        def answer(self) -> None:
            return None

        async def it_fails_saying_it_stays_private(
            self, call: Call, published: dict[str, Any]
        ) -> None:
            with pytest.raises(ToolError, match="stays private"):
                await _go_public(call, published)

        async def it_leaves_the_artifact_private(
            self, call: Call, published: dict[str, Any], services: Services
        ) -> None:
            with pytest.raises(ToolError):
                await _go_public(call, published)
            assert _visibility(services, published) is Visibility.PRIVATE

        async def it_records_the_refusal_as_unsupported(
            self, call: Call, published: dict[str, Any], events: Events
        ) -> None:
            with pytest.raises(ToolError):
                await _go_public(call, published)
            assert _refusal(events) is GateDecision.UNSUPPORTED

    class WhenPublicModeIsDisabled:
        @pytest.fixture
        def settings(self, environment: None, monkeypatch: pytest.MonkeyPatch) -> Settings:
            monkeypatch.setenv("DROP_PUBLIC_MODE", "disabled")
            return load_settings()

        async def it_does_not_ask(
            self, call: Call, published: dict[str, Any], asked: list[str]
        ) -> None:
            with pytest.raises(ToolError):
                await _go_public(call, published)
            assert asked == []

        async def it_leaves_the_artifact_private(
            self, call: Call, published: dict[str, Any], services: Services
        ) -> None:
            with pytest.raises(ToolError):
                await _go_public(call, published)
            assert _visibility(services, published) is Visibility.PRIVATE

        async def it_records_the_refusal_as_disabled(
            self, call: Call, published: dict[str, Any], events: Events
        ) -> None:
            with pytest.raises(ToolError):
                await _go_public(call, published)
            assert _refusal(events) is GateDecision.DISABLED

    class WhenPublicModeIsUnrestricted:
        @pytest.fixture
        def settings(self, environment: None, monkeypatch: pytest.MonkeyPatch) -> Settings:
            monkeypatch.setenv("DROP_PUBLIC_MODE", "unrestricted")
            return load_settings()

        async def it_makes_the_artifact_public_without_asking(
            self, call: Call, published: dict[str, Any], asked: list[str]
        ) -> None:
            await _go_public(call, published)
            assert asked == []

        async def it_records_that_no_approval_was_required(
            self, call: Call, published: dict[str, Any], events: Events
        ) -> None:
            await _go_public(call, published)
            assert events()[0].gate is GateDecision.NOT_REQUIRED

        def it_warns_at_startup(
            self, settings: Settings, services: Services, caplog: pytest.LogCaptureFixture
        ) -> None:
            with caplog.at_level(logging.WARNING, logger="mcp_drop.mcp.server"):
                build_mcp(settings, services)
            assert "unrestricted" in caplog.text

    class WhenTheArtifactIsAlreadyPublic:
        async def it_does_not_ask_again(
            self, call: Call, published: dict[str, Any], asked: list[str]
        ) -> None:
            await _go_public(call, published)
            await _go_public(call, published)
            assert len(asked) == 1

    class WhenAPublicArtifactGoesBackToPrivate:
        async def it_does_not_ask(
            self, call: Call, published: dict[str, Any], asked: list[str]
        ) -> None:
            await _go_public(call, published)
            await call("artifact_set_visibility", id=published["id"], visibility="private")
            assert len(asked) == 1

        async def it_answers_with_a_private_link(
            self, call: Call, published: dict[str, Any], settings: Settings
        ) -> None:
            await _go_public(call, published)
            result = await call("artifact_set_visibility", id=published["id"], visibility="private")
            assert result["url"].startswith(f"{settings.public_base_url}/s/")

        async def it_revokes_the_links_again(
            self, call: Call, published: dict[str, Any], services: Services
        ) -> None:
            await _go_public(call, published)
            await call("artifact_set_visibility", id=published["id"], visibility="private")
            assert services.artifacts.get(published["id"]).artifact.link_epoch == 2


class DescribeTheHandshakeEraGate:
    @pytest.fixture
    def mode(self) -> str:
        return HANDSHAKE

    class WhenTheHumanNeverAnswers:
        @pytest.fixture
        def handler(self, asked: list[str], monkeypatch: pytest.MonkeyPatch) -> Handler:
            monkeypatch.setattr(gate, "GATE_TIMEOUT", 0.05)

            async def stall(message: str, *_: object) -> ElicitResult:
                asked.append(message)
                await asyncio.sleep(1)
                return ElicitResult(action="accept", content={})

            return stall

        async def it_leaves_the_artifact_private(
            self, call: Call, published: dict[str, Any], services: Services
        ) -> None:
            with pytest.raises(ToolError):
                await _go_public(call, published)
            assert _visibility(services, published) is Visibility.PRIVATE

        async def it_records_the_refusal_as_declined(
            self, call: Call, published: dict[str, Any], events: Events
        ) -> None:
            with pytest.raises(ToolError):
                await _go_public(call, published)
            assert _refusal(events) is GateDecision.DECLINED

    class WhenTheClientFailsWhileAnswering:
        @pytest.fixture
        def handler(self) -> Handler:
            async def fail(*_: object) -> ElicitResult:
                raise RuntimeError("the approval UI crashed")

            return fail

        async def it_leaves_the_artifact_private(
            self, call: Call, published: dict[str, Any], services: Services
        ) -> None:
            with pytest.raises(ToolError):
                await _go_public(call, published)
            assert _visibility(services, published) is Visibility.PRIVATE

        async def it_records_the_refusal_as_unsupported(
            self, call: Call, published: dict[str, Any], events: Events
        ) -> None:
            with pytest.raises(ToolError):
                await _go_public(call, published)
            assert _refusal(events) is GateDecision.UNSUPPORTED
