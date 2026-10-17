"""``RateLimiter``: in-memory token buckets per client and per source IP (spec § 7.5)."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from mcp_vitrine.config import Settings, TickRate
from mcp_vitrine.domain.errors import RateLimited
from mcp_vitrine.services.metrics import Metrics, NoMetrics

REJECTED_RATE = "rate_limited"
# How often idle state is looked for; a full bucket is the same as none, so dropping it is exact.
PRUNE_EVERY = 60.0


class Bucket(StrEnum):
    """The buckets of spec § 7.5."""

    WRITES = "writes"
    LINKS = "links"
    READS = "reads"
    VIEWS = "views"
    UNLOCK = "unlock"
    UNLOCK_LINK = "unlock_link"


@dataclass(slots=True)
class _Tokens:
    """One bucket's state: what is left and when it was last touched."""

    tokens: float
    at: float


class RateLimiter:
    """Token buckets in memory; counters reset on restart, which a single process allows."""

    def __init__(
        self,
        rates: Mapping[Bucket, TickRate | None],
        *,
        clock: Callable[[], float] = time.monotonic,
        metrics: Metrics | None = None,
    ) -> None:
        """Take one rate per bucket; ``None`` disables a bucket."""
        self._rates = dict(rates)
        self._clock = clock
        self._metrics = metrics or NoMetrics()
        self._state: dict[tuple[Bucket, str], _Tokens] = {}
        self._lock = threading.Lock()
        self._pruned_at = clock()

    @classmethod
    def from_settings(cls, settings: Settings, *, metrics: Metrics | None = None) -> RateLimiter:
        """Take the bucket rates from the effective configuration."""
        return cls(
            {
                Bucket.WRITES: settings.rate_writes,
                Bucket.LINKS: settings.rate_links,
                Bucket.READS: settings.rate_reads,
                Bucket.VIEWS: settings.rate_views,
                Bucket.UNLOCK: settings.rate_unlock,
                Bucket.UNLOCK_LINK: settings.rate_unlock_link,
            },
            metrics=metrics,
        )

    def check(self, bucket: Bucket, key: str) -> None:
        """Spend one token for ``key``, or raise ``RateLimited`` carrying the wait.

        A caller checks before doing any work, which is what makes a rejected call write nothing.
        """
        rate = self._rates.get(bucket)
        if rate is None:
            return
        with self._lock:
            self._prune()
            state = self._refill(bucket, key, rate)
            if state.tokens < 1:
                # Counted here, not where it is audited: a refused view is counted but not audited.
                self._metrics.rejected(REJECTED_RATE)
                raise RateLimited(self._retry_after(state, rate))
            state.tokens -= 1

    def refund(self, bucket: Bucket, key: str) -> None:
        """Give back the token a call spent, for a bucket that counts failures only (§ 7.5).

        Spending first and refunding a success keeps the check atomic: two wrong passcodes sent at
        once cannot both pass on the bucket's last token.
        """
        rate = self._rates.get(bucket)
        if rate is None:
            return
        with self._lock:
            state = self._refill(bucket, key, rate)
            state.tokens = min(float(rate.count), state.tokens + 1)

    def tracked(self) -> int:
        """How many keys hold state, which pruning keeps to the ones in use."""
        with self._lock:
            return len(self._state)

    def _prune(self) -> None:
        """Drop buckets that have refilled to full: an absent bucket starts full, so it is exact.

        Without this, the per-IP and per-link state grows with every address that ever called.
        The caller holds the lock.
        """
        now = self._clock()
        if now - self._pruned_at < PRUNE_EVERY:
            return
        self._pruned_at = now
        for key, state in list(self._state.items()):
            rate = self._rates.get(key[0])
            if rate is None or state.tokens + (now - state.at) * rate.per_second() >= rate.count:
                del self._state[key]

    def _refill(self, bucket: Bucket, key: str, rate: TickRate) -> _Tokens:
        """Bring one bucket up to date, then hand it back."""
        now = self._clock()
        state = self._state.get((bucket, key))
        if state is None:
            state = _Tokens(tokens=float(rate.count), at=now)
            self._state[(bucket, key)] = state
        state.tokens = min(float(rate.count), state.tokens + (now - state.at) * rate.per_second())
        state.at = now
        return state

    def _retry_after(self, state: _Tokens, rate: TickRate) -> int:
        """Whole seconds until the bucket holds one token again, never zero."""
        return max(1, math.ceil((1 - state.tokens) / rate.per_second()))
