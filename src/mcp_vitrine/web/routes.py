"""The HTTP routes of spec § 7.1. Each one authenticates itself (security T3).

The MCP auth provider covers ``/mcp`` only, so every route here names how it is authorised in
``ROUTE_ACCESS``, and ``tests/http/routes_spec.py`` fails on a route that is not listed there.
Like a tool, a route stays thin: it checks the caller, translates, calls a service and maps a
``VitrineError`` through :func:`problem`, which is the only place a status is chosen.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from functools import partial
from typing import Annotated, Any
from urllib.parse import quote

from anyio import to_thread
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp_vitrine.config import Settings, requested_duration
from mcp_vitrine.domain import layout
from mcp_vitrine.domain.errors import Invalid, NotFound, TooLarge, VitrineError
from mcp_vitrine.domain.ids import CLIENT_REF_MAX, is_valid_id
from mcp_vitrine.domain.models import (
    PROJECT_PATTERN,
    TAG_LIMIT,
    TAG_MAX,
    TITLE_MAX,
    Artifact,
    ArtifactDraft,
    ArtifactQuery,
    PublishedArtifact,
    Scope,
    VersionDraft,
)
from mcp_vitrine.services.audit import DenialReason
from mcp_vitrine.services.container import Services
from mcp_vitrine.services.limits import Bucket
from mcp_vitrine.web.renderers import RENDER_LIMIT, renderer_for
from mcp_vitrine.web.responses import (
    Prefix,
    artifact_file,
    forbidden,
    problem,
    rendered,
    security_headers,
    unauthorized,
)

NEVER = "never"
DURATION = r"^\d+[smhd]?$"
BEARER = re.compile(r"^bearer\s+(\S+)$", re.IGNORECASE)
# The multipart parser's own file cap only has to stop pathological bodies (the body limit
# already bounds them); max_files is counted here, so going over it is a 413, not a parse error.
PARSER_MAX_FILES = 10_000
PARSER_MAX_FIELDS = 64
VERSION_MARK = "@"
# More digits than any real version number needs.
MAX_PIN_DIGITS = 9


class Access(StrEnum):
    """How a route proves the caller may use it (security § 6.2)."""

    PUBLIC = "public"
    SIGNATURE = "signature"
    BEARER = "bearer"
    MCP = "mcp"


ROUTE_ACCESS: dict[str, Access] = {
    "view_public": Access.PUBLIC,
    "view_private": Access.SIGNATURE,
    "upload": Access.SIGNATURE,
    "api_create": Access.BEARER,
    "api_update": Access.BEARER,
    "api_list": Access.BEARER,
    "health": Access.PUBLIC,
    "mcp": Access.MCP,
}


class _Fields(BaseModel):
    """The text fields of a multipart create or update, bounded as the tools bound them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: Annotated[str | None, Field(max_length=TITLE_MAX)] = None
    entry: str | None = None
    expires_in: Annotated[str | None, Field(pattern=rf"{DURATION}|^{NEVER}$")] = None
    tags: Annotated[
        tuple[Annotated[str, Field(max_length=TAG_MAX)], ...], Field(max_length=TAG_LIMIT)
    ]
    # An empty project clears it on an update, and means none on a create.
    project: Annotated[str | None, Field(pattern=rf"{PROJECT_PATTERN}|^$")] = None
    note: Annotated[str, Field(max_length=1000)] = ""
    client_ref: Annotated[
        str | None, Field(max_length=CLIENT_REF_MAX, pattern=r"^[A-Za-z0-9._:-]+$")
    ] = None
    merge: bool = False

    @field_validator("tags", mode="before")
    @classmethod
    def _split_tags(cls, value: object) -> object:
        """Accept repeated ``tags`` fields, each of which may itself be comma-separated."""
        if isinstance(value, list):
            return tuple(
                tag.strip() for item in value for tag in str(item).split(",") if tag.strip()
            )
        return value


class WebRoutes:
    """The route handlers, holding the services they were built with; nothing is global."""

    def __init__(self, settings: Settings, services: Services) -> None:
        """Keep the services and the settings a route needs."""
        self._settings = settings
        self._services = services

    def routes(self) -> list[Route]:
        """The table, named as ``ROUTE_ACCESS`` names it."""
        return [
            Route("/a/{ref}/{path:path}", self.view_public, methods=["GET"], name="view_public"),
            Route("/s/{ref}/{path:path}", self.view_private, methods=["GET"], name="view_private"),
            Route("/u/{sig}", self.upload, methods=["PUT"], name="upload"),
            Route("/api/artifacts", self.api_create, methods=["POST"], name="api_create"),
            Route("/api/artifacts", self.api_list, methods=["GET"], name="api_list"),
            Route("/api/artifacts/{id}", self.api_update, methods=["PUT"], name="api_update"),
            Route("/healthz", self.health, methods=["GET"], name="health"),
        ]

    async def view_public(self, request: Request) -> Response:
        """``GET /a/{id}[@n]/[path]``: a public artifact, no credentials."""
        return await self._view(request, Prefix.PUBLIC, self._services.artifacts.open_public)

    async def view_private(self, request: Request) -> Response:
        """``GET /s/{sig}[@n]/[path]``: any artifact the signature names."""
        return await self._view(request, Prefix.SIGNED, self._services.artifacts.open_signed)

    async def upload(self, request: Request) -> Response:
        """``PUT /u/{sig}?filename=``: one raw file, once per URL (spec § 7.3)."""
        service = self._services.artifacts
        try:
            claims = service.upload_claims(request.path_params["sig"])
            client = claims.client
            # A removed client, or one no longer allowed to write, has revoked its own URLs.
            if client is None or not self._services.tokens.allows(client, Scope.WRITE):
                raise NotFound()
            await self._spend(Bucket.WRITES, client, route="upload", artifact=claims.id)
            filename = request.query_params.get("filename")
            if not filename:
                raise Invalid("name the file: PUT /u/<sig>?filename=report.pdf")
            title = request.query_params.get("title")
            if title is not None and len(title) > TITLE_MAX:
                raise Invalid(f"a title is at most {TITLE_MAX} characters")
            body = await request.body()
            published = await _run(partial(service.upload, claims, filename, body, title=title))
        except VitrineError as error:
            return problem(error)
        return _published(published, status=201 if published.version == 1 else 200)

    async def api_create(self, request: Request) -> Response:
        """``POST /api/artifacts``: multipart files into a new, always private artifact."""
        caller = await self._authenticate(request, Scope.WRITE, route="api_create")
        if isinstance(caller, Response):
            return caller
        service = self._services.artifacts
        try:
            await self._spend(Bucket.WRITES, caller, route="api_create")
            files, fields = await self._form(request)
            if fields.title is None:
                raise Invalid("a title is required")
            if fields.expires_in == NEVER or fields.merge:
                raise Invalid("expires_in=never and merge apply to an update")
            draft = ArtifactDraft(
                title=fields.title,
                files=files,
                entry=fields.entry,
                tags=fields.tags,
                project=fields.project or None,
                note=fields.note,
                expires_at=self._expiry(fields.expires_in),
                client_ref=fields.client_ref,
            )
            published = await _run(partial(service.create, draft, client=caller))
        except VitrineError as error:
            return problem(error)
        return _published(published, status=201 if published.created else 200)

    async def api_update(self, request: Request) -> Response:
        """``PUT /api/artifacts/{id}``: the same body; adds a version, ``merge=true`` overlays."""
        artifact_id = request.path_params["id"]
        caller = await self._authenticate(
            request, Scope.WRITE, route="api_update", artifact=artifact_id
        )
        if isinstance(caller, Response):
            return caller
        service = self._services.artifacts
        try:
            await self._spend(Bucket.WRITES, caller, route="api_update", artifact=artifact_id)
            files, fields = await self._form(request)
            if fields.client_ref is not None:
                raise Invalid("client_ref applies to a create")
            draft = VersionDraft(
                files=files or None,
                merge=fields.merge,
                entry=fields.entry,
                title=fields.title,
                tags=fields.tags or None,
                project=fields.project,
                note=fields.note,
                expires_at=None if fields.expires_in == NEVER else self._expiry(fields.expires_in),
                clear_expiry=fields.expires_in == NEVER,
            )
            published = await _run(partial(service.update, artifact_id, draft, client=caller))
        except VitrineError as error:
            return problem(error)
        return _published(published, status=200)

    async def api_list(self, request: Request) -> Response:
        """``GET /api/artifacts``: the same filters and page as ``artifact_list``."""
        caller = await self._authenticate(request, Scope.READ, route="api_list")
        if isinstance(caller, Response):
            return caller
        try:
            await self._spend(Bucket.READS, caller, route="api_list")
            params: dict[str, Any] = dict(request.query_params)
            if "expiring_within" in params:
                params["expiring_within"] = requested_duration(params["expiring_within"])
            try:
                query = ArtifactQuery.model_validate(params)
            except ValidationError as error:
                raise Invalid(_describe(error)) from None
            page = await _run(partial(self._services.artifacts.list, query))
        except VitrineError as error:
            return problem(error)
        return JSONResponse(page.model_dump(mode="json"), headers=security_headers())

    async def health(self, request: Request) -> Response:
        """``GET /healthz``: 200 while ``data_dir`` is writable, else 503."""
        staging = self._settings.data_dir / layout.TMP_DIR
        healthy = await _run(partial(os.access, staging, os.W_OK))
        return JSONResponse(
            {"ok": healthy}, status_code=200 if healthy else 503, headers=security_headers()
        )

    async def _view(
        self, request: Request, prefix: Prefix, open_artifact: Callable[..., Artifact]
    ) -> Response:
        """Rate-limit, open, resolve and serve one file under the response policy (§ 7.2)."""
        service = self._services.artifacts
        try:
            client = request.client.host if request.client else "-"
            self._services.limits.check(Bucket.VIEWS, client)
            key, version = _pinned(request.path_params["ref"])
            artifact = await _run(partial(open_artifact, key, version=version))
            path = request.path_params["path"]
            if not path:
                path = await _run(partial(service.entry_of, artifact, version=version))
                if "/" in path:
                    # A nested entry is redirected to, so its relative links resolve beside it.
                    return RedirectResponse(
                        _relative(path), status_code=307, headers=security_headers()
                    )
            raw = request.query_params.get("raw") == "1"
            download = request.query_params.get("download") == "1"
            file = await _run(partial(service.locate, artifact, path, version=version))
            render = None if raw or download else renderer_for(path, self._settings.renderers)
            if render is not None and (await _run(file.stat)).st_size <= RENDER_LIMIT:
                data = await _run(file.read_bytes)
                self._services.metrics.viewed(prefix.value)
                return rendered(await _run(partial(render, data, path)), prefix=prefix)
        except VitrineError as error:
            return problem(error, view=True)
        self._services.metrics.viewed(prefix.value)
        return artifact_file(file, path, prefix=prefix, download=download)

    async def _authenticate(
        self, request: Request, scope: Scope, *, route: str, artifact: str | None = None
    ) -> str | Response:
        """The client a bearer token names, or the 401 or 403 to answer with."""
        match = BEARER.fullmatch(request.headers.get("authorization", "").strip())
        entry = self._services.tokens.identify(match.group(1)) if match else None
        if entry is None:
            return unauthorized()
        if scope not in entry.scopes:
            try:
                await self._deny(entry.name, DenialReason.SCOPE, route, artifact)
            except VitrineError as error:
                return problem(error)
            return forbidden()
        return entry.name

    async def _spend(
        self, bucket: Bucket, client: str, *, route: str, artifact: str | None = None
    ) -> None:
        """Spend one token of the caller's bucket before any work, auditing a refusal."""
        try:
            self._services.limits.check(bucket, client)
        except VitrineError:
            await self._deny(client, DenialReason.RATE_LIMITED, route, artifact)
            raise

    async def _deny(
        self, client: str, reason: DenialReason, route: str, artifact: str | None
    ) -> None:
        """Append one ``access_denied`` event, off the event loop (it fsyncs)."""
        await _run(
            partial(
                self._services.audit.deny,
                client=client,
                reason=reason,
                route=route,
                artifact=artifact if artifact is not None and is_valid_id(artifact) else None,
            )
        )

    async def _form(self, request: Request) -> tuple[dict[str, bytes], _Fields]:
        """Parse the multipart body; a body the parser refuses is a 400, not Starlette's own."""
        try:
            async with request.form(
                max_files=PARSER_MAX_FILES, max_fields=PARSER_MAX_FIELDS
            ) as form:
                return await self._files(form), _fields(form)
        except HTTPException as error:
            raise Invalid(f"the body is not valid multipart: {error.detail}") from None

    async def _files(self, form: FormData) -> dict[str, bytes]:
        """The ``file`` parts, keyed by the relative path each part's filename carries."""
        files: dict[str, bytes] = {}
        for name, value in form.multi_items():
            if name != "file":
                continue
            if not isinstance(value, UploadFile) or not value.filename:
                raise Invalid("each file part needs a filename, which is its relative path")
            if value.filename in files:
                raise Invalid(f"{value.filename} is given twice")
            files[value.filename] = await value.read()
        if len(files) > self._settings.max_files:
            raise TooLarge(f"a version holds at most {self._settings.max_files} files")
        return files

    def _expiry(self, expires_in: str | None) -> datetime | None:
        """The instant ``expires_in`` lands on, on the service's clock."""
        if expires_in is None:
            return None
        return self._services.artifacts.expiry(requested_duration(expires_in))


def build_routes(settings: Settings, services: Services) -> list[Route]:
    """The web routes of spec § 7.1, bound to one set of services."""
    return WebRoutes(settings, services).routes()


async def _run[T](call: Callable[[], T]) -> T:
    """Run blocking filesystem work off the event loop."""
    return await to_thread.run_sync(call)


def _relative(path: str) -> str:
    """A ``Location`` that stays a path: ``./`` and escaping keep ``a:b`` from being a scheme."""
    return "./" + "/".join(quote(segment, safe="") for segment in path.split("/"))


def _pinned(reference: str) -> tuple[str, int | None]:
    """Split ``<id-or-sig>@<n>``; a malformed pin is as unknown as a malformed id (§ 7.1)."""
    key, mark, number = reference.rpartition(VERSION_MARK)
    if not mark:
        return reference, None
    # The length cap comes first: int() refuses a string of thousands of digits with ValueError.
    if not (number.isascii() and number.isdigit() and len(number) <= MAX_PIN_DIGITS):
        raise NotFound()
    if int(number) < 1:
        raise NotFound()
    return key, int(number)


def _fields(form: FormData) -> _Fields:
    """Read and bound the text fields; a file where text belongs is refused."""
    values: dict[str, Any] = {"tags": []}
    for name, value in form.multi_items():
        if name == "file":
            continue
        if not isinstance(value, str):
            raise Invalid(f"{name} must be a text field")
        if name == "tags":
            values["tags"].append(value)
        elif name in values:
            raise Invalid(f"{name} is given twice")
        else:
            values[name] = value
    try:
        return _Fields.model_validate(values)
    except ValidationError as error:
        raise Invalid(_describe(error)) from None


def _published(published: PublishedArtifact, *, status: int) -> Response:
    """``{id, url, version, expires_at}``: always a private link (spec § 7.1)."""
    body = published.model_dump(mode="json", include={"id", "url", "version", "expires_at"})
    return JSONResponse(body, status_code=status, headers=security_headers())


def _describe(error: ValidationError) -> str:
    """One line naming each refused field, without echoing the value."""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'request'}: {item['msg']}"
        for item in error.errors()
    )
