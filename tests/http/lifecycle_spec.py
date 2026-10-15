"""``build_app``'s lifespan: the startup sweep, and the views it counts (spec § 11.2)."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import httpx2
import pytest

from mcp_drop.config import Settings
from mcp_drop.domain import layout
from mcp_drop.domain.errors import Expired, NotFound
from mcp_drop.domain.models import ArtifactDraft, PublishedArtifact
from mcp_drop.services.container import Services
from tests.conftest import FrozenClock
from tests.http.conftest import CLIENT
from tests.unit.services.metrics_spec import RecordingMetrics

WAIT = 10.0


def _eventually(condition: Callable[[], bool]) -> bool:
    """Whether ``condition`` holds within ``WAIT`` seconds; the sweep runs on its own schedule."""
    deadline = time.monotonic() + WAIT
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def _gone(services: Services, artifact_id: str) -> bool:
    """Whether the artifact no longer exists."""
    try:
        services.artifacts.get(artifact_id)
    except NotFound:
        return True
    except Expired:
        # Expired but not swept yet.
        return False
    return False


@pytest.fixture
def leftover(settings: Settings) -> Path:
    """A stage a crashed write left in ``tmp/`` before the server started."""
    stage = settings.data_dir / layout.TMP_DIR / "crashed-write"
    stage.mkdir(parents=True)
    return stage


@pytest.fixture
def metrics() -> RecordingMetrics:
    """The counts the running server records."""
    return RecordingMetrics()


@pytest.fixture
def services(settings: Settings, clock: FrozenClock, metrics: RecordingMetrics) -> Services:
    """The server's services, counting into ``metrics``."""
    return Services.from_settings(settings, clock=clock, metrics=metrics)


class DescribeTheLifespan:
    class WhenTheServerStarts:
        def it_sweeps_what_a_crash_left_in_staging(self, leftover: Path, base: str) -> None:
            assert not leftover.exists()

    class WhenTheSweepIntervalPasses:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            """Sweep every second."""
            return {"DROP_SWEEP_INTERVAL": "1s"}

        def it_deletes_what_expired_meanwhile(
            self, base: str, services: Services, clock: FrozenClock
        ) -> None:
            draft = ArtifactDraft(
                title="Brief", files={"a": b"a"}, expires_at=clock.now() + timedelta(minutes=1)
            )
            artifact_id = services.artifacts.create(draft, client=CLIENT).id
            clock.advance(timedelta(hours=1))
            assert _eventually(lambda: _gone(services, artifact_id))

    class WhenASweepFails:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            """Sweep every second."""
            return {"DROP_SWEEP_INTERVAL": "1s"}

        @pytest.fixture
        def failing(self, services: Services, monkeypatch: pytest.MonkeyPatch) -> list[int]:
            """A sweep that raises, counting its attempts."""
            attempts: list[int] = []

            def sweep(**_: object) -> None:
                attempts.append(1)
                raise OSError("the disk went away")

            monkeypatch.setattr(services.artifacts, "sweep", sweep)
            return attempts

        def it_keeps_serving_and_tries_again(
            self, failing: list[int], base: str, http: httpx2.Client
        ) -> None:
            assert _eventually(lambda: len(failing) >= 2)
            assert http.get("/healthz").status_code == 200

    class WhenAPrivateLinkIsViewed:
        def it_counts_a_signed_view(
            self, published: PublishedArtifact, http: httpx2.Client, metrics: RecordingMetrics
        ) -> None:
            http.get(published.url)
            assert metrics.counts["view", "s"] == 1
