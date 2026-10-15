"""The ``mcp-drop`` entry point. The commands arrive with M2-M4 (``docs/roadmap.md``)."""

from __future__ import annotations

import sys

_NOT_BUILT = (
    "drop has a core but nothing serves yet: config, storage and the services are built (M1), "
    "and the MCP and web layers arrive with M2-M4, so there is no CLI command yet. The build "
    "follows docs/roadmap.md from M2."
)


def main() -> int:
    """Report that there is nothing to serve yet and fail."""
    print(_NOT_BUILT, file=sys.stderr)
    return 1
