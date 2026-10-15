"""The package surface: an importable, typed package and its ``mcp-drop`` entry point."""

import runpy
import sys

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
    class WhenNoCommandIsGiven:
        def it_exits_as_a_usage_error(self) -> None:
            with pytest.raises(SystemExit) as exit_info:
                mcp_drop.cli.main([])
            assert exit_info.value.code == 2

    class WhenItIsRunAsAModule:
        def it_runs_the_same_commands(
            self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
        ) -> None:
            monkeypatch.setattr(sys, "argv", ["mcp-drop", "--version"])
            with pytest.raises(SystemExit):
                runpy.run_module("mcp_drop", run_name="__main__")
            assert capsys.readouterr().out == f"mcp-drop {mcp_drop.__version__}\n"
