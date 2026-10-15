"""``docs/configuration.md``: generated from ``Settings``, and committed in step with it."""

from __future__ import annotations

from pathlib import Path

from mcp_drop.config import Settings
from mcp_drop.reference import GROUPS, group_of, render

DOC = Path(__file__).parents[2] / "docs" / "configuration.md"


class DescribeTheConfigurationReference:
    class WhenItIsRendered:
        def it_matches_the_committed_doc(self) -> None:
            assert DOC.read_text(encoding="utf-8") == render(), "run `just docs`"

        def it_lists_every_setting(self) -> None:
            assert all(f"| `{name}` |" in render() for name in Settings.model_fields)

    class WhenASettingIsDeclared:
        def it_names_a_known_group(self) -> None:
            assert {group_of(field) for field in Settings.model_fields.values()} <= set(GROUPS)

        def it_carries_a_description(self) -> None:
            assert all(field.description for field in Settings.model_fields.values())
