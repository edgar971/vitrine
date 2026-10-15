"""drop's ``/api/artifacts`` routes, over ``http.client`` alone.

Hermes installs a plugin into its own environment, so the plugin brings no dependencies: a
missing ``httpx`` there would only show when the agent first publishes.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from http.client import HTTPConnection, HTTPResponse, HTTPSConnection
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

# A guard against walking a home directory by mistake; the server's own `max_files` decides.
MAX_FILES = 1000
CHUNK = 64 * 1024
TIMEOUT = 300.0
# What a multipart header cannot carry without an escaping rule the server would have to guess.
UNSAFE_NAME = ('"', "\r", "\n")
EARLY_CLOSE = (
    "drop closed the connection before the upload finished, which it does when it refuses an "
    "upload unread: check that DROP_TOKEN is right and has the write scope, and that the files "
    "fit the server's max_artifact"
)


class DropError(Exception):
    """A refusal to show the agent as it is: the server's message, or the local reason."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Where the drop server is and how this agent authenticates to it."""

    url: str
    token: str

    @classmethod
    def from_env(cls) -> Endpoint:
        """``DROP_URL`` and ``DROP_TOKEN``, which Hermes reads from ``~/.hermes/.env``."""
        url = os.environ.get("DROP_URL", "").rstrip("/")
        token = os.environ.get("DROP_TOKEN", "")
        if not (url and token):
            raise DropError("set DROP_URL and DROP_TOKEN in ~/.hermes/.env")
        if urlsplit(url).scheme not in {"http", "https"}:
            raise DropError("DROP_URL must be an http or https URL")
        return cls(url=url, token=token)


def collect(paths: Sequence[str], cwd: Path) -> list[tuple[str, Path]]:
    """The files to upload, each with the relative path it gets in the artifact.

    A file keeps its name; a folder contributes its contents relative to itself, so a site's
    ``index.html`` lands at the root. Hidden files and symlinks inside a folder are skipped, as the
    server refuses hidden paths and a link can point anywhere on the machine.
    """
    files: dict[str, Path] = {}
    for raw in paths:
        path = (cwd / Path(raw).expanduser()).resolve()
        if path.is_dir():
            found = [(f.relative_to(path).as_posix(), f) for f in _walk(path)]
        elif path.is_file():
            found = [(path.name, path)]
        else:
            raise DropError(f"{raw}: no such file or folder")
        for name, file in found:
            if name in files:
                raise DropError(f"two files would both be stored as {name}")
            if any(character in name for character in UNSAFE_NAME):
                raise DropError(f"{name}: a file name cannot contain a quote or a line break")
            files[name] = file
            if len(files) > MAX_FILES:
                raise DropError(f"more than {MAX_FILES} files; publish a smaller folder")
    if not files:
        raise DropError("nothing to upload: the folders hold no visible files")
    return list(files.items())


def _walk(root: Path) -> Iterator[Path]:
    """Visible regular files under ``root``, in a stable order, without following links."""
    for current, folders, names in os.walk(root):
        folders[:] = sorted(f for f in folders if not f.startswith("."))
        for name in sorted(names):
            file = Path(current, name)
            if not name.startswith(".") and file.is_file() and not file.is_symlink():
                yield file


class Multipart:
    """A ``multipart/form-data`` body streamed from disk, with its length known up front.

    The length lets the request go out as the files are read, so a large PDF is never held in
    memory.
    """

    def __init__(
        self, fields: Sequence[tuple[str, str]], files: Sequence[tuple[str, Path]]
    ) -> None:
        """Lay out the parts; the files are read only when the body is sent."""
        self.boundary = uuid.uuid4().hex
        self._parts: list[bytes | Path] = []
        for name, value in fields:
            self._parts.append(self._header(f'name="{name}"') + value.encode() + b"\r\n")
        for name, file in files:
            disposition = f'name="file"; filename="{name}"'
            self._parts += [
                self._header(disposition, "application/octet-stream"),
                file,
                b"\r\n",
            ]
        self._parts.append(f"--{self.boundary}--\r\n".encode())

    def _header(self, disposition: str, content_type: str | None = None) -> bytes:
        lines = [f"--{self.boundary}", f"Content-Disposition: form-data; {disposition}"]
        if content_type:
            lines.append(f"Content-Type: {content_type}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    @property
    def content_type(self) -> str:
        """The header value naming this body's boundary."""
        return f"multipart/form-data; boundary={self.boundary}"

    def __len__(self) -> int:
        """The body's size in bytes, which becomes ``Content-Length``."""
        return sum(p.stat().st_size if isinstance(p, Path) else len(p) for p in self._parts)

    def __iter__(self) -> Iterator[bytes]:
        """The body, one chunk at a time."""
        for part in self._parts:
            if isinstance(part, bytes):
                yield part
                continue
            with part.open("rb") as handle:
                while chunk := handle.read(CHUNK):
                    yield chunk


class DropClient:
    """The two calls the plugin makes: publish files, and list what was published."""

    def __init__(self, endpoint: Endpoint) -> None:
        """Talk to ``endpoint`` with its token."""
        self._endpoint = endpoint

    def publish(  # noqa: PLR0913 - one keyword per documented form field
        self,
        files: Sequence[tuple[str, Path]],
        *,
        title: str | None = None,
        artifact_id: str | None = None,
        entry: str | None = None,
        expires_in: str | None = None,
        tags: Sequence[str] = (),
        note: str | None = None,
        merge: bool = False,
    ) -> dict[str, Any]:
        """Create a private artifact, or add a version to ``artifact_id``.

        There is no visibility field: the route refuses one, and going public is the MCP gate's
        job, where a human is asked.
        """
        if title is None and artifact_id is None:
            title = files[0][0]
        fields = [
            (name, value)
            for name, value in (
                ("title", title),
                ("entry", entry),
                ("expires_in", expires_in),
                ("note", note),
                ("merge", "true" if merge else None),
            )
            if value is not None
        ]
        fields += [("tags", tag) for tag in tags]
        body = Multipart(fields, files)
        path = "/api/artifacts"
        if artifact_id is not None:
            path += "/" + quote(artifact_id, safe="")
        return self._call("PUT" if artifact_id else "POST", path, body)

    def list(self, query: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """The newest artifacts, optionally those whose title or tag holds ``query``."""
        params = {"limit": str(limit)} | ({"query": query} if query else {})
        page = self._call("GET", "/api/artifacts?" + urlencode(params))
        items: list[dict[str, Any]] = page["items"]
        return items

    def _call(self, method: str, path: str, body: Multipart | None = None) -> dict[str, Any]:
        url = urlsplit(self._endpoint.url)
        kind = HTTPSConnection if url.scheme == "https" else HTTPConnection
        connection = kind(url.netloc, timeout=TIMEOUT)
        try:
            response = self._exchange(connection, method, url.path + path, body)
            payload = response.read()
        except OSError as error:
            raise DropError(f"cannot reach drop at {self._endpoint.url}: {error}") from None
        finally:
            connection.close()
        if response.status >= HTTPStatus.BAD_REQUEST:
            raise DropError(_refusal(response, payload))
        result: dict[str, Any] = json.loads(payload)
        return result

    def _exchange(
        self, connection: HTTPConnection, method: str, path: str, body: Multipart | None
    ) -> HTTPResponse:
        """Send the request; when the server answers before reading the body, read that answer.

        drop checks the token, the scope, the rate limit and the size before it reads an upload,
        and closes the connection after refusing, so the send can fail while the reason waits
        in the socket.
        """
        connection.putrequest(method, path)
        connection.putheader("Authorization", f"Bearer {self._endpoint.token}")
        if body is not None:
            connection.putheader("Content-Type", body.content_type)
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders()
        try:
            for chunk in body or ():
                connection.send(chunk)
        except (BrokenPipeError, ConnectionResetError):
            try:
                return connection.getresponse()
            except OSError:
                raise DropError(EARLY_CLOSE) from None
        return connection.getresponse()


def _refusal(response: HTTPResponse, payload: bytes) -> str:
    """The server's own message, which already says what to change and never names a secret."""
    try:
        message = str(json.loads(payload)["error"])
    except (ValueError, KeyError, TypeError):
        message = response.reason
    retry = response.headers.get("Retry-After")
    suffix = f" (retry after {retry}s)" if retry else ""
    return f"drop refused the request ({response.status}): {message}{suffix}"
