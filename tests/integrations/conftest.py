"""Hermes plugin givens: a real server (the HTTP givens), and the plugin pointed at it."""

from __future__ import annotations

import socket
import struct
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.http import conftest as http_givens
from tests.http.conftest import FULL_TOKEN

# The HTTP givens, so these specs drive the same real server the HTTP specs do.
port = http_givens.port
overrides = http_givens.overrides
settings = http_givens.settings
services = http_givens.services
base = http_givens.base


@dataclass
class Recorder:
    """A stand-in ``PluginContext`` that keeps what ``register()`` handed it."""

    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    skills: dict[str, Path] = field(default_factory=dict)
    commands: dict[str, Callable[[str], str]] = field(default_factory=dict)

    def register_tool(self, name: str, toolset: str, **options: Any) -> None:  # noqa: ANN401
        self.tools[name] = {"toolset": toolset, **options}

    def register_skill(self, name: str, path: Path, description: str = "") -> None:
        self.skills[name] = path

    def register_command(self, name: str, handler: Callable[[str], str], **_options: str) -> None:
        self.commands[name] = handler


@pytest.fixture
def connected(base: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """``DROP_URL`` and ``DROP_TOKEN`` as Hermes reads them from ``~/.hermes/.env``."""
    monkeypatch.setenv("DROP_URL", base)
    monkeypatch.setenv("DROP_TOKEN", FULL_TOKEN)
    return base


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A folder an agent built, with assets, a hidden file and a link out of the folder.

    The agent's working directory is its parent, so ``site`` is a relative path.
    """
    workspace = tmp_path / "workspace"
    root = workspace / "site"
    (root / "img").mkdir(parents=True)
    (root / "index.html").write_text("<h1>Energy</h1><img src='img/chart.png'>")
    (root / "img" / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 512)
    (root / ".env").write_text("SECRET=1")
    (workspace / "outside.txt").write_text("not part of the site")
    (root / "linked.txt").symlink_to(workspace / "outside.txt")
    monkeypatch.chdir(workspace)
    return root


@pytest.fixture
def hang_up() -> Iterator[str]:
    """A server that reads a request's headers, then resets the connection without answering.

    That is what reaches the client when drop refuses a large upload unread: the kernel drops
    the answer along with the unread body.
    """
    listener = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        connection, _ = listener.accept()
        received = b""
        while b"\r\n\r\n" not in received:
            received += connection.recv(4096)
        # SO_LINGER with a zero timeout turns close() into a reset.
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        connection.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    thread.join(5)
    listener.close()
