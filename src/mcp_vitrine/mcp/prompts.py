"""The ``share`` prompt: a one-click "share this" that grants nothing the tools don't (§ 5.5)."""

# No ``from __future__ import annotations``: FastMCP resolves a prompt's argument types in its
# own namespace, where a string ``Literal[...]`` cannot be evaluated.
from typing import Annotated, Literal

from fastmcp.server.providers import LocalProvider
from pydantic import Field

prompts = LocalProvider()

PRIVATE_STEP = "Keep it private: do not change its visibility."
PUBLIC_STEP = (
    "The user wants a public link: then call artifact_set_visibility with visibility "
    '"public". The user is asked to confirm; if they decline, it stays private, so share the '
    "private link and say so. When the link is public, say so clearly."
)


@prompts.prompt(
    name="share",
    title="Share",
    description="Publish something from this conversation with vitrine and reply with the link.",
)
def share(
    what: Annotated[str, Field(description="The output to share, described in a few words.")],
    audience: Annotated[
        Literal["private", "public"],
        Field(description="private (default): a signed link. public: anyone with the link."),
    ] = "private",
) -> str:
    """Tell the agent to publish ``what`` and, only for a public audience, to ask for public."""
    step = PUBLIC_STEP if audience == "public" else PRIVATE_STEP
    return (
        f"Publish {what} with vitrine: call artifact_create with its files, or artifact_update "
        "with the same id if you published it before, so the link stays the same. "
        "Reply with the link. "
        f"{step}"
    )
