"""``ViewLog``: anonymous view counts per artifact, link and hour (spec § 11.4).

Views are telemetry, not audit: counters live in memory and are appended to
``views/<YYYY-MM>.jsonl`` every minute and at shutdown, without an fsync, so a flood costs one
counter, not one disk write per request. No IP address or user agent ever reaches disk: a visitor
is an HMAC under a key that lives in memory only and changes every hour, and only the number of
distinct values is written.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from mcp_vitrine.domain import layout
from mcp_vitrine.domain.clock import Clock
from mcp_vitrine.domain.models import LinkViews, Views

# Link unfurlers: a chat showing a preview is worth knowing, and is not someone opening the link.
UNFURLERS = (
    "slackbot",
    "discordbot",
    "telegrambot",
    "whatsapp",
    "twitterbot",
    "facebookexternalhit",
    "linkedinbot",
)
KEY_BYTES = 32
VISITOR_BYTES = 8

logger = logging.getLogger(__name__)


class ViewLine(BaseModel):
    """One line of ``views/<YYYY-MM>.jsonl``: counts added since the previous line for this key.

    The lines of one key add up to its totals; ids and counts only, never a viewer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hour: AwareDatetime
    artifact: str
    route: str
    lid: str | None = None
    views: int = Field(default=0, ge=0)
    visitors: int = Field(default=0, ge=0)
    bots: int = Field(default=0, ge=0)


class HourViews(BaseModel):
    """One hour of an artifact's views, every route and link together."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hour: AwareDatetime
    views: int = Field(ge=0)
    visitors: int = Field(ge=0)
    bots: int = Field(ge=0)


type _Key = tuple[datetime, str, str, str | None]


@dataclass(slots=True)
class _Counter:
    """One key's counts not yet on disk, and the hour's distinct visitors seen so far."""

    views: int = 0
    bots: int = 0
    seen: set[bytes] = field(default_factory=set)
    written_visitors: int = 0

    def written(self, line: ViewLine) -> None:
        """Take what a flush wrote off the pending counts; views since then stay pending."""
        self.views -= line.views
        self.bots -= line.bots
        self.written_visitors += line.visitors

    def pending(self, key: _Key) -> ViewLine | None:
        """What a flush would append for this key, or ``None`` when nothing changed."""
        visitors = len(self.seen) - self.written_visitors
        if not (self.views or self.bots or visitors):
            return None
        hour, artifact, route, lid = key
        return ViewLine(
            hour=hour,
            artifact=artifact,
            route=route,
            lid=lid,
            views=self.views,
            visitors=visitors,
            bots=self.bots,
        )


class ViewLog:
    """Counts views in memory, appends them to the month's file, and reads them back."""

    def __init__(self, root: Path, *, enabled: bool, retention: timedelta, clock: Clock) -> None:
        """Count under ``root/views`` when ``enabled``; keep each month for ``retention``."""
        self._data_dir = root
        self._root = root / layout.VIEWS_DIR
        self._enabled = enabled
        self._retention = retention
        self._clock = clock
        self._counters: dict[_Key, _Counter] = {}
        self._hour = _hour_of(clock.now())
        self._key = secrets.token_bytes(KEY_BYTES)
        # ``_lock`` guards the counters and is held for microseconds, by every view; ``_flushing``
        # keeps a flush and a read of the files apart, and is never taken by a view.
        self._lock = threading.Lock()
        self._flushing = threading.Lock()

    @property
    def enabled(self) -> bool:
        """Whether views are counted (``view_log = counts``)."""
        return self._enabled

    def record(
        self, artifact: str, *, route: str, lid: str | None, address: str, user_agent: str
    ) -> None:
        """Count one view of ``artifact``; the address and agent are only ever hashed."""
        if not self._enabled:
            return
        with self._lock:
            hour = self._rotate()
            counter = self._counters.setdefault((hour, artifact, route, lid), _Counter())
            if is_unfurler(user_agent):
                counter.bots += 1
                return
            counter.views += 1
            counter.seen.add(self._visitor(address, user_agent))

    def flush(self) -> int:
        """Append what changed since the last flush; how many lines were written.

        A crash loses what was not flushed yet, at most a minute of counts (spec § 11.4).
        """
        if not self._enabled:
            return 0
        with self._flushing:
            with self._lock:
                self._rotate()
                pending = {
                    key: line
                    for key, counter in self._counters.items()
                    if (line := counter.pending(key))
                }
            if not pending:
                return 0
            # Written outside ``_lock``, so a view arriving meanwhile never waits on the disk.
            self._append(pending.values())
            with self._lock:
                for key, line in pending.items():
                    self._counters[key].written(line)
                # A past hour's visitors can no longer grow: drop its set, its count is on disk.
                self._counters = {
                    key: counter
                    for key, counter in self._counters.items()
                    if key[0] == self._hour or counter.pending(key)
                }
            return len(pending)

    def totals(self, artifact: str, *, labels: Mapping[str, str]) -> Views | None:
        """An artifact's totals and its per-link counts; ``None`` while counting is off."""
        if not self._enabled:
            return None
        lines = list(self.lines(artifact=artifact))
        by_link: dict[str, list[ViewLine]] = {}
        for line in lines:
            if line.lid is not None:
                by_link.setdefault(line.lid, []).append(line)
        first, last = _span(lines)
        return Views(
            total=sum(line.views for line in lines),
            visitors=sum(line.visitors for line in lines),
            bots=sum(line.bots for line in lines),
            first_viewed_at=first,
            last_viewed_at=last,
            links=tuple(
                LinkViews(
                    lid=lid,
                    label=labels.get(lid, ""),
                    views=sum(line.views for line in group),
                    first_viewed_at=_span(group)[0],
                    last_viewed_at=_span(group)[1],
                )
                for lid, group in sorted(by_link.items())
            ),
        )

    def series(self, artifact: str, *, since: datetime) -> tuple[HourViews, ...]:
        """An artifact's views hour by hour from ``since``, oldest first; empty hours left out."""
        hours: dict[datetime, list[int]] = {}
        for line in self.lines(artifact=artifact, since=since):
            counts = hours.setdefault(line.hour, [0, 0, 0])
            counts[0] += line.views
            counts[1] += line.visitors
            counts[2] += line.bots
        return tuple(
            HourViews(hour=hour, views=views, visitors=visitors, bots=bots)
            for hour, (views, visitors, bots) in sorted(hours.items())
        )

    def lines(
        self, *, artifact: str | None = None, since: datetime | None = None
    ) -> Iterator[ViewLine]:
        """Every count on disk and not flushed yet that matches, in no particular order.

        A flush cannot run in between, so no count is read twice or missed; a view being
        recorded meanwhile does not wait, since the disk is read outside ``_lock``.
        """
        with self._flushing:
            on_disk = self._read_disk()
            with self._lock:
                found = [*on_disk, *self._unflushed()]
        for line in found:
            if (artifact is None or line.artifact == artifact) and (
                since is None or line.hour >= _hour_of(since)
            ):
                yield line

    def sweep(self) -> int:
        """Delete whole months older than ``view_retention``; how many went."""
        cutoff = self._clock.now() - self._retention
        removed = 0
        for path in sorted(self._root.glob("*.jsonl")):
            month = _month_of(path)
            if month is not None and month < (cutoff.year, cutoff.month):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def _rotate(self) -> datetime:
        """The current hour; a new hour gets a new visitor key. The caller holds the lock."""
        hour = _hour_of(self._clock.now())
        if hour != self._hour:
            self._hour = hour
            self._key = secrets.token_bytes(KEY_BYTES)
        return hour

    def _visitor(self, address: str, user_agent: str) -> bytes:
        """``HMAC(hour_key, ip | user_agent)``: the same viewer within the hour, nobody after."""
        message = f"{address}|{user_agent}".encode()
        return hmac.new(self._key, message, hashlib.sha256).digest()[:VISITOR_BYTES]

    def _append(self, lines: Iterable[ViewLine]) -> None:
        """Append lines to their months' files; telemetry, so no fsync."""
        by_file: dict[Path, list[str]] = {}
        for line in lines:
            path = layout.views_file(self._data_dir, line.hour)
            by_file.setdefault(path, []).append(line.model_dump_json(exclude_none=True))
        self._root.mkdir(parents=True, exist_ok=True)
        for path, rows in by_file.items():
            with path.open("a", encoding="utf-8") as handle:
                handle.write("".join(f"{row}\n" for row in rows))

    def _unflushed(self) -> list[ViewLine]:
        """What is counted in memory and not on disk yet. The caller holds ``_lock``."""
        return [line for key, counter in self._counters.items() if (line := counter.pending(key))]

    def _read_disk(self) -> list[ViewLine]:
        """Every readable line on disk; a torn one is skipped."""
        found: list[ViewLine] = []
        for path in sorted(self._root.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    found.append(ViewLine.model_validate_json(line))
                except ValidationError:
                    logger.warning("views: %s holds a line that is not a count; skipped", path.name)
        return found


def is_unfurler(user_agent: str) -> bool:
    """Whether a user agent is one of the chat link unfurlers."""
    agent = user_agent.casefold()
    return any(name in agent for name in UNFURLERS)


def _hour_of(moment: datetime) -> datetime:
    """The start of the hour ``moment`` falls in."""
    return moment.replace(minute=0, second=0, microsecond=0)


def _span(lines: Iterable[ViewLine]) -> tuple[datetime | None, datetime | None]:
    """The first and last hour with a view, to the hour."""
    hours = [line.hour for line in lines if line.views]
    return (min(hours), max(hours)) if hours else (None, None)


def _month_of(path: Path) -> tuple[int, int] | None:
    """The (year, month) a views file name names, or ``None`` when it is not one of ours."""
    try:
        month = datetime.strptime(path.stem, "%Y-%m")
    except ValueError:
        return None
    return month.year, month.month
