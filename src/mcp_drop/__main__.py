"""``python -m mcp_drop``: the same entry point as the ``mcp-drop`` console script."""

from __future__ import annotations

from mcp_drop.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
