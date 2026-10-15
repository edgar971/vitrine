"""drop: publish local files to the drop artifact service (MCP server: mcp_servers.drop)."""
from __future__ import annotations

import json
import os
from pathlib import Path

PUBLISH_SCHEMA = {
    "name": "artifact_publish_files",
    "description": (
        "Upload local files or one folder to drop and return a private link. Use for files already on "
        "disk, binaries (PNG, PDF) or multi-file HTML. Pass `id` to publish a new version at the same "
        "link. Visibility is always private here; use mcp_drop_artifact_set_visibility to go public."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "title": {"type": "string"},
            "id": {"type": "string", "description": "Existing artifact id to update in place."},
            "entry": {"type": "string", "description": "Entry file; defaults to index.html or the only file."},
            "expires_in": {"type": "string", "description": "e.g. 1h, 7d"},
        },
        "required": ["paths"],
    },
}


def _publish(params: dict, **_kwargs) -> str:
    from .client import publish_paths  # lazy: keep register() import-light
    try:
        return json.dumps({"success": True, **publish_paths(**params)})
    except Exception as exc:  # surface as a tool result, never crash the agent loop
        return json.dumps({"success": False, "error": str(exc)})


def _check() -> bool:
    return bool(os.environ.get("DROP_URL") and os.environ.get("DROP_TOKEN"))


def _share(raw_args: str) -> str:
    from .client import publish_paths
    args = raw_args.split()
    if not args:
        return "usage: /share <path> [<path>...]   (private link; go public via the agent, which asks you)"
    return publish_paths(paths=args)["url"]


def _artifacts(raw_args: str) -> str:
    from .client import list_artifacts
    rows = list_artifacts(query=raw_args.strip() or None)
    return "\n".join(f"{r['title']}  {r['visibility']}  {r['url']}" for r in rows) or "no artifacts"


def register(ctx) -> None:
    ctx.register_tool(name="artifact_publish_files", toolset="drop", schema=PUBLISH_SCHEMA,
                      handler=_publish, check_fn=_check, emoji="📎")
    ctx.register_skill("drop", Path(__file__).parent / "SKILL.md",
                       description="When and how to share files as drop links")
    ctx.register_command("share", _share, description="Upload files to drop (private link)",
                         args_hint="<path> [<path>...]")
    ctx.register_command("artifacts", _artifacts, description="List recent drop artifacts",
                         args_hint="[query]")
