"""``docs/security.md`` § 6: every ``AGENTS.md`` invariant names specs, and those specs exist."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
AGENTS = ROOT / "AGENTS.md"
SECURITY = ROOT / "docs" / "security.md"
ROW = re.compile(r"^\| (?P<invariant>.+?) \| `(?P<spec>tests/[^`]+)` \|$", re.MULTILINE)
BOLD_LEAD = re.compile(r"^- \*\*(?P<lead>.+?)[.:]?\*\*", re.MULTILINE)


def invariants() -> set[str]:
    """The bold lead of each bullet under ``AGENTS.md``'s invariants heading."""
    text = AGENTS.read_text(encoding="utf-8")
    section = text.split("## Invariants", 1)[1].split("\n## ", 1)[0]
    return {match["lead"] for match in BOLD_LEAD.finditer(section)}


def table() -> list[tuple[str, str]]:
    """The ``(invariant, spec node id)`` rows of the security doc's table."""
    text = SECURITY.read_text(encoding="utf-8")
    return [(match["invariant"], match["spec"]) for match in ROW.finditer(text)]


def defines(node_id: str) -> bool:
    """Whether ``file::Class::...::it_name`` is a chain of definitions in that file."""
    path, *names = node_id.split("::")
    scope: list[ast.stmt] = ast.parse((ROOT / path).read_text(encoding="utf-8")).body
    for name in names:
        found = [
            node
            for node in scope
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == name
        ]
        if not found:
            return False
        scope = found[0].body
    return True


class DescribeTheInvariantTable:
    def it_covers_every_invariant(self) -> None:
        assert {invariant for invariant, _ in table()} == invariants()

    @pytest.mark.parametrize("spec", [spec for _, spec in table()])
    def it_names_a_spec_that_exists(self, spec: str) -> None:
        assert defines(spec), f"{spec} is listed in docs/security.md § 6 but not defined"
