"""The package surface: an importable, typed package and its ``mcp-vitrine`` entry point."""

import runpy
import sys
import tomllib
from pathlib import Path

import pytest

import mcp_vitrine
import mcp_vitrine.cli
import mcp_vitrine.domain
import mcp_vitrine.mcp
import mcp_vitrine.services
import mcp_vitrine.storage
import mcp_vitrine.web

ROOT = Path(__file__).parents[2]


class DescribeThePackage:
    class WhenItIsImported:
        def it_exposes_the_version_pyproject_declares(self) -> None:
            project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
            assert mcp_vitrine.__version__ == project["project"]["version"]

        def it_imports_every_layer_the_layering_contract_names(self) -> None:
            layers = (
                mcp_vitrine.domain,
                mcp_vitrine.storage,
                mcp_vitrine.services,
                mcp_vitrine.mcp,
                mcp_vitrine.web,
            )
            assert [layer.__name__ for layer in layers] == [
                "mcp_vitrine.domain",
                "mcp_vitrine.storage",
                "mcp_vitrine.services",
                "mcp_vitrine.mcp",
                "mcp_vitrine.web",
            ]


class DescribeTheCommandLine:
    class WhenNoCommandIsGiven:
        def it_exits_as_a_usage_error(self) -> None:
            with pytest.raises(SystemExit) as exit_info:
                mcp_vitrine.cli.main([])
            assert exit_info.value.code == 2

    class WhenItIsRunAsAModule:
        def it_runs_the_same_commands(
            self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
        ) -> None:
            monkeypatch.setattr(sys, "argv", ["mcp-vitrine", "--version"])
            with pytest.raises(SystemExit):
                runpy.run_module("mcp_vitrine", run_name="__main__")
            assert capsys.readouterr().out == f"mcp-vitrine {mcp_vitrine.__version__}\n"
