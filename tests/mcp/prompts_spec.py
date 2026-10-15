"""The ``share`` prompt (spec § 5.5)."""

from __future__ import annotations

from typing import Any

from fastmcp import Client
from mcp.types import TextContent


async def _share(client: Client[Any], **arguments: str) -> str:
    """The text of the one message ``share`` produces."""
    result = await client.get_prompt("share", arguments)
    content = result.messages[0].content
    assert isinstance(content, TextContent)
    return content.text


class DescribeTheSharePrompt:
    class WhenTheAudienceIsPrivate:
        async def it_says_to_publish_with_artifact_create(self, client: Client[Any]) -> None:
            assert "artifact_create" in await _share(client, what="the report")

        async def it_names_what_to_share(self, client: Client[Any]) -> None:
            assert "the report" in await _share(client, what="the report")

        async def it_never_asks_for_public(self, client: Client[Any]) -> None:
            text = await _share(client, what="the report")
            assert "artifact_set_visibility" not in text

    class WhenTheAudienceIsPublic:
        async def it_asks_for_public_through_set_visibility(self, client: Client[Any]) -> None:
            text = await _share(client, what="the report", audience="public")
            assert 'artifact_set_visibility with visibility "public"' in text

        async def it_says_the_user_confirms(self, client: Client[Any]) -> None:
            text = await _share(client, what="the report", audience="public")
            assert "asked to confirm" in text
