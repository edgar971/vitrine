"""The ``/api/artifacts`` calls the ``vitrine`` command makes, over ``httpx2``."""

from __future__ import annotations

import os
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import quote, urlsplit

import httpx2

TIMEOUT = 300.0
# A guard against walking a home directory by mistake; the server's own `max_files` decides.
MAX_FILES = 10_000
ZIP_TYPE = "application/zip"
OCTET_STREAM = "application/octet-stream"
UNSAFE_NAME = ('"', "\r", "\n")
TOO_LARGE_HINT = "send fewer or smaller files, or a built site as one archive with --zip"
EARLY_CLOSE = (
    "vitrine closed the connection before the upload finished, which it does when it refuses an "
    "upload unread: check that the token is right and has the write scope, and that the files "
    "fit the server's max_upload and max_artifact"
)

type Json = dict[str, Any]


class ClientError(Exception):
    """A refusal or a failure to show the user as it is; the server's words when it has them."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Where the server is and the token that authenticates to it."""

    url: str
    token: str

    @classmethod
    def resolve(cls, url: str | None, token_file: Path | None) -> Endpoint:
        """``--url`` or ``VITRINE_URL``; ``--token-file`` or ``VITRINE_TOKEN``.

        Never a token on the command line: argv is visible to every user through ``ps``.
        """
        base = (url or os.environ.get("VITRINE_URL", "")).rstrip("/")
        if token_file is not None:
            try:
                token = token_file.read_text(encoding="utf-8").strip()
            except OSError as error:
                raise ConfigurationError(f"cannot read {token_file}: {error.strerror}") from None
        else:
            token = os.environ.get("VITRINE_TOKEN", "").strip()
        if not base:
            raise ConfigurationError("set VITRINE_URL or pass --url")
        if urlsplit(base).scheme not in {"http", "https"}:
            raise ConfigurationError("the vitrine URL must be http or https")
        if not token:
            raise ConfigurationError("set VITRINE_TOKEN or pass --token-file")
        return cls(url=base, token=token)


class ConfigurationError(ClientError):
    """The URL or the token is missing or unusable: exit status 2."""


def collect(paths: Sequence[Path]) -> list[tuple[str, Path]]:
    """The files to upload, each with its relative path in the artifact.

    A file keeps its name; a folder gives its contents relative to itself, so a site's
    ``index.html`` lands at the root. Hidden files and symlinks inside a folder are skipped: the
    server refuses hidden paths, and a link can point anywhere on the machine.
    """
    files: dict[str, Path] = {}
    for raw in paths:
        path = raw.expanduser().resolve()
        if path.is_dir():
            found = [(file.relative_to(path).as_posix(), file) for file in _walk(path)]
        elif path.is_file():
            found = [(path.name, path)]
        else:
            raise ClientError(f"{raw}: no such file or folder")
        for name, file in found:
            if name in files:
                raise ClientError(f"two files would both be stored as {name}")
            if any(character in name for character in UNSAFE_NAME):
                raise ClientError(f"{name}: a file name cannot hold a quote or a line break")
            files[name] = file
            if len(files) > MAX_FILES:
                raise ClientError(f"more than {MAX_FILES} files: publish a smaller folder")
    if not files:
        raise ClientError("nothing to upload: the folders hold no visible files")
    return list(files.items())


def pack(files: Sequence[tuple[str, Path]], into: Path) -> Path:
    """One deflated zip of ``files`` under their artifact paths, for ``unpack=zip``."""
    archive = into / "site.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as writer:
        for name, file in files:
            writer.write(file, name)
    return archive


class Client:
    """The ``/api`` routes, one method each; every call sends the bearer token."""

    def __init__(
        self, endpoint: Endpoint, *, transport: httpx2.BaseTransport | None = None
    ) -> None:
        """Talk to ``endpoint``; ``transport`` is a seam for tests."""
        self._http = httpx2.Client(
            base_url=endpoint.url,
            headers={"Authorization": f"Bearer {endpoint.token}"},
            timeout=TIMEOUT,
            transport=transport,
        )
        self._url = endpoint.url

    def close(self) -> None:
        """Close the connection pool."""
        self._http.close()

    def publish(
        self,
        files: Sequence[tuple[str, Path]],
        fields: Sequence[tuple[str, str]],
        *,
        artifact_id: str | None = None,
        archive: bool = False,
    ) -> Json:
        """Create a private artifact, or add a version: multipart, streamed from disk.

        There is no visibility field: the route refuses one, and going public is the MCP gate's.
        """
        path = "/api/artifacts" + ("" if artifact_id is None else f"/{_segment(artifact_id)}")
        with TemporaryDirectory(prefix="vitrine-") as scratch, ExitStack() as handles:
            if archive:
                packed = pack(files, Path(scratch))
                parts = [("file", ("site.zip", handles.enter_context(packed.open("rb")), ZIP_TYPE))]
                fields = [*fields, ("unpack", "zip")]
            else:
                parts = [
                    ("file", (name, handles.enter_context(file.open("rb")), OCTET_STREAM))
                    for name, file in files
                ]
            return self._call(
                "PUT" if artifact_id else "POST", path, files=parts, data=_form(fields)
            )

    def list(self, params: Mapping[str, str]) -> Json:
        """One page of ``GET /api/artifacts``."""
        return self._call("GET", "/api/artifacts", params=dict(params))

    def get(self, artifact_id: str) -> Json:
        """``GET /api/artifacts/{id}``: metadata, files, links and views."""
        return self._call("GET", f"/api/artifacts/{_segment(artifact_id)}")

    def link(self, artifact_id: str, body: Json) -> Json:
        """``POST /api/artifacts/{id}/links``."""
        return self._call("POST", f"/api/artifacts/{_segment(artifact_id)}/links", json=body)

    def revoke(self, artifact_id: str, body: Json) -> Json:
        """``POST /api/artifacts/{id}/revoke``."""
        return self._call("POST", f"/api/artifacts/{_segment(artifact_id)}/revoke", json=body)

    def meta(self, artifact_id: str, body: Json) -> Json:
        """``PATCH /api/artifacts/{id}``."""
        return self._call("PATCH", f"/api/artifacts/{_segment(artifact_id)}", json=body)

    def delete(self, artifact_id: str) -> Json:
        """``DELETE /api/artifacts/{id}``."""
        return self._call("DELETE", f"/api/artifacts/{_segment(artifact_id)}")

    def _call(self, method: str, path: str, **request: Any) -> Json:  # noqa: ANN401 - httpx2's own keywords
        try:
            response = self._http.request(method, path, **request)
        except httpx2.RemoteProtocolError, httpx2.WriteError:
            raise ClientError(EARLY_CLOSE) from None
        except httpx2.HTTPError as error:
            raise ClientError(f"cannot reach vitrine at {self._url}: {error}") from None
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            raise ClientError(_refusal(response))
        body: Json = response.json()
        return body


@contextmanager
def connected(endpoint: Endpoint) -> Iterator[Client]:
    """A client for one command, closed when it ends."""
    client = Client(endpoint)
    try:
        yield client
    finally:
        client.close()


def _form(fields: Sequence[tuple[str, str]]) -> dict[str, list[str]]:
    """Repeated fields (``tags``) as httpx2 wants them: one list per name."""
    form: dict[str, list[str]] = {}
    for name, value in fields:
        form.setdefault(name, []).append(value)
    return form


def _segment(artifact_id: str) -> str:
    """An id as one path segment, whatever the user typed."""
    return quote(artifact_id, safe="")


def _refusal(response: httpx2.Response) -> str:
    """The server's own message, which already says what to change and never names a secret."""
    try:
        message = str(response.json()["error"])
    except ValueError, KeyError, TypeError:
        message = response.reason_phrase
    retry = response.headers.get("Retry-After")
    suffix = f" (retry after {retry}s)" if retry else ""
    hint = (
        f"; {TOO_LARGE_HINT}" if response.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE else ""
    )
    return f"vitrine refused the request ({response.status_code}): {message}{suffix}{hint}"


def _walk(root: Path) -> Iterator[Path]:
    """Visible regular files under ``root``, in a stable order, without following links."""
    for current, folders, names in os.walk(root):
        folders[:] = sorted(folder for folder in folders if not folder.startswith("."))
        for name in sorted(names):
            file = Path(current, name)
            if not name.startswith(".") and file.is_file() and not file.is_symlink():
                yield file
