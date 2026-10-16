"""``serve``: what is checked before listening, and a real process answering ``/healthz``."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
import uvicorn

from mcp_vitrine.config import Settings
from mcp_vitrine.domain import layout
from mcp_vitrine.domain.models import SCHEMA_VERSION, ArtifactDraft
from mcp_vitrine.services.container import Services
from tests.cli.conftest import Runner
from tests.http.conftest import free_port

STARTUP_TIMEOUT = 15.0


@dataclass(frozen=True, slots=True)
class Listening:
    """Where the server process answers."""

    base: str
    metrics: str


@pytest.fixture
def uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stand in for ``uvicorn.run``, keeping the arguments ``serve`` hands it.

    ``serve`` also configures the root logger; the copy below is what it changes, so the next
    spec's log capture is left as pytest set it up.
    """
    monkeypatch.setattr(logging.root, "handlers", list(logging.root.handlers))
    monkeypatch.setattr(logging.root, "level", logging.root.level)
    seen: dict[str, Any] = {}

    def run(app: object, **options: object) -> None:
        seen.update(options, app=app)

    monkeypatch.setattr(uvicorn, "run", run)
    return seen


@pytest.fixture
def artifact_id(settings: Settings) -> str:
    """One artifact in the data_dir."""
    draft = ArtifactDraft(title="Energy", files={"index.html": b"hi"})
    return Services.from_settings(settings).artifacts.create(draft, client="hermes").id


def _set_schema(settings: Settings, artifact_id: str, version: int) -> None:
    """Rewrite one ``meta.json`` as another build would have left it."""
    path = layout.meta_file(settings.data_dir, artifact_id)
    path.write_text(json.dumps(json.loads(path.read_text()) | {"schema_version": version}))


class DescribeServe:
    class WhenTheConfigurationIsValid:
        def it_hands_the_app_to_uvicorn(
            self, run: Runner, environment: None, uvicorn_run: dict[str, Any]
        ) -> None:
            run("serve")
            assert uvicorn_run["app"] is not None

        def it_leaves_access_logging_to_the_redacting_middleware(
            self, run: Runner, environment: None, uvicorn_run: dict[str, Any]
        ) -> None:
            run("serve")
            assert (uvicorn_run["access_log"], uvicorn_run["proxy_headers"]) == (False, False)

    class WhenHostAndPortAreGivenAsFlags:
        def it_listens_there(
            self, run: Runner, environment: None, uvicorn_run: dict[str, Any]
        ) -> None:
            run("serve", "--host", "0.0.0.0", "--port", "9999")
            assert (uvicorn_run["host"], uvicorn_run["port"]) == ("0.0.0.0", 9999)

    class WhenTheLogFormatIsJson:
        def it_logs_one_json_object_per_line(
            self,
            run: Runner,
            environment: None,
            monkeypatch: pytest.MonkeyPatch,
            uvicorn_run: dict[str, Any],
        ) -> None:
            monkeypatch.setenv("VITRINE_LOG_FORMAT", "json")
            run("serve")
            record = logging.LogRecord("vitrine", logging.INFO, __file__, 1, "hello", None, None)
            line = json.loads(logging.root.handlers[0].format(record))
            assert (line["level"], line["message"]) == ("INFO", "hello")

    class WhenTheDataIsOlderThanThisBuild:
        def it_asks_for_a_migration_instead_of_starting(
            self,
            run: Runner,
            environment: None,
            settings: Settings,
            artifact_id: str,
            uvicorn_run: dict[str, Any],
        ) -> None:
            _set_schema(settings, artifact_id, SCHEMA_VERSION - 1)
            assert "mcp-vitrine migrate" in run("serve").err

        def it_never_listens(
            self,
            run: Runner,
            environment: None,
            settings: Settings,
            artifact_id: str,
            uvicorn_run: dict[str, Any],
        ) -> None:
            _set_schema(settings, artifact_id, SCHEMA_VERSION - 1)
            run("serve")
            assert uvicorn_run == {}

    class WhenTheDataIsNewerThanThisBuild:
        def it_points_at_the_pre_upgrade_backup(
            self,
            run: Runner,
            environment: None,
            settings: Settings,
            artifact_id: str,
            uvicorn_run: dict[str, Any],
        ) -> None:
            _set_schema(settings, artifact_id, SCHEMA_VERSION + 1)
            assert "pre-upgrade backup" in run("serve").err

    class WhenMetricsAreOnWithoutTheExtra:
        def it_exits_as_misconfigured(
            self,
            run: Runner,
            environment: None,
            monkeypatch: pytest.MonkeyPatch,
            uvicorn_run: dict[str, Any],
        ) -> None:
            monkeypatch.setenv("VITRINE_METRICS_PORT", "9090")
            monkeypatch.setitem(sys.modules, "prometheus_client", None)
            monkeypatch.delitem(sys.modules, "mcp_vitrine.metrics", raising=False)
            assert run("serve").status == 2

    class WhenTheConfigurationIsInvalid:
        def it_exits_as_misconfigured(
            self,
            run: Runner,
            environment: None,
            monkeypatch: pytest.MonkeyPatch,
            uvicorn_run: dict[str, Any],
        ) -> None:
            monkeypatch.delenv("VITRINE_SECRET")
            assert run("serve").status == 2


class DescribeTheServerProcess:
    @pytest.fixture
    def server(self, environment: None) -> Iterator[Listening]:
        """``python -m mcp_vitrine serve`` on a free port, with metrics on a second one."""
        port, metrics_port = free_port(), free_port()
        env = os.environ | {
            "VITRINE_PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
            "VITRINE_METRICS_PORT": str(metrics_port),
        }
        process = subprocess.Popen(  # noqa: S603 - our own interpreter and module
            [sys.executable, "-m", "mcp_vitrine", "serve", "--port", str(port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            _wait_for(f"{base}/healthz")
            yield Listening(base=base, metrics=f"http://127.0.0.1:{metrics_port}")
        finally:
            process.terminate()
            process.wait(STARTUP_TIMEOUT)

    class WhenItHasStarted:
        def it_answers_healthz(self, server: Listening) -> None:
            assert httpx2.get(f"{server.base}/healthz").json() == {"ok": True}

        def it_serves_metrics_on_their_own_port(self, server: Listening) -> None:
            assert "vitrine_artifacts 0.0" in httpx2.get(f"{server.metrics}/metrics").text


def _wait_for(url: str) -> None:
    """Poll until ``url`` answers, or fail after the startup timeout."""
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        try:
            httpx2.get(url)
        except httpx2.TransportError:
            time.sleep(0.05)
        else:
            return
    pytest.fail(f"{url} never answered")
