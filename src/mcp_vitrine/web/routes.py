"""The HTTP routes of spec § 7.1. Each one authenticates itself (security T3).

The MCP auth provider covers ``/mcp`` only, so every route here names how it is authorised in
``ROUTE_ACCESS``, and ``tests/http/routes_spec.py`` fails on a route that is not listed there.
Like a tool, a route stays thin: it checks the caller, translates, calls a service and maps a
``VitrineError`` through :func:`problem`, which is the only place a status is chosen.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from functools import partial
from http import HTTPStatus
from pathlib import PurePosixPath
from typing import Annotated, Any
from urllib.parse import quote

from anyio import to_thread
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
    field_validator,
)
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp_vitrine.config import Settings, requested_duration
from mcp_vitrine.domain import layout
from mcp_vitrine.domain.errors import (
    Invalid,
    Locked,
    NotFound,
    RateLimited,
    TooLarge,
    UnsupportedType,
    VitrineError,
)
from mcp_vitrine.domain.ids import CLIENT_REF_MAX, LID_PATTERN, is_valid_id
from mcp_vitrine.domain.models import (
    PROJECT_PATTERN,
    TAG_LIMIT,
    TAG_MAX,
    TITLE_MAX,
    ArtifactDraft,
    ArtifactQuery,
    Content,
    MetaChange,
    PublishedArtifact,
    Scope,
    Spooled,
    VersionDraft,
)
from mcp_vitrine.domain.paths import MAX_PATH_BYTES, validate_path
from mcp_vitrine.services import archives
from mcp_vitrine.services.archives import UNPACK_ZIP, ZIP_TYPE, ArchiveLimits, Unpacked
from mcp_vitrine.services.artifacts import Opened
from mcp_vitrine.services.audit import DenialReason
from mcp_vitrine.services.container import Services
from mcp_vitrine.services.limits import Bucket
from mcp_vitrine.services.views import is_unfurler
from mcp_vitrine.web.jsx import STATIC_PREFIX, JsxBundle
from mcp_vitrine.web.renderers import RENDER_LIMIT, renderer_for
from mcp_vitrine.web.responses import (
    Prefix,
    artifact_file,
    forbidden,
    problem,
    rendered,
    security_headers,
    unauthorized,
    unlock_form,
)
from mcp_vitrine.web.spool import FilePart, Spool, spooled

NEVER = "never"
DURATION = r"^\d+[smhd]?$"
BEARER = re.compile(r"^bearer\s+(\S+)$", re.IGNORECASE)
PARSER_MAX_FIELDS = 64
VERSION_MARK = "@"
# More digits than any real version number needs.
MAX_PIN_DIGITS = 9
UNLOCK_FIELDS = 4
NOT_FOUND_PAGE = "404.html"
IMMUTABLE = "public, max-age=31536000, immutable"
# A JSON body names a few short fields; anything near this is not a request vitrine understands.
JSON_BODY = 16 * 1024
JSON_TYPE = "application/json"
# Requests a browser makes ahead of a click: not someone opening the link.
PREFETCH = frozenset({"prefetch", "prerender"})


class Access(StrEnum):
    """How a route proves the caller may use it (security § 6.2)."""

    PUBLIC = "public"
    SIGNATURE = "signature"
    BEARER = "bearer"
    MCP = "mcp"


ROUTE_ACCESS: dict[str, Access] = {
    "view_public": Access.PUBLIC,
    "view_private": Access.SIGNATURE,
    "unlock_private": Access.SIGNATURE,
    "upload": Access.SIGNATURE,
    "api_create": Access.BEARER,
    "api_update": Access.BEARER,
    "api_list": Access.BEARER,
    "api_get": Access.BEARER,
    "api_delete": Access.BEARER,
    "api_meta": Access.BEARER,
    "api_link": Access.BEARER,
    "api_revoke": Access.BEARER,
    "health": Access.PUBLIC,
    "static_asset": Access.PUBLIC,
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
    # An empty fallback clears it on an update.
    fallback: Annotated[str | None, Field(max_length=MAX_PATH_BYTES)] = None
    unpack: Annotated[str | None, Field(pattern=f"^{UNPACK_ZIP}$")] = None

    @field_validator("tags", mode="before")
    @classmethod
    def _split_tags(cls, value: object) -> object:
        """Accept repeated ``tags`` fields, each of which may itself be comma-separated."""
        if isinstance(value, list):
            return tuple(
                tag.strip() for item in value for tag in str(item).split(",") if tag.strip()
            )
        return value


class _Body(BaseModel):
    """A JSON body of the ``/api`` routes: an unknown key is a 400, never ignored."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class _MetaBody(_Body):
    """``PATCH``: the fields of ``artifact_set_meta``. A visibility is not one of them."""

    title: str | None = None
    tags: list[str] | None = None
    add_tags: list[str] | None = None
    remove_tags: list[str] | None = None
    project: str | None = None

    def to_change(self) -> MetaChange:
        """The service's change, validated by the same model the tool builds."""
        try:
            return MetaChange(
                title=self.title,
                tags=None if self.tags is None else tuple(self.tags),
                add_tags=tuple(self.add_tags or ()),
                remove_tags=tuple(self.remove_tags or ()),
                project=self.project,
            )
        except ValidationError as error:
            raise Invalid(_describe(error)) from None


class _LinkBody(_Body):
    """``POST …/links``: the arguments of ``artifact_link``."""

    ttl: Annotated[str | None, Field(pattern=DURATION)] = None
    label: Annotated[str | None, Field(max_length=TITLE_MAX)] = None
    # Strict, so "true" as a string is a supplied passcode, not a request to generate one.
    passcode: Annotated[StrictBool | StrictStr | None, Field(union_mode="left_to_right")] = None


class _RevokeBody(_Body):
    """``POST …/revoke``: one ``lid``, or nothing for every link."""

    link: Annotated[str | None, Field(pattern=LID_PATTERN.pattern)] = None


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
            Route("/s/{ref}/unlock", self.unlock_private, methods=["POST"], name="unlock_private"),
            Route("/s/{ref}/{path:path}", self.view_private, methods=["GET"], name="view_private"),
            Route("/u/{sig}", self.upload, methods=["PUT"], name="upload"),
            Route("/api/artifacts", self.api_create, methods=["POST"], name="api_create"),
            Route("/api/artifacts", self.api_list, methods=["GET"], name="api_list"),
            Route("/api/artifacts/{id}", self.api_update, methods=["PUT"], name="api_update"),
            Route("/api/artifacts/{id}", self.api_get, methods=["GET"], name="api_get"),
            Route("/api/artifacts/{id}", self.api_delete, methods=["DELETE"], name="api_delete"),
            Route("/api/artifacts/{id}", self.api_meta, methods=["PATCH"], name="api_meta"),
            Route("/api/artifacts/{id}/links", self.api_link, methods=["POST"], name="api_link"),
            Route(
                "/api/artifacts/{id}/revoke", self.api_revoke, methods=["POST"], name="api_revoke"
            ),
            Route("/healthz", self.health, methods=["GET"], name="health"),
            Route(
                f"{STATIC_PREFIX}/{{path:path}}",
                self.static_asset,
                methods=["GET"],
                name="static_asset",
            ),
        ]

    async def view_public(self, request: Request) -> Response:
        """``GET /a/{id}[@n]/[path]``: a public artifact, no credentials."""
        return await self._view(request, Prefix.PUBLIC, self._services.artifacts.open_public)

    async def view_private(self, request: Request) -> Response:
        """``GET /s/{sig}[@n]/[path]``: any artifact the signature names."""
        return await self._view(request, Prefix.SIGNED, self._services.artifacts.open_signed)

    async def unlock_private(self, request: Request) -> Response:
        """``POST /s/{sig}/unlock``: trade a link's passcode for an unlocked URL (spec § 7.1).

        Only a wrong passcode spends the two buckets; an empty one answers 429 before the
        passcode is looked at. The redirect is ``303``, so the browser follows with a ``GET``.
        """
        service = self._services.artifacts
        limits = self._services.limits
        reference = request.path_params["ref"]
        spent: list[tuple[Bucket, str]] = []
        wrong = False
        try:
            signature, version = _pinned(reference)
            claims = service.view_claims(signature)
            passcode, path = await self._unlock_fields(request)
            address = request.client.host if request.client else "-"
            link = f"{claims.id}|{claims.lid or '-'}"
            for bucket, key in ((Bucket.UNLOCK, f"{link}|{address}"), (Bucket.UNLOCK_LINK, link)):
                limits.check(bucket, key)
                spent.append((bucket, key))
            try:
                unlocked = await _run(partial(service.unlock, signature, passcode))
            except Locked:
                wrong = True
                self._services.metrics.unlocked("wrong")
                return unlock_form(reference, path, wrong=True)
        except VitrineError as error:
            if isinstance(error, RateLimited):
                self._services.metrics.unlocked("rate_limited")
            return problem(error, view=True)
        finally:
            if not wrong:
                for bucket, key in spent:
                    limits.refund(bucket, key)
        if unlocked is None:
            # No passcode on this link: it opens as it is.
            return _see_other(reference, path)
        self._services.metrics.unlocked("ok")
        pin = "" if version is None else f"{VERSION_MARK}{version}"
        return _see_other(f"{unlocked}{pin}", path)

    async def upload(self, request: Request) -> Response:
        """``PUT /u/{sig}?filename=`` or ``?unpack=zip``: one raw body, once per URL (§ 7.3)."""
        service = self._services.artifacts
        try:
            claims = service.upload_claims(request.path_params["sig"])
            client = claims.client
            # A removed client, or one no longer allowed to write, has revoked its own URLs.
            if client is None or not self._services.tokens.allows(client, Scope.WRITE):
                raise NotFound()
            await self._spend(Bucket.WRITES, client, route="upload", artifact=claims.id)
            query = request.query_params
            unpack = _unpack(query.get("unpack"))
            filename = query.get("filename")
            if not filename and not unpack:
                raise Invalid("name the file: PUT /u/<sig>?filename=report.pdf")
            title = query.get("title")
            if title is not None and len(title) > TITLE_MAX:
                raise Invalid(f"a title is at most {TITLE_MAX} characters")
            async with spooled(self._settings.data_dir) as spool:
                body = await spool.body(request)
                if not unpack:
                    published = await _run(
                        partial(service.upload, claims, filename or "", body, title=title)
                    )
                    return _published(published, status=201 if published.version == 1 else 200)
                site = await _run(partial(self._unpack, body, spool))
                # Read before the write, which moves the spooled files into the version.
                extra = await _run(partial(_site, site))
                published = await _run(
                    partial(
                        service.upload_site,
                        claims,
                        site.files,
                        title=title,
                        merge=query.get("merge") == "true",
                    )
                )
        except VitrineError as error:
            return problem(error)
        return _published(published, status=201 if published.version == 1 else 200, site=extra)

    async def api_create(self, request: Request) -> Response:
        """``POST /api/artifacts``: multipart files into a new, always private artifact."""
        caller = await self._authenticate(request, Scope.WRITE, route="api_create")
        if isinstance(caller, Response):
            return caller
        service = self._services.artifacts
        try:
            await self._spend(Bucket.WRITES, caller, route="api_create")
            async with spooled(self._settings.data_dir) as spool:
                files, fields, site = await self._form(request, spool)
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
                    fallback=fields.fallback or None,
                )
                extra = await _run(partial(_site, site, entry=fields.entry))
                published = await _run(partial(service.create, draft, client=caller))
        except VitrineError as error:
            return problem(error)
        return _published(published, status=201 if published.created else 200, site=extra)

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
            async with spooled(self._settings.data_dir) as spool:
                files, fields, site = await self._form(request, spool)
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
                    expires_at=None
                    if fields.expires_in == NEVER
                    else self._expiry(fields.expires_in),
                    clear_expiry=fields.expires_in == NEVER,
                    fallback=fields.fallback,
                )
                extra = await _run(partial(_site, site, entry=fields.entry))
                published = await _run(partial(service.update, artifact_id, draft, client=caller))
        except VitrineError as error:
            return problem(error)
        return _published(published, status=200, site=extra)

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

    async def api_get(self, request: Request) -> Response:
        """``GET /api/artifacts/{id}[?version=]``: as ``artifact_get``, with no URL in it."""
        artifact_id = request.path_params["id"]
        caller = await self._authenticate(
            request, Scope.READ, route="api_get", artifact=artifact_id
        )
        if isinstance(caller, Response):
            return caller
        try:
            await self._spend(Bucket.READS, caller, route="api_get", artifact=artifact_id)
            version = _version(request.query_params.get("version"))
            detail = await _run(partial(self._services.artifacts.get, artifact_id, version=version))
        except VitrineError as error:
            return problem(error)
        return _json_response(detail.model_dump(mode="json"))

    async def api_delete(self, request: Request) -> Response:
        """``DELETE /api/artifacts/{id}``: as ``artifact_delete``; every link stops at once."""
        artifact_id = request.path_params["id"]
        caller = await self._authenticate(
            request, Scope.WRITE, route="api_delete", artifact=artifact_id
        )
        if isinstance(caller, Response):
            return caller
        try:
            await self._spend(Bucket.WRITES, caller, route="api_delete", artifact=artifact_id)
            await _run(partial(self._services.artifacts.delete, artifact_id, client=caller))
        except VitrineError as error:
            return problem(error)
        return _json_response({"deleted": artifact_id})

    async def api_meta(self, request: Request) -> Response:
        """``PATCH /api/artifacts/{id}``: as ``artifact_set_meta``; a visibility is refused."""
        artifact_id = request.path_params["id"]
        caller = await self._authenticate(
            request, Scope.WRITE, route="api_meta", artifact=artifact_id
        )
        if isinstance(caller, Response):
            return caller
        try:
            await self._spend(Bucket.WRITES, caller, route="api_meta", artifact=artifact_id)
            body = await _json_body(request)
            change = _model(_MetaBody, body)
            artifact = await _run(
                partial(
                    self._services.artifacts.set_meta,
                    artifact_id,
                    change.to_change(),
                    client=caller,
                )
            )
        except VitrineError as error:
            return problem(error)
        return _json_response(
            {
                "id": artifact.id,
                "title": artifact.title,
                "tags": list(artifact.tags),
                "project": artifact.project,
            }
        )

    async def api_link(self, request: Request) -> Response:
        """``POST /api/artifacts/{id}/links``: as ``artifact_link``; a passcode is shown once."""
        artifact_id = request.path_params["id"]
        caller = await self._authenticate(
            request, Scope.SHARE, route="api_link", artifact=artifact_id
        )
        if isinstance(caller, Response):
            return caller
        try:
            await self._spend(Bucket.LINKS, caller, route="api_link", artifact=artifact_id)
            fields = _model(_LinkBody, await _json_body(request))
            link = await _run(
                partial(
                    self._services.artifacts.link,
                    artifact_id,
                    client=caller,
                    ttl=None if fields.ttl is None else requested_duration(fields.ttl),
                    label=fields.label or "",
                    passcode=fields.passcode,
                )
            )
        except VitrineError as error:
            return problem(error)
        return _json_response(link.model_dump(mode="json"), status=201)

    async def api_revoke(self, request: Request) -> Response:
        """``POST /api/artifacts/{id}/revoke``: one link by ``lid``, or every private link."""
        artifact_id = request.path_params["id"]
        caller = await self._authenticate(
            request, Scope.SHARE, route="api_revoke", artifact=artifact_id
        )
        if isinstance(caller, Response):
            return caller
        try:
            await self._spend(Bucket.LINKS, caller, route="api_revoke", artifact=artifact_id)
            fields = _model(_RevokeBody, await _json_body(request))
            artifact = await _run(
                partial(
                    self._services.artifacts.revoke_links,
                    artifact_id,
                    client=caller,
                    lid=fields.link,
                )
            )
        except VitrineError as error:
            return problem(error)
        return _json_response(
            {"id": artifact.id, "link_epoch": artifact.link_epoch, "link": fields.link}
        )

    async def health(self, request: Request) -> Response:
        """``GET /healthz``: 200 while ``data_dir`` is writable, else 503."""
        staging = self._settings.data_dir / layout.TMP_DIR
        healthy = await _run(partial(os.access, staging, os.W_OK))
        return JSONResponse(
            {"ok": healthy}, status_code=200 if healthy else 503, headers=security_headers()
        )

    async def static_asset(self, request: Request) -> Response:
        """``GET /_vitrine/jsx/{bundle}/{file}``: the JSX renderer's vendored runtime (§ 7.1).

        The content is vitrine's own and the URL names its version, so it is cached for good and
        open to any origin: a rendered page runs in an opaque one, which makes its module loads
        cross-origin.
        """
        found = JsxBundle.load().asset(request.path_params["path"])
        if found is None:
            return problem(NotFound(), view=True)
        content, kind = found
        headers = security_headers(IMMUTABLE)
        headers["Access-Control-Allow-Origin"] = "*"
        headers["Cross-Origin-Resource-Policy"] = "cross-origin"
        return Response(content, media_type=f"{kind}; charset=utf-8", headers=headers)

    async def _view(
        self, request: Request, prefix: Prefix, open_artifact: Callable[..., Opened]
    ) -> Response:
        """Rate-limit, open, resolve and serve one file under the response policy (§ 7.2)."""
        service = self._services.artifacts
        try:
            client = request.client.host if request.client else "-"
            self._services.limits.check(Bucket.VIEWS, client)
            reference = request.path_params["ref"]
            key, version = _pinned(reference)
            try:
                opened = await _run(partial(open_artifact, key, version=version))
            except Locked:
                return unlock_form(reference, request.path_params["path"])
            artifact = opened.artifact
            path = request.path_params["path"]
            entry = await _run(partial(service.entry_of, artifact, version=version))
            if not path:
                path = entry
                if "/" in path:
                    # A nested entry is redirected to, so its relative links resolve beside it.
                    return RedirectResponse(
                        _relative(path), status_code=307, headers=security_headers()
                    )
            raw = request.query_params.get("raw") == "1"
            download = request.query_params.get("download") == "1"
            # A missing route of a site serves its fallback: a 404 page as 404, an SPA as 200.
            status = HTTPStatus.OK
            try:
                file = await _run(partial(service.locate, artifact, path, version=version))
            except NotFound:
                fallback = service.fallback_for(artifact, path)
                if fallback is None:
                    raise
                file = await _run(partial(service.locate, artifact, fallback, version=version))
                if PurePosixPath(fallback).name == NOT_FOUND_PAGE:
                    status = HTTPStatus.NOT_FOUND
                path = fallback
            # An SPA's fallback is its page under another route, so it is a view of it too.
            page_view = status is HTTPStatus.OK and path in {entry, artifact.fallback}
            render = (
                None
                if raw or download
                else renderer_for(path, self._settings.renderers, jsx_cdn=self._settings.jsx_cdn)
            )
            if render is not None and (await _run(file.stat)).st_size <= RENDER_LIMIT:
                data = await _run(file.read_bytes)
                html = await _run(partial(render, data, path))
                self._served(request, prefix, opened, counted=status is HTTPStatus.OK)
                return rendered(html, prefix=prefix, status=status)
        except VitrineError as error:
            return problem(error, view=True)
        self._served(request, prefix, opened, counted=page_view)
        return artifact_file(file, path, prefix=prefix, download=download, status=status)

    def _served(self, request: Request, prefix: Prefix, opened: Opened, *, counted: bool) -> None:
        """Count a served file in the metrics, and a page view in the view log (§ 11.4).

        A view is a page someone opened: the entry or a rendered page, fetched with a plain
        ``GET``. Assets, ``HEAD``, ``Range`` and prefetches are not views.
        """
        agent = request.headers.get("user-agent", "")
        self._services.metrics.viewed(prefix.value, "bot" if is_unfurler(agent) else "view")
        purpose = request.headers.get("sec-purpose", request.headers.get("purpose", ""))
        if (
            not counted
            or request.method != "GET"
            or "range" in request.headers
            or purpose.split(";")[0].strip().lower() in PREFETCH
        ):
            return
        self._services.artifacts.count_view(
            opened,
            route=prefix.value,
            address=request.client.host if request.client else "-",
            user_agent=agent,
        )

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

    async def _unlock_fields(self, request: Request) -> tuple[str, str]:
        """The passcode and the path asked for; a path that is not one opens the entry."""
        try:
            async with request.form(max_files=0, max_fields=UNLOCK_FIELDS) as form:
                passcode = form.get("passcode")
                path = form.get("path")
        except HTTPException as error:
            raise Invalid(f"the body is not a form: {error.detail}") from None
        passcode = passcode if isinstance(passcode, str) else ""
        path = path if isinstance(path, str) else ""
        try:
            validate_path(path)
        except Invalid:
            path = ""
        return passcode, path

    async def _form(
        self, request: Request, spool: Spool
    ) -> tuple[dict[str, Content], _Fields, Unpacked | None]:
        """Stream the multipart body to the spool; with ``unpack=zip``, unpack its one archive."""
        form = await spool.form(
            request, max_files=self._settings.max_files, max_fields=PARSER_MAX_FIELDS
        )
        fields = _fields(form.fields)
        if any(part.name != "file" for part in form.files):
            raise Invalid("files are sent as parts named file")
        if not fields.unpack:
            return _files(form.files), fields, None
        if len(form.files) != 1 or form.files[0].content_type != ZIP_TYPE:
            raise Invalid(f"unpack=zip takes exactly one file part of type {ZIP_TYPE}")
        site = await _run(partial(self._unpack, form.files[0].content, spool))
        return dict(site.files), fields, site

    def _unpack(self, archive: Spooled, spool: Spool) -> Unpacked:
        """Inflate an archive into the spool under the version's own limits (spec § 7.3)."""
        limits = ArchiveLimits(
            max_files=self._settings.max_files,
            max_bytes=self._settings.max_artifact,
            max_ratio=self._settings.unpack_max_ratio,
        )
        return archives.unpack(archive.path, spool.path(), limits)

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


def _see_other(reference: str, path: str) -> Response:
    """``303`` to a path under ``/s/{reference}/``, every segment escaped, never off-site."""
    location = f"/s/{reference}/" + "/".join(quote(part, safe="") for part in path.split("/"))
    return RedirectResponse(location, status_code=303, headers=security_headers())


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


def _fields(items: list[tuple[str, str]]) -> _Fields:
    """Read and bound the text fields; a field given twice is refused, ``tags`` aside."""
    values: dict[str, Any] = {"tags": []}
    for name, value in items:
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


def _files(parts: list[FilePart]) -> dict[str, Content]:
    """The ``file`` parts, keyed by the relative path each part's filename carries."""
    files: dict[str, Content] = {}
    for part in parts:
        if not part.filename:
            raise Invalid("each file part needs a filename, which is its relative path")
        if part.filename in files:
            raise Invalid(f"{part.filename} is given twice")
        files[part.filename] = part.content
    return files


def _unpack(value: str | None) -> bool:
    """``unpack=zip``, the one archive format; anything else named is refused."""
    if value is None:
        return False
    if value != UNPACK_ZIP:
        raise Invalid(f"unpack takes {UNPACK_ZIP}")
    return True


def _site(site: Unpacked | None, *, entry: str | None = None) -> dict[str, Any] | None:
    """What an unpacked archive adds to the result: its counts and its warnings."""
    if site is None:
        return None
    return {
        "files": len(site.files),
        "skipped": site.skipped,
        "warnings": list(archives.warnings(site.files, entry=entry)),
    }


async def _json_body(request: Request) -> dict[str, Any]:
    """The JSON object a request carries, counted while it streams; an empty body is ``{}``."""
    kind = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > JSON_BODY:
            raise TooLarge(f"a JSON body is at most {JSON_BODY // 1024} KiB")
    if not body:
        return {}
    if kind != JSON_TYPE:
        raise UnsupportedType(f"send the body as {JSON_TYPE}")
    try:
        value = json.loads(body)
    except ValueError:
        raise Invalid("the body is not valid JSON") from None
    if not isinstance(value, dict):
        raise Invalid("the body must be a JSON object")
    return value


def _model[M: BaseModel](model: type[M], body: dict[str, Any]) -> M:
    """Validate a JSON body, naming each refused field without echoing its value."""
    try:
        return model.model_validate(body)
    except ValidationError as error:
        raise Invalid(_describe(error)) from None


def _version(value: str | None) -> int | None:
    """``?version=n``: a positive number, or none for the current version."""
    if value is None:
        return None
    if not (value.isascii() and value.isdigit() and len(value) <= MAX_PIN_DIGITS) or not int(value):
        raise Invalid("version is a positive number")
    return int(value)


def _json_response(body: object, *, status: int = 200) -> Response:
    """A JSON answer of the ``/api`` routes, with the headers every response carries."""
    return JSONResponse(body, status_code=status, headers=security_headers())


def _published(
    published: PublishedArtifact, *, status: int, site: dict[str, Any] | None = None
) -> Response:
    """``{id, url, version, expires_at}``: always a private link (spec § 7.1).

    An unpacked archive adds ``files``, ``skipped`` and ``warnings``.
    """
    body = published.model_dump(mode="json", include={"id", "url", "version", "expires_at"})
    body |= site or {}
    return JSONResponse(body, status_code=status, headers=security_headers())


def _describe(error: ValidationError) -> str:
    """One line naming each refused field, without echoing the value."""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'request'}: {item['msg']}"
        for item in error.errors()
    )
