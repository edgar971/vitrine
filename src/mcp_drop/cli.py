"""The ``mcp-drop`` entry point. The commands arrive with M3-M4 (``docs/roadmap.md``)."""

from __future__ import annotations

import sys

_NOT_BUILT = (
    "drop has a core and an MCP layer but nothing serves yet: config, storage, the services (M1) "
    "and the MCP tools, resources, prompt and public gate (M2) are built, while the web layer "
    "and the CLI arrive with M3-M4, so there is no CLI command yet. The build follows "
    "docs/roadmap.md from M3."
)


def main() -> int:
    """Report that there is nothing to serve yet and fail."""
    print(_NOT_BUILT, file=sys.stderr)
    return 1
