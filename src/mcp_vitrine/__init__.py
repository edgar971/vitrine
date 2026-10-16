"""vitrine: shareable, versioned artifacts for AI agents."""

from importlib.metadata import version

# pyproject.toml is the one place the version is written; everything else reads it from there.
__version__ = version("mcp-vitrine")
