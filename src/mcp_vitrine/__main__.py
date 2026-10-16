"""``python -m mcp_vitrine``: the same entry point as the ``mcp-vitrine`` console script."""

from __future__ import annotations

from mcp_vitrine.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
