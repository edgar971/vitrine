"""The clock seam, so a service never reads the wall clock directly and a spec can freeze time."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """A source of the current time, always UTC."""

    def now(self) -> datetime:
        """The current instant."""
        ...


class SystemClock:
    """The wall clock at whole-second precision, which is ``meta.json``'s (spec § 4.2)."""

    def now(self) -> datetime:
        """The current second, in UTC."""
        return datetime.now(UTC).replace(microsecond=0)
