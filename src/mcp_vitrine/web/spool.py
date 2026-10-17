"""Request bodies streamed to ``data_dir/tmp`` instead of memory (spec § 7.3, security T9).

A body lands in one spool directory per request, which the store's write path then renames into
the version, so a file is written once and never held whole. The directory is removed when the
request ends, and the startup sweep removes one a crash left behind. ``BodyLimit`` bounds the
bytes as they stream, so nothing here has to.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from io import BufferedWriter
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qsl

from anyio import to_thread
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import Request

from mcp_vitrine.domain import layout
from mcp_vitrine.domain.errors import Invalid, TooLarge
from mcp_vitrine.domain.models import Spooled

if TYPE_CHECKING:
    # Declared for type checkers only.
    from python_multipart.multipart import MultipartCallbacks

# A text field is a title, a note or a tag; nothing near this is one.
FIELD_MAX = 64 * 1024
SPOOL_PREFIX = "spool-"
URLENCODED = b"application/x-www-form-urlencoded"


@dataclass(frozen=True, slots=True)
class FilePart:
    """One ``file`` part of a multipart body, already on disk."""

    name: str
    filename: str
    content_type: str
    content: Spooled


@dataclass(slots=True)
class Form:
    """A multipart body: its file parts on disk, its text fields in memory, in order."""

    files: list[FilePart] = field(default_factory=list)
    fields: list[tuple[str, str]] = field(default_factory=list)


class Spool:
    """One request's directory under ``data_dir/tmp``; its files belong to the request."""

    def __init__(self, directory: Path) -> None:
        """Use ``directory``, which the caller created and removes."""
        self.directory = directory

    def path(self) -> Path:
        """A fresh file name inside the spool."""
        return self.directory / uuid.uuid4().hex

    async def body(self, request: Request) -> Spooled:
        """The raw body, written to one file as it arrives."""
        target = self.path()
        sink = await to_thread.run_sync(_open, target)
        try:
            async for chunk in request.stream():
                if chunk:
                    await to_thread.run_sync(self.append, sink, chunk)
        finally:
            await to_thread.run_sync(sink.close)
        return Spooled(target)

    async def form(self, request: Request, *, max_files: int, max_fields: int) -> Form:
        """A ``multipart/form-data`` body, each file part streamed to its own file.

        The parser runs in a worker thread, so writing a part never blocks the event loop.
        """
        kind, options = parse_options_header(request.headers.get("content-type", ""))
        if kind == URLENCODED:
            return Form(fields=await _urlencoded(request, max_fields=max_fields))
        boundary = options.get(b"boundary")
        if kind != b"multipart/form-data" or not boundary:
            raise Invalid("the body is not valid multipart: send multipart/form-data")
        reader = _Reader(self, max_files=max_files, max_fields=max_fields)
        parser = MultipartParser(boundary, reader.callbacks())
        try:
            async for chunk in request.stream():
                if chunk:
                    await to_thread.run_sync(parser.write, chunk)
            await to_thread.run_sync(parser.finalize)
        except MultipartParseError as error:
            raise Invalid(f"the body is not valid multipart: {error}") from None
        finally:
            await to_thread.run_sync(reader.close)
        if not reader.complete:
            raise Invalid("the body is not valid multipart: it ends inside a part")
        return reader.form

    def append(self, sink: BufferedWriter, chunk: bytes) -> None:
        """Append a chunk and keep the spool fresh, so a sweep beside a slow upload spares it."""
        sink.write(chunk)
        os.utime(self.directory)


@asynccontextmanager
async def spooled(root: Path) -> AsyncIterator[Spool]:
    """A spool directory under ``root/tmp`` for one request, removed when the request ends."""
    directory = root / layout.TMP_DIR / f"{SPOOL_PREFIX}{uuid.uuid4().hex}"
    await to_thread.run_sync(directory.mkdir)
    try:
        yield Spool(directory)
    finally:
        await to_thread.run_sync(_rmtree, directory)


def _open(path: Path) -> BufferedWriter:
    """Open a new spool file for writing; it never replaces one."""
    return path.open("xb")


def _rmtree(path: Path) -> None:
    """Remove a spool directory and whatever is still in it."""
    shutil.rmtree(path, ignore_errors=True)


class _Reader:
    """The parser's callbacks: one part at a time, its headers first, then its data."""

    def __init__(self, spool: Spool, *, max_files: int, max_fields: int) -> None:
        """Count parts against the caps as they begin, before their data is read."""
        self._spool = spool
        self._max_files = max_files
        self._max_fields = max_fields
        self.form = Form()
        self.complete = False
        self._headers: dict[bytes, bytes] = {}
        self._header_name = b""
        self._header_value = b""
        self._name = ""
        self._filename: str | None = None
        self._kind = ""
        self._text = bytearray()
        self._sink: BufferedWriter | None = None
        self._target: Path | None = None

    def callbacks(self) -> MultipartCallbacks:
        """The callbacks ``MultipartParser`` calls, by its names for them."""
        return {
            "on_part_begin": self._begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_text,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._data,
            "on_part_end": self._end,
            "on_end": self._finished,
        }

    def close(self) -> None:
        """Close a part left open by a body that stopped halfway."""
        if self._sink is not None:
            self._sink.close()
            self._sink = None

    def _begin(self) -> None:
        self._headers = {}
        self._filename = None
        self._text = bytearray()

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def _header_text(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def _header_end(self) -> None:
        self._headers[self._header_name.lower()] = self._header_value
        self._header_name = b""
        self._header_value = b""

    def _headers_finished(self) -> None:
        _, options = parse_options_header(self._headers.get(b"content-disposition", b""))
        if b"name" not in options:
            raise Invalid("each part needs a Content-Disposition name")
        self._name = _decode(options[b"name"])
        self._kind = _decode(self._headers.get(b"content-type", b"")).split(";")[0].strip()
        if b"filename" not in options:
            if len(self.form.fields) >= self._max_fields:
                raise Invalid(f"a body carries at most {self._max_fields} fields")
            return
        if len(self.form.files) >= self._max_files:
            raise TooLarge(f"a version holds at most {self._max_files} files")
        self._filename = _decode(options[b"filename"])
        self._target = self._spool.path()
        self._sink = _open(self._target)

    def _data(self, data: bytes, start: int, end: int) -> None:
        if self._sink is not None:
            self._spool.append(self._sink, data[start:end])
            return
        self._text += data[start:end]
        if len(self._text) > FIELD_MAX:
            raise Invalid(f"a text field is at most {FIELD_MAX // 1024} KiB")

    def _end(self) -> None:
        if self._sink is None:
            self.form.fields.append((self._name, _decode(bytes(self._text))))
            return
        self._sink.close()
        self._sink = None
        self.form.files.append(
            FilePart(
                name=self._name,
                filename=cast("str", self._filename),
                content_type=self._kind,
                content=Spooled(cast("Path", self._target)),
            )
        )

    def _finished(self) -> None:
        self.complete = True


async def _urlencoded(request: Request, *, max_fields: int) -> list[tuple[str, str]]:
    """A form of text fields only, which an update that names no file may send."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > FIELD_MAX:
            raise Invalid(f"a form of fields is at most {FIELD_MAX // 1024} KiB")
    try:
        fields = parse_qsl(body.decode(), keep_blank_values=True, max_num_fields=max_fields)
    except UnicodeDecodeError, ValueError:
        raise Invalid(f"the body is not a valid form of at most {max_fields} fields") from None
    return fields


def _decode(value: bytes) -> str:
    """A header or field value as text; undecodable bytes are replaced, not refused."""
    return value.decode("utf-8", errors="replace")
