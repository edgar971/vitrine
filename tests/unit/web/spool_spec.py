"""The spool's streaming costs: batched thread hops and a throttled freshness stamp (§ 7.3)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mcp_vitrine.web.spool import BATCH_BYTES, TOUCH_EVERY, Spool, batches


async def _chunks(*sizes: int) -> AsyncIterator[bytes]:
    """Network chunks of the given sizes."""
    for size in sizes:
        yield b"x" * size


async def _gathered(*sizes: int) -> list[int]:
    """The batch sizes those chunks are handed to the worker thread in."""
    return [len(batch) async for batch in batches(_chunks(*sizes))]


@pytest.fixture
def stamps(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every directory the spool refreshed the timestamp of."""
    touched: list[Path] = []
    monkeypatch.setattr("os.utime", touched.append)
    return touched


@pytest.fixture
def clock() -> list[float]:
    """A monotonic clock the spec moves: ``clock[0]`` is now."""
    return [1000.0]


@pytest.fixture
def pool(tmp_path: Path, clock: list[float]) -> Spool:
    """A spool on the spec's clock."""
    return Spool(tmp_path, clock=lambda: clock[0])


class DescribeBatches:
    class WhenManySmallChunksArrive:
        async def it_hands_them_over_in_few_batches(self) -> None:
            sizes = await _gathered(*[4096] * 128)
            assert sizes == [BATCH_BYTES, BATCH_BYTES]

    class WhenTheBodyEndsMidBatch:
        async def it_hands_over_the_rest(self) -> None:
            assert await _gathered(10, 20) == [30]

    class WhenTheBodyIsEmpty:
        async def it_hands_over_nothing(self) -> None:
            assert await _gathered() == []


class DescribeAppending:
    class WhenChunksArriveWithinTheInterval:
        def it_refreshes_the_spool_at_most_once(
            self, tmp_path: Path, pool: Spool, stamps: list[Path], clock: list[float]
        ) -> None:
            with (tmp_path / "part").open("wb") as sink:
                clock[0] += TOUCH_EVERY
                pool.append(sink, b"a")
                pool.append(sink, b"b")
            assert stamps == [tmp_path]

    class WhenTheIntervalPasses:
        def it_refreshes_the_spool_again(
            self, tmp_path: Path, pool: Spool, stamps: list[Path], clock: list[float]
        ) -> None:
            with (tmp_path / "part").open("wb") as sink:
                clock[0] += TOUCH_EVERY
                pool.append(sink, b"a")
                clock[0] += TOUCH_EVERY
                pool.append(sink, b"b")
            assert len(stamps) == 2

    class WhenAChunkIsAppended:
        def it_writes_every_byte(self, tmp_path: Path, pool: Spool, stamps: list[Path]) -> None:
            with (tmp_path / "part").open("wb") as sink:
                pool.append(sink, b"ab")
                pool.append(sink, b"cd")
            assert (tmp_path / "part").read_bytes() == b"abcd"
