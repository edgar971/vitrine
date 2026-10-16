"""``artifact://`` resources: read back before revising (spec § 5.2)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client
from mcp.types import TextResourceContents

from mcp import MCPError
from mcp_vitrine.domain.ids import new_id
from mcp_vitrine.services.audit import AuditEventName
from tests.mcp.conftest import Call


async def _read(client: Client[Any], uri: str) -> str:
    """The text of one resource."""
    contents = await client.read_resource(uri)
    assert isinstance(contents[0], TextResourceContents)
    return contents[0].text


class DescribeTheArtifactResource:
    class WhenTheArtifactExists:
        async def it_returns_the_metadata(
            self, client: Client[Any], published: dict[str, Any]
        ) -> None:
            body = json.loads(await _read(client, f"artifact://{published['id']}"))
            assert body["artifact"]["title"] == "Energy, September"

        async def it_lists_the_files(self, client: Client[Any], published: dict[str, Any]) -> None:
            body = json.loads(await _read(client, f"artifact://{published['id']}"))
            assert [file["path"] for file in body["files"]] == ["index.html", "notes/readme.md"]

    class WhenTheIdIsUnknown:
        async def it_fails_as_not_found(self, client: Client[Any]) -> None:
            with pytest.raises(MCPError, match="not found"):
                await _read(client, f"artifact://{new_id()}")


class DescribeTheFileResource:
    class WhenTheFileIsText:
        async def it_returns_its_text(self, client: Client[Any], published: dict[str, Any]) -> None:
            text = await _read(client, f"artifact://{published['id']}/notes/readme.md")
            assert text == "# notes"

    class WhenTheFileIsNotAText:
        async def it_fails_naming_the_type_and_the_size(
            self, call: Call, client: Client[Any]
        ) -> None:
            created = await call(
                "artifact_create", title="T", files={"index.html": "x", "a.png": "png"}
            )
            with pytest.raises(MCPError, match=r"a\.png is image/png \(3 bytes\)"):
                await _read(client, f"artifact://{created['id']}/a.png")

    class WhenTheFileIsOverOneMegabyte:
        async def it_fails_naming_the_type_and_the_size(
            self, call: Call, client: Client[Any]
        ) -> None:
            big = "x" * (1024 * 1024 + 1)
            created = await call("artifact_create", title="T", files={"big.txt": big})
            with pytest.raises(MCPError, match=r"text/plain \(1048577 bytes\)"):
                await _read(client, f"artifact://{created['id']}/big.txt")

    class WhenThePathEscapes:
        async def it_fails(self, client: Client[Any], published: dict[str, Any]) -> None:
            with pytest.raises(MCPError):
                await _read(client, f"artifact://{published['id']}/../meta.json")


class DescribeTheHistoryResource:
    class WhenTheArtifactChanged:
        async def it_returns_the_events_newest_first(
            self, call: Call, client: Client[Any], published: dict[str, Any]
        ) -> None:
            await call("artifact_revoke_links", id=published["id"])
            events = json.loads(await _read(client, f"artifact://{published['id']}/history"))
            assert [event["event"] for event in events] == [
                AuditEventName.LINKS_REVOKED,
                AuditEventName.CREATED,
            ]

    class WhenTheIdIsUnknown:
        async def it_fails_as_not_found(self, client: Client[Any]) -> None:
            with pytest.raises(MCPError, match="not found"):
                await _read(client, f"artifact://{new_id()}/history")
