"""drop for Hermes Agent: publish local files as private links (MCP server: ``mcp_servers.drop``).

The MCP server owns everything else, including the one path to public. This plugin only adds
what an MCP call cannot do well: stream files that are already on disk.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from .client import DropClient, DropError, Endpoint, collect

TOOL = "artifact_publish_files"
LIST_LIMIT = 20

PUBLISH_SCHEMA: dict[str, Any] = {
    "name": TOOL,
    "description": (
        "Upload files already on disk, or a folder, to drop and get back a private link. Use it "
        "for binaries (PNG, PDF), a folder with assets, or files too large to pass inline; for "
        "text you write yourself, use mcp_drop_artifact_create. Pass `id` to publish a new version "
        "behind the same link. Always private: going public is mcp_drop_artifact_set_visibility, "
        "which asks the user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": (
                    "Files or folders. A file keeps its name; a folder's contents keep their paths "
                    "relative to it. Hidden files and symlinks inside folders are skipped."
                ),
            },
            "title": {
                "type": "string",
                "maxLength": 200,
                "description": "Shown on the link; defaults to the first file's name.",
            },
            "id": {"type": "string", "description": "An existing artifact to add a version to."},
            "merge": {
                "type": "boolean",
                "description": "With `id`: overlay these files on the current ones instead of "
                "replacing them all.",
            },
            "entry": {
                "type": "string",
                "description": "The file the link opens; defaults to index.html or the only file.",
            },
            "expires_in": {
                "type": "string",
                "description": "How long the artifact lives, e.g. 1h or 7d; `never` with `id`.",
            },
            "tags": {"type": "array", "items": {"type": "string"}},
            "note": {"type": "string", "description": "What changed in this version."},
        },
        "required": ["paths"],
        "additionalProperties": False,
    },
}


class PluginContext(Protocol):
    """The part of Hermes's ``PluginContext`` this plugin registers through."""

    def register_tool(  # noqa: D102, PLR0913 - Hermes's signature, as far as the plugin uses it
        self,
        name: str,
        toolset: str,
        *,
        schema: dict[str, Any],
        handler: Callable[..., str],
        check_fn: Callable[[], bool],
        emoji: str,
    ) -> object: ...

    def register_skill(self, name: str, path: Path, *, description: str) -> object: ...  # noqa: D102

    def register_command(  # noqa: D102
        self, name: str, handler: Callable[[str], str], *, description: str, args_hint: str
    ) -> object: ...


def _cwd() -> Path:
    """The agent's working directory, which relative paths mean in a session.

    Hermes keeps it apart from the process's own; outside Hermes (the admission probe, the specs)
    the process's directory is the only one there is.
    """
    try:
        from agent.runtime_cwd import (  # type: ignore[import-not-found]  # noqa: PLC0415
            resolve_agent_cwd,  # only inside Hermes
        )
    except ImportError:
        return Path.cwd()
    return Path(resolve_agent_cwd())


def publish(args: dict[str, Any], **_context: object) -> str:
    """The tool: upload ``paths`` and return the link, or the reason it was refused."""
    try:
        files = collect(args["paths"], _cwd())
        result = DropClient(Endpoint.from_env()).publish(
            files,
            title=args.get("title"),
            artifact_id=args.get("id"),
            entry=args.get("entry"),
            expires_in=args.get("expires_in"),
            tags=args.get("tags") or (),
            note=args.get("note"),
            merge=bool(args.get("merge")),
        )
    except DropError as error:
        # A refusal is a tool result the agent can act on, never an exception in its loop.
        return json.dumps({"success": False, "error": str(error)})
    return json.dumps({"success": True, "visibility": "private", "files": len(files), **result})


def configured() -> bool:
    """Offer the tool only once Hermes has the server's address and a token."""
    return bool(os.environ.get("DROP_URL") and os.environ.get("DROP_TOKEN"))


def share(raw_args: str) -> str:
    """``/share <path>...``: a private link to the files, without a turn of the agent."""
    paths = shlex.split(raw_args)
    if not paths:
        return "usage: /share <path> [<path>...]  (a private link; ask the agent to make it public)"
    try:
        result = DropClient(Endpoint.from_env()).publish(collect(paths, _cwd()))
    except DropError as error:
        return str(error)
    return f"{result['url']}  (private, version {result['version']}, id {result['id']})"


def artifacts(raw_args: str) -> str:
    """``/artifacts [query]``: the newest artifacts; links are minted on purpose, so none here."""
    try:
        rows = DropClient(Endpoint.from_env()).list(raw_args.strip() or None, LIST_LIMIT)
    except DropError as error:
        return str(error)
    if not rows:
        return "no artifacts"
    return "\n".join(
        f"{row['id']}  {row['visibility']:<7}  v{row['version']}  {row['title']}" for row in rows
    )


def register(ctx: PluginContext) -> None:
    """Hermes's entry point: one tool, one skill, two commands."""
    ctx.register_tool(
        name=TOOL,
        toolset="drop",
        schema=PUBLISH_SCHEMA,
        handler=publish,
        check_fn=configured,
        emoji="📎",
    )
    ctx.register_skill(
        "drop",
        Path(__file__).parent / "SKILL.md",
        description="When to share output as a drop link, and how to keep it private",
    )
    ctx.register_command(
        "share", share, description="Upload files to drop (private link)", args_hint="<path>..."
    )
    ctx.register_command(
        "artifacts", artifacts, description="List recent drop artifacts", args_hint="[query]"
    )
