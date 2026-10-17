"""The tool catalogue as a client sees it: names, titles, hints, scopes and schemas (design § 4)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from mcp.types import Tool

from mcp_vitrine.mcp.instructions import INSTRUCTIONS
from mcp_vitrine.mcp.policy import scope_of
from mcp_vitrine.services.container import Services

SPEC = Path(__file__).parents[2] / "docs" / "spec.md"

# design § 4, "Tool catalogue": title, scope, read-only, destructive, idempotent, open-world.
CATALOGUE: dict[str, tuple[str, str, bool, bool, bool, bool]] = {
    "artifact_create": ("Create artifact", "vitrine:write", False, False, False, False),
    "artifact_update": ("Update artifact", "vitrine:write", False, False, False, False),
    "artifact_get": ("Get artifact", "vitrine:read", True, False, True, False),
    "artifact_list": ("List artifacts", "vitrine:read", True, False, True, False),
    "artifact_set_meta": ("Edit artifact metadata", "vitrine:write", False, False, True, False),
    "artifact_link": ("Create private link", "vitrine:share", False, False, False, False),
    "artifact_set_visibility": ("Change visibility", "vitrine:publish", False, False, True, True),
    "artifact_revoke_links": ("Revoke private links", "vitrine:share", False, True, False, False),
    "artifact_upload_url": ("Create upload URL", "vitrine:write", False, False, False, False),
    "artifact_delete": ("Delete artifact", "vitrine:write", False, True, True, False),
}

WRITERS = ("artifact_create", "artifact_update", "artifact_upload_url", "artifact_set_meta")


@pytest.fixture
async def listed(client: Client[Any]) -> dict[str, Tool]:
    """Every tool a full-scope token sees, by name."""
    return {tool.name: tool for tool in await client.list_tools()}


def _hints(tool: Tool) -> tuple[bool | None, ...]:
    """The four annotation hints, in the catalogue's column order."""
    hints = tool.annotations
    assert hints is not None
    return (
        hints.read_only_hint,
        hints.destructive_hint,
        hints.idempotent_hint,
        hints.open_world_hint,
    )


def _tags(tool: Tool) -> list[str]:
    """The tags FastMCP publishes in the tool's metadata."""
    assert tool.meta is not None
    tags: list[str] = tool.meta["fastmcp"]["tags"]
    return tags


def _spec_instructions() -> str:
    """The instructions block of spec § 5.4, verbatim."""
    section = SPEC.read_text(encoding="utf-8").split("### 5.4 Server instructions", 1)[1]
    match = re.search(r"```text\n(.*?)\n```", section, re.DOTALL)
    assert match is not None
    return match.group(1)


class DescribeTheToolCatalogue:
    class WhenAFullScopeTokenLists:
        def it_offers_exactly_the_ten_tools(self, listed: dict[str, Tool]) -> None:
            assert sorted(listed) == sorted(CATALOGUE)

        @pytest.mark.parametrize("name", sorted(CATALOGUE))
        def it_titles_each_tool(self, listed: dict[str, Tool], name: str) -> None:
            assert listed[name].title == CATALOGUE[name][0]

        @pytest.mark.parametrize("name", sorted(CATALOGUE))
        def it_sets_all_four_hints(self, listed: dict[str, Tool], name: str) -> None:
            assert _hints(listed[name]) == CATALOGUE[name][2:]

        @pytest.mark.parametrize("name", sorted(CATALOGUE))
        def it_names_the_scope(self, listed: dict[str, Tool], name: str) -> None:
            scope = scope_of(_tags(listed[name]))
            assert scope is not None
            assert scope.value == CATALOGUE[name][1]

        @pytest.mark.parametrize("name", sorted(CATALOGUE))
        def it_describes_every_parameter(self, listed: dict[str, Tool], name: str) -> None:
            properties = listed[name].input_schema["properties"]
            undocumented = [
                key for key, schema in properties.items() if not schema.get("description")
            ]
            assert undocumented == []

        @pytest.mark.parametrize("name", sorted(CATALOGUE))
        def it_hides_the_injected_parameters(self, listed: dict[str, Tool], name: str) -> None:
            properties = set(listed[name].input_schema["properties"])
            assert properties.isdisjoint({"service", "gate", "caller", "ctx"})

        @pytest.mark.parametrize("name", sorted(CATALOGUE))
        def it_publishes_an_output_schema(self, listed: dict[str, Tool], name: str) -> None:
            assert listed[name].output_schema is not None

        @pytest.mark.parametrize("name", WRITERS)
        def it_takes_no_visibility_where_it_writes(
            self, listed: dict[str, Tool], name: str
        ) -> None:
            assert "visibility" not in listed[name].input_schema["properties"]

    class WhenTheModelReadsTheDescriptions:
        @pytest.mark.parametrize("name", ["artifact_create", "artifact_update"])
        def it_says_artifacts_are_private(self, listed: dict[str, Tool], name: str) -> None:
            assert "PRIVATE" in (listed[name].description or "")

        @pytest.mark.parametrize("name", ["artifact_create", "artifact_update"])
        def it_says_to_reuse_the_id(self, listed: dict[str, Tool], name: str) -> None:
            assert "same id" in (listed[name].description or "").replace("\n", " ")

        def it_limits_public_to_a_user_request(self, listed: dict[str, Tool]) -> None:
            description = listed["artifact_set_visibility"].description or ""
            assert "only when the user asked" in description

        def it_says_the_user_confirms(self, listed: dict[str, Tool]) -> None:
            description = listed["artifact_set_visibility"].description or ""
            assert "asked to confirm" in description

        def it_says_to_tell_the_user(self, listed: dict[str, Tool]) -> None:
            description = listed["artifact_set_visibility"].description or ""
            assert "tell the user clearly" in description


class DescribeTheServerInstructions:
    class WhenAClientConnects:
        async def it_sends_the_spec_text(self, client: Client[Any]) -> None:
            initialized = client.initialize_result
            assert initialized is not None
            assert initialized.instructions == _spec_instructions()

        def it_matches_spec_section_5_4(self) -> None:
            assert _spec_instructions() == INSTRUCTIONS


class DescribeTheResourceTemplates:
    class WhenAFullScopeTokenLists:
        async def it_offers_the_three_templates(
            self, client: Client[Any], server: FastMCP[dict[str, Services]]
        ) -> None:
            templates = {
                template.uri_template for template in await client.list_resource_templates()
            }
            assert templates == {
                "artifact://{id}",
                "artifact://{id}/{path*}",
                "artifact://{id}/history",
            }
