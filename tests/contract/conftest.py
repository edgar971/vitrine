"""The ``Store`` implementations the contract suite runs against."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from mcp_drop.domain.clock import Clock
from mcp_drop.storage.base import Store, StoreConfig
from mcp_drop.storage.filesystem import FileStore

# Adding a Store implementation is one line: the suite then runs against all of them.
STORE_FACTORIES: dict[str, Callable[[StoreConfig, Clock], Store]] = {"filesystem": FileStore}


@pytest.fixture(params=sorted(STORE_FACTORIES))
def store_factory(request: pytest.FixtureRequest) -> Callable[[StoreConfig, Clock], Store]:
    """The factory of one implementation."""
    return STORE_FACTORIES[request.param]


@pytest.fixture
def store(
    store_factory: Callable[[StoreConfig, Clock], Store], store_config: StoreConfig, clock: Clock
) -> Store:
    """A fresh store of one implementation, rooted in the test's temporary directory."""
    return store_factory(store_config, clock)


@pytest.fixture
def limited_store(
    store_factory: Callable[[StoreConfig, Clock], Store], store_config: StoreConfig, clock: Clock
) -> Store:
    """A store with one small version allowed, so the limits are reachable."""
    return store_factory(
        store_config.model_copy(update={"quota": 12, "max_artifact": 8, "max_files": 2}), clock
    )


@pytest.fixture
def merge_quota_store(
    store_factory: Callable[[StoreConfig, Clock], Store], store_config: StoreConfig, clock: Clock
) -> Store:
    """A store whose quota sits between a merged version and the bytes the merge adds."""
    return store_factory(store_config.model_copy(update={"quota": 9, "max_artifact": 12}), clock)


@pytest.fixture
def two_version_store(
    store_factory: Callable[[StoreConfig, Clock], Store], store_config: StoreConfig, clock: Clock
) -> Store:
    """A store that keeps two versions, so pruning is reachable."""
    return store_factory(store_config.model_copy(update={"retain_versions": 2}), clock)
