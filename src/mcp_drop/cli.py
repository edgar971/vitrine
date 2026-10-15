"""The ``mcp-drop`` entry point. The commands arrive with M4 (``docs/roadmap.md``)."""

from __future__ import annotations

import sys

_NOT_BUILT = (
    "drop has a core, an MCP layer and a web layer, but no CLI yet: config, storage and the "
    "services (M1), the MCP tools, resources, prompt and public gate (M2) and the views, uploads, "
    "API and middleware assembled by mcp_drop.app.build_app (M3) are built, while `serve` and "
    "the other commands arrive with M4. The build follows docs/roadmap.md from M4."
)


def main() -> int:
    """Report that there is nothing to serve yet and fail."""
    print(_NOT_BUILT, file=sys.stderr)
    return 1
