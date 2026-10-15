"""CLI givens: run ``mcp-drop`` in-process and read back what it printed."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from mcp_drop.cli import main


@dataclass(frozen=True, slots=True)
class Run:
    """One finished command: its exit status and what it printed."""

    status: int
    out: str
    err: str


type Runner = Callable[..., Run]


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Runner:
    """Run ``mcp-drop <args>`` and capture its result."""

    def invoke(*args: str) -> Run:
        status = main(list(args))
        captured = capsys.readouterr()
        return Run(status=status, out=captured.out, err=captured.err)

    return invoke
