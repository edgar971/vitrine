"""HTTP givens: the whole app (``build_app``) served by real uvicorn on a free port."""

from __future__ import annotations

import hashlib
import socket
import threading
import time
from collections.abc import Iterator

import httpx2
import pytest
import uvicorn

from mcp_vitrine.app import build_app
from mcp_vitrine.config import Settings, load_settings
from mcp_vitrine.domain.models import ArtifactDraft, PublishedArtifact
from mcp_vitrine.services.container import Services
from tests.conftest import FrozenClock

CLIENT = "hermes"
FULL_TOKEN = "full-token-for-hermes"
READ_TOKEN = "read-token-for-viewer"
STARTUP_TIMEOUT = 10.0
FILES = {
    "index.html": b"<h1>hi</h1><img src='img/a.png'>",
    "img/a.png": b"\x89PNG\r\n\x1a\n",
    "img/dot.svg": b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>",
    "notes.md": b"# Notes\n\n<script>alert(1)</script>",
    "data.csv": b"month,kwh\nsep,12",
    "data.json": b'{"a":1}',
    "feed.xml": b"<feed/>",
    "blob.bin": b"\x00\x01\x02",
    "clip.mp4": b"0123456789",
}


def token_hash(token: str) -> str:
    """The ``VITRINE_TOKENS`` form of a token."""
    return hashlib.sha256(token.encode()).hexdigest()


def signature(url: str, marker: str = "/s/") -> str:
    """The signature segment of a link URL."""
    return url.split(marker)[1].split("?", maxsplit=1)[0].split("/", maxsplit=1)[0]


def free_port() -> int:
    """A port nothing listens on right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture
def port() -> int:
    """The port this spec's server listens on, known before the settings are built."""
    return free_port()


@pytest.fixture
def overrides() -> dict[str, str]:
    """Extra ``VITRINE_*`` variables; a nested class narrows a limit by overriding this."""
    return {}


@pytest.fixture
def settings(
    environment: None, monkeypatch: pytest.MonkeyPatch, port: int, overrides: dict[str, str]
) -> Settings:
    """Two clients, one with every scope and one that only reads, on this spec's origin."""
    monkeypatch.setenv(
        "VITRINE_TOKENS",
        f"{CLIENT}:{token_hash(FULL_TOKEN)},viewer:{token_hash(READ_TOKEN)}:read",
    )
    monkeypatch.setenv("VITRINE_PUBLIC_BASE_URL", f"http://127.0.0.1:{port}")
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    return load_settings()


@pytest.fixture
def services(settings: Settings, clock: FrozenClock) -> Services:
    """The services the server is built from, so a spec can arrange state directly."""
    return Services.from_settings(settings, clock=clock)


@pytest.fixture
def base(settings: Settings, services: Services, port: int) -> Iterator[str]:
    """``build_app`` under uvicorn, configured as ``mcp-vitrine serve`` runs it."""
    config = uvicorn.Config(
        build_app(settings, services),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        proxy_headers=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(STARTUP_TIMEOUT)


@pytest.fixture
def http(base: str) -> Iterator[httpx2.Client]:
    """A plain client of the running server; it sends no credentials of its own."""
    with httpx2.Client(base_url=base) as client:
        yield client


@pytest.fixture
def bearer() -> dict[str, str]:
    """The header of the client with every scope."""
    return {"Authorization": f"Bearer {FULL_TOKEN}"}


@pytest.fixture
def published(services: Services) -> PublishedArtifact:
    """One private artifact holding one file of every kind the response policy treats apart."""
    return services.artifacts.create(ArtifactDraft(title="Energy", files=FILES), client=CLIENT)
