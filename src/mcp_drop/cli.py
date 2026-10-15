"""The ``mcp-drop`` entry point. The commands arrive with M1-M4 (``docs/roadmap.md``)."""

from __future__ import annotations

import sys

_NOT_BUILT = (
    "drop is designed, not built: M0 ships the toolchain only, so there is no server or CLI "
    "command yet. The build follows docs/roadmap.md from M1."
)


def main() -> int:
    """Report that the package has no behaviour yet and fail."""
    print(_NOT_BUILT, file=sys.stderr)
    return 1
