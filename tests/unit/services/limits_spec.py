"""``RateLimiter``: the buckets of spec § 7.5."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from mcp_vitrine.config import Settings, parse_rate
from mcp_vitrine.domain.errors import RateLimited
from mcp_vitrine.services.limits import Bucket, RateLimiter

CLIENT = "hermes"


class FakeMonotonic:
    """A monotonic clock the spec moves, so refill is exact."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """The current reading."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the reading forward."""
        self.now += seconds


@pytest.fixture
def monotonic() -> FakeMonotonic:
    """The monotonic clock the limiter is built on."""
    return FakeMonotonic()


@pytest.fixture
def limiter(monotonic: FakeMonotonic) -> RateLimiter:
    """A limiter with the documented defaults, on the fake clock."""
    return RateLimiter(
        {
            Bucket.WRITES: parse_rate("60/m"),
            Bucket.LINKS: parse_rate("120/m"),
            Bucket.READS: parse_rate("600/m"),
            Bucket.VIEWS: parse_rate("1200/m"),
        },
        clock=monotonic,
    )


class DescribeSpendingTokens:
    class WhenTheBucketHasTokens:
        def it_allows_every_call_up_to_the_bucket_size(self, limiter: RateLimiter) -> None:
            for _ in range(60):
                limiter.check(Bucket.WRITES, CLIENT)

    class WhenTheBucketIsEmpty:
        def it_refuses_the_call(self, limiter: RateLimiter) -> None:
            for _ in range(60):
                limiter.check(Bucket.WRITES, CLIENT)
            with pytest.raises(RateLimited):
                limiter.check(Bucket.WRITES, CLIENT)

        def it_says_how_long_to_wait(self, limiter: RateLimiter) -> None:
            for _ in range(60):
                limiter.check(Bucket.WRITES, CLIENT)
            with pytest.raises(RateLimited) as refusal:
                limiter.check(Bucket.WRITES, CLIENT)
            assert refusal.value.retry_after == 1

        def it_says_so_in_the_message(self, limiter: RateLimiter) -> None:
            for _ in range(60):
                limiter.check(Bucket.WRITES, CLIENT)
            with pytest.raises(RateLimited, match="retry after 1s"):
                limiter.check(Bucket.WRITES, CLIENT)

    class WhenTimePasses:
        def it_refills_at_the_configured_rate(
            self, limiter: RateLimiter, monotonic: FakeMonotonic
        ) -> None:
            for _ in range(60):
                limiter.check(Bucket.WRITES, CLIENT)
            monotonic.advance(1)
            limiter.check(Bucket.WRITES, CLIENT)

        def it_does_not_refill_beyond_the_bucket(
            self, limiter: RateLimiter, monotonic: FakeMonotonic
        ) -> None:
            monotonic.advance(60)
            for _ in range(60):
                limiter.check(Bucket.WRITES, CLIENT)
            with pytest.raises(RateLimited):
                limiter.check(Bucket.WRITES, CLIENT)

    class WhenAClientExhaustsItsBucket:
        def it_leaves_the_other_clients_alone(self, limiter: RateLimiter) -> None:
            for _ in range(60):
                limiter.check(Bucket.WRITES, CLIENT)
            limiter.check(Bucket.WRITES, "ci-bot")

        def it_leaves_the_other_buckets_alone(self, limiter: RateLimiter) -> None:
            for _ in range(60):
                limiter.check(Bucket.WRITES, CLIENT)
            limiter.check(Bucket.READS, CLIENT)

    class WhenABucketIsOff:
        def it_never_refuses(self, monotonic: FakeMonotonic) -> None:
            limiter = RateLimiter({Bucket.WRITES: None}, clock=monotonic)
            for _ in range(1000):
                limiter.check(Bucket.WRITES, CLIENT)


class DescribeConcurrency:
    class WhenManyCallsArriveAtOnce:
        def it_grants_exactly_the_bucket_size(self, monotonic: FakeMonotonic) -> None:
            limiter = RateLimiter({Bucket.WRITES: parse_rate("10/m")}, clock=monotonic)

            def call(_: int) -> bool:
                try:
                    limiter.check(Bucket.WRITES, CLIENT)
                except RateLimited:
                    return False
                return True

            with ThreadPoolExecutor(max_workers=16) as pool:
                granted = list(pool.map(call, range(50)))
            assert granted.count(True) == 10


class DescribeBuildingFromSettings:
    class WhenTheLimiterIsBuiltFromSettings:
        def it_takes_the_configured_rates(self, settings: Settings) -> None:
            limiter = RateLimiter.from_settings(settings)
            for _ in range(1200):
                limiter.check(Bucket.VIEWS, "10.0.0.1")
            with pytest.raises(RateLimited):
                limiter.check(Bucket.VIEWS, "10.0.0.1")
