"""Shared givens: a clock the specs move by hand and a valid environment."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mcp_drop.config import Settings, load_settings
from mcp_drop.storage.base import StoreConfig

SECRET = "0123456789abcdef0123456789abcdef"
TOKEN_HASH = "a" * 64


class FrozenClock:
    """A clock the specs move by hand, so expiry and refill are exact."""

    def __init__(self, start: datetime) -> None:
        """Start at a fixed instant."""
        self._now = start

    def now(self) -> datetime:
        """The frozen instant, to the second."""
        return self._now

    def advance(self, delta: timedelta) -> None:
        """Move time forward."""
        self._now += delta


@pytest.fixture
def clock() -> FrozenClock:
    """A clock the spec controls."""
    return FrozenClock(datetime(2026, 10, 15, 10, 0, tzinfo=UTC))


@pytest.fixture
def settings(environment: None) -> Settings:
    """The effective settings of a valid environment."""
    return load_settings()


@pytest.fixture
def store_config(tmp_path: Path) -> StoreConfig:
    """A store rooted in the test's temporary directory."""
    return StoreConfig(root=tmp_path / "data")


@pytest.fixture
def advance(clock: FrozenClock) -> Callable[[timedelta], None]:
    """Move the frozen clock forward, so expiry, refill and retention are exact."""
    return clock.advance


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A valid environment, with any ambient ``DROP_*`` key removed first."""
    for key in list(os.environ):
        if key.startswith("DROP_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("DROP_SECRET", SECRET)
    monkeypatch.setenv("DROP_TOKENS", f"hermes:{TOKEN_HASH}:read+write+share+publish")
    monkeypatch.setenv("DROP_DATA_DIR", str(tmp_path / "data"))
