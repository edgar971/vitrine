"""The M0 surface: an importable, typed package with no behaviour yet."""

import runpy

import pytest

import mcp_drop
import mcp_drop.cli
import mcp_drop.domain
import mcp_drop.mcp
import mcp_drop.services
import mcp_drop.storage
import mcp_drop.web


class DescribeThePackage:
    class WhenItIsImported:
        def it_exposes_the_version_pyproject_declares(self) -> None:
            assert mcp_drop.__version__ == "0.1.0"

        def it_imports_every_layer_the_layering_contract_names(self) -> None:
            layers = (
                mcp_drop.domain,
                mcp_drop.storage,
                mcp_drop.services,
                mcp_drop.mcp,
                mcp_drop.web,
            )
            assert [layer.__name__ for layer in layers] == [
                "mcp_drop.domain",
                "mcp_drop.storage",
                "mcp_drop.services",
                "mcp_drop.mcp",
                "mcp_drop.web",
            ]


class DescribeTheCommandLine:
    class WhenNothingIsBuiltYet:
        def it_exits_nonzero(self) -> None:
            assert mcp_drop.cli.main() == 1

        def it_points_at_the_roadmap(self, capsys: pytest.CaptureFixture[str]) -> None:
            mcp_drop.cli.main()
            assert "docs/roadmap.md" in capsys.readouterr().err

    class WhenItIsRunAsAModule:
        def it_exits_with_the_same_status(self) -> None:
            with pytest.raises(SystemExit) as exit_info:
                runpy.run_module("mcp_drop", run_name="__main__")
            assert exit_info.value.code == 1
