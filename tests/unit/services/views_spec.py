"""``ViewLog``: anonymous counts per artifact, link and hour (spec § 11.4, security T23)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import timedelta
from pathlib import Path

import pytest

from mcp_vitrine.domain import layout
from mcp_vitrine.domain.clock import Clock
from mcp_vitrine.domain.models import Views
from mcp_vitrine.services.views import ViewLine, ViewLog
from tests.conftest import FrozenClock

ARTIFACT = "k7m2q9x4tz3wbn6r"
LID = "AbCdEfGh_-0"
ADDRESS = "203.0.113.7"
AGENT = "Mozilla/5.0 spec-browser"


@pytest.fixture
def log(tmp_path: Path, clock: Clock) -> ViewLog:
    """A view log counting under the test's directory."""
    return ViewLog(tmp_path, enabled=True, retention=timedelta(days=90), clock=clock)


def _view(
    log: ViewLog, *, address: str = ADDRESS, agent: str = AGENT, lid: str | None = None
) -> None:
    """One view of ``ARTIFACT`` through ``lid``."""
    log.record(ARTIFACT, route="s", lid=lid, address=address, user_agent=agent)


def _totals(log: ViewLog) -> Views:
    """The totals ``artifact_get`` would show."""
    totals = log.totals(ARTIFACT, labels={LID: "sent to finance"})
    assert totals is not None
    return totals


def _written(tmp_path: Path) -> str:
    """Everything on disk under ``views/``."""
    return "".join(path.read_text() for path in sorted((tmp_path / layout.VIEWS_DIR).glob("*")))


class DescribeCounting:
    class WhenTwoPeopleView:
        def it_counts_two_visitors(self, log: ViewLog) -> None:
            _view(log)
            _view(log, address="198.51.100.2")
            assert _totals(log).visitors == 2

    class WhenOnePersonViewsTwiceInAnHour:
        def it_counts_two_views_and_one_visitor(self, log: ViewLog) -> None:
            _view(log)
            _view(log)
            assert (_totals(log).total, _totals(log).visitors) == (2, 1)

    class WhenTheSamePersonComesBackNextHour:
        def it_counts_them_again(self, log: ViewLog, advance: Callable[[timedelta], None]) -> None:
            _view(log)
            advance(timedelta(hours=1))
            _view(log)
            assert _totals(log).visitors == 2

    class WhenAnUnfurlerFetches:
        def it_counts_a_bot_not_a_view(self, log: ViewLog) -> None:
            _view(log, agent="facebookexternalhit/1.1")
            assert (_totals(log).total, _totals(log).bots, _totals(log).visitors) == (0, 1, 0)

    class WhenALinkHasALid:
        def it_attributes_the_view_to_it(self, log: ViewLog) -> None:
            _view(log, lid=LID)
            link = _totals(log).links[0]
            assert (link.lid, link.label, link.views) == (LID, "sent to finance", 1)

    class WhenItIsRead:
        def it_dates_the_views_to_the_hour(self, log: ViewLog, clock: FrozenClock) -> None:
            _view(log)
            assert _totals(log).first_viewed_at == clock.now().replace(minute=0, second=0)


class DescribeFlushing:
    class WhenCountsAreFlushed:
        def it_keeps_the_totals(self, log: ViewLog) -> None:
            _view(log)
            _view(log)
            log.flush()
            assert (_totals(log).total, _totals(log).visitors) == (2, 1)

        def it_writes_one_line_per_key(self, log: ViewLog, tmp_path: Path) -> None:
            _view(log)
            _view(log, lid=LID)
            assert log.flush() == 2

        def it_never_writes_an_ip_or_user_agent(self, log: ViewLog, tmp_path: Path) -> None:
            _view(log)
            log.flush()
            written = _written(tmp_path)
            assert (ADDRESS in written, "spec-browser" in written) == (False, False)

    class WhenTheSameVisitorReturnsAfterAFlush:
        def it_does_not_count_them_twice(self, log: ViewLog) -> None:
            _view(log)
            log.flush()
            _view(log)
            log.flush()
            assert (_totals(log).total, _totals(log).visitors) == (2, 1)

    class WhenAViewArrivesWhileItWrites:
        def it_counts_it_once(self, log: ViewLog, monkeypatch: pytest.MonkeyPatch) -> None:
            # The moment between counting and writing, where a view can arrive.
            original = log._append

            def append_then_view(lines: Iterable[ViewLine]) -> None:
                original(lines)
                _view(log)

            _view(log)
            monkeypatch.setattr(log, "_append", append_then_view)
            log.flush()
            monkeypatch.undo()
            log.flush()
            assert _totals(log).total == 2

    class WhenNothingWasViewed:
        def it_writes_nothing(self, log: ViewLog, tmp_path: Path) -> None:
            assert (log.flush(), _written(tmp_path)) == (0, "")

    class WhenAFreshLogReadsTheFile:
        def it_sees_what_was_flushed(self, log: ViewLog, tmp_path: Path, clock: Clock) -> None:
            _view(log)
            log.flush()
            again = ViewLog(tmp_path, enabled=True, retention=timedelta(days=90), clock=clock)
            assert again.totals(ARTIFACT, labels={}) == _totals(log)


class DescribeTheSeries:
    class WhenViewsSpanTwoHours:
        def it_lists_each_hour_oldest_first(
            self, log: ViewLog, clock: FrozenClock, advance: Callable[[timedelta], None]
        ) -> None:
            _view(log)
            advance(timedelta(hours=1))
            _view(log)
            _view(log, address="198.51.100.2")
            series = log.series(ARTIFACT, since=clock.now() - timedelta(days=30))
            assert [hour.views for hour in series] == [1, 2]


class DescribeRetention:
    class WhenAMonthIsOlderThanTheRetention:
        def it_is_deleted(
            self, log: ViewLog, tmp_path: Path, advance: Callable[[timedelta], None]
        ) -> None:
            _view(log)
            log.flush()
            advance(timedelta(days=120))
            assert (log.sweep(), _written(tmp_path)) == (1, "")


class DescribeViewLogOff:
    @pytest.fixture
    def off(self, tmp_path: Path, clock: Clock) -> ViewLog:
        """A view log that records nothing."""
        return ViewLog(tmp_path, enabled=False, retention=timedelta(days=90), clock=clock)

    class WhenViewLogIsOff:
        def it_writes_nothing(self, off: ViewLog, tmp_path: Path) -> None:
            _view(off)
            off.flush()
            assert _written(tmp_path) == ""

        def it_reports_no_totals(self, off: ViewLog) -> None:
            assert off.totals(ARTIFACT, labels={}) is None
