"""The operator dashboard under ``/_admin`` (spec § 7.6, ADR 0008).

It is off until ``admin_password`` is set. It signs in with that password only, never a client
token, and keeps the session in a ``SameSite=Strict`` cookie scoped to ``/_admin``. Every change
is a form ``POST`` that must carry vitrine's own ``Origin`` and the session's CSRF token. Phase 1
only narrows access: no route here makes anything public.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from functools import partial
from importlib import resources
from typing import Any
from urllib.parse import quote, urlsplit

from anyio import CapacityLimiter, to_thread
from pydantic import ValidationError
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp_vitrine.config import Settings
from mcp_vitrine.domain.errors import Invalid, NotFound, RateLimited, VitrineError
from mcp_vitrine.domain.ids import is_valid_id, is_valid_lid
from mcp_vitrine.domain.models import (
    TITLE_MAX,
    ArtifactQuery,
    GateDecision,
    ListSort,
    MetaChange,
    Visibility,
)
from mcp_vitrine.services.admin import ADMIN, SESSION_TTL, AdminLogin, Session
from mcp_vitrine.services.audit import AuditEventName
from mcp_vitrine.services.container import Services
from mcp_vitrine.services.limits import Bucket
from mcp_vitrine.web import pages
from mcp_vitrine.web.middleware import origin_of
from mcp_vitrine.web.pages import ADMIN_PREFIX, AuditView, DetailView, ListView, Minted
from mcp_vitrine.web.responses import INTERNAL, security_headers, status_of

COOKIE = "vitrine_admin"
# ``__Host-`` would pin ``Path=/`` and send the cookie with every artifact request (ADR 0008).
SECURE_COOKIE = f"__Secure-{COOKIE}"
DASHBOARD_CSP = (
    "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
# A form posts a few short fields; the login, a password.
FORM_FIELDS = 8
# The body limit of every route under the prefix; app.py applies it while the body streams.
FORM_BODY = 16 * 1024
# How long the link "Open" mints lasts: long enough to read, short enough to be one sitting.
OPEN_TTL = timedelta(hours=1)
OPEN_LABEL = "dashboard"
EXPIRIES = {"1d": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30)}
AUDIT_PAGE = 100
# More digits than any audit log has pages.
PAGE_DIGITS = 6
LIST_PAGE = 25
# Password checks that may run at once, whoever sends them.
PASSWORD_CHECKS = 2
CONFIRMABLE = frozenset({"private", "delete", "revoke"})
WRONG_PASSWORD = "That password didn't work."  # noqa: S105 - a message, not a password
TOO_MANY = "Too many attempts. Wait a minute, then try again."
FILTERS = ("q", "visibility", "project", "tag", "sort", "cursor")
STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}


class DashboardRoutes:
    """The dashboard's handlers, bound to the services and the login they were built with."""

    def __init__(self, settings: Settings, services: Services, login: AdminLogin) -> None:
        """Keep what the handlers need, and work out the cookie and the origin once."""
        self._settings = settings
        self._services = services
        self._login = login
        base = urlsplit(settings.public_base_url)
        self._secure = base.scheme == "https"
        self._cookie = SECURE_COOKIE if self._secure else COOKIE
        self._origin = settings.public_base_url
        # Compared as an origin, so an explicit default port on either side still matches.
        self._own = origin_of(settings.public_base_url)
        # Each check is a 32 MiB scrypt. The login bucket is per address, so without a cap
        # here a few addresses together could fill the thread pool and the memory limit.
        self._checks = CapacityLimiter(PASSWORD_CHECKS)

    def routes(self) -> list[Route]:
        """The table, named as ``ROUTE_ACCESS`` names it."""
        p = ADMIN_PREFIX
        return [
            Route(f"{p}/static/{{path:path}}", self.static, methods=["GET"], name="admin_static"),
            Route(f"{p}/login", self.login_form, methods=["GET"], name="admin_login_form"),
            Route(f"{p}/login", self.login, methods=["POST"], name="admin_login"),
            Route(f"{p}/logout", self.logout, methods=["POST"], name="admin_logout"),
            Route(f"{p}/", self.list, methods=["GET"], name="admin_list"),
            Route(f"{p}/audit", self.audit, methods=["GET"], name="admin_audit"),
            Route(f"{p}/a/{{id}}", self.artifact, methods=["GET"], name="admin_artifact"),
            Route(
                f"{p}/a/{{id}}/confirm/{{action}}",
                self.confirm,
                methods=["GET"],
                name="admin_confirm",
            ),
            Route(f"{p}/a/{{id}}/open", self.open, methods=["POST"], name="admin_open"),
            Route(f"{p}/a/{{id}}/links", self.link, methods=["POST"], name="admin_link"),
            Route(f"{p}/a/{{id}}/revoke", self.revoke, methods=["POST"], name="admin_revoke"),
            Route(f"{p}/a/{{id}}/meta", self.meta, methods=["POST"], name="admin_meta"),
            Route(f"{p}/a/{{id}}/private", self.private, methods=["POST"], name="admin_private"),
            Route(f"{p}/a/{{id}}/delete", self.delete, methods=["POST"], name="admin_delete"),
        ]

    async def static(self, request: Request) -> Response:
        """``GET /_admin/static/{file}``: the stylesheet, the fonts and the one script."""
        path = request.path_params["path"]
        kind = next((kind for suffix, kind in STATIC_TYPES.items() if path.endswith(suffix)), None)
        found = _static_file(path) if kind is not None else None
        if found is None or kind is None:
            return self._page(pages.error(404, str(NotFound()), csrf=None), status=404)
        headers = security_headers("public, max-age=86400")
        return Response(found, media_type=kind, headers=headers)

    async def login_form(self, request: Request) -> Response:
        """``GET /_admin/login``: the form, or the list for someone already signed in."""
        if self._session(request) is not None:
            return _see_other(f"{ADMIN_PREFIX}/")
        return self._page(pages.login())

    async def login(self, request: Request) -> Response:
        """``POST /_admin/login``: a right password earns the session cookie.

        Only a wrong password spends ``rate_admin_login``; an empty bucket answers 429 before
        the password is looked at, like the passcode unlock.
        """
        refused = self._foreign(request)
        if refused is not None:
            return refused
        limits = self._services.limits
        address = request.client.host if request.client else "-"
        try:
            limits.check(Bucket.ADMIN_LOGIN, address)
        except RateLimited as error:
            limited = self._page(pages.login(message=TOO_MANY), status=429)
            limited.headers["Retry-After"] = str(error.retry_after)
            return limited
        form = await _form(request)
        password = form.get("password") if form is not None else None
        right = isinstance(password, str) and await to_thread.run_sync(
            partial(self._login.verify, password), limiter=self._checks
        )
        if not right:
            return self._page(pages.login(message=WRONG_PASSWORD), status=401)
        limits.refund(Bucket.ADMIN_LOGIN, address)
        response = _see_other(f"{ADMIN_PREFIX}/")
        response.set_cookie(
            self._cookie,
            self._login.issue(),
            max_age=int(SESSION_TTL.total_seconds()),
            path=ADMIN_PREFIX,
            secure=self._secure,
            httponly=True,
            samesite="strict",
        )
        return response

    async def logout(self, request: Request) -> Response:
        """``POST /_admin/logout``: drop the cookie. The session itself ends at its expiry."""
        checked = await self._checked(request)
        if isinstance(checked, Response):
            return checked
        response = _see_other(f"{ADMIN_PREFIX}/login")
        response.delete_cookie(
            self._cookie, path=ADMIN_PREFIX, secure=self._secure, httponly=True, samesite="strict"
        )
        return response

    async def list(self, request: Request) -> Response:
        """``GET /_admin/``: every artifact, filtered, searched and paged."""
        session = self._session(request)
        if session is None:
            return _to_login()
        filters = {key: value for key in FILTERS if (value := request.query_params.get(key))}
        try:
            query = ArtifactQuery(
                query=filters.get("q"),
                visibility=_visibility(filters.get("visibility")),
                project=filters.get("project"),
                tag=filters.get("tag"),
                sort=_sort(filters.get("sort")),
                cursor=filters.get("cursor"),
                facets=True,
                limit=LIST_PAGE,
            )
        except ValidationError:
            return _see_other(f"{ADMIN_PREFIX}/")
        artifacts = self._services.artifacts
        try:
            listing = await _run(partial(artifacts.list, query))
            total = (await _run(artifacts.usage))[0]
            public = await _run(partial(_count_public, artifacts.list))
        except VitrineError as error:
            return self._error(error, session)
        view = ListView(
            page=listing,
            filters=filters,
            total=total,
            public=public,
            notice=request.query_params.get("done"),
        )
        return self._page(pages.artifact_list(view, csrf=session.csrf))

    async def artifact(self, request: Request) -> Response:
        """``GET /_admin/a/{id}``: one artifact."""
        session = self._session(request)
        if session is None:
            return _to_login()
        try:
            view = await self._detail(request.path_params["id"])
        except VitrineError as error:
            return self._error(error, session)
        view = replace(view, notice=request.query_params.get("done"))
        return self._page(pages.artifact(view, csrf=session.csrf))

    async def confirm(self, request: Request) -> Response:
        """``GET /_admin/a/{id}/confirm/{action}``: the step before a change that sticks."""
        session = self._session(request)
        if session is None:
            return _to_login()
        action = request.path_params["action"]
        try:
            if action not in CONFIRMABLE:
                raise NotFound()
            detail = await _run(partial(self._services.artifacts.get, _id(request)))
        except VitrineError as error:
            return self._error(error, session)
        if action == "private" and detail.artifact.visibility is not Visibility.PUBLIC:
            return _see_other(_artifact_url(detail.artifact.id))
        return self._page(pages.confirm(action, detail, csrf=session.csrf))

    async def audit(self, request: Request) -> Response:
        """``GET /_admin/audit``: the audit log, newest first, a hundred events a page."""
        session = self._session(request)
        if session is None:
            return _to_login()
        params = request.query_params
        filters = {
            key: value.strip()
            for key in ("client", "event", "artifact")
            if (value := params.get(key))
        }
        number = params.get("page", "1")
        page = (
            int(number)
            if number.isascii() and number.isdigit() and len(number) <= PAGE_DIGITS
            else 1
        )
        event = filters.get("event")
        kind = AuditEventName(event) if event in set(AuditEventName) else None
        try:
            found = await _run(
                partial(
                    self._services.audit.query,
                    artifact=filters.get("artifact"),
                    client=filters.get("client"),
                )
            )
        except VitrineError as error:
            return self._error(error, session)
        matched = [item for item in found if kind is None or item.event is kind]
        start = (max(page, 1) - 1) * AUDIT_PAGE
        view = AuditView(
            events=matched[start : start + AUDIT_PAGE],
            filters=filters,
            page=max(page, 1),
            more=len(matched) > start + AUDIT_PAGE,
        )
        return self._page(pages.audit(view, csrf=session.csrf))

    async def open(self, request: Request) -> Response:
        """``POST …/open``: the public URL, or a private link minted for one sitting."""
        checked = await self._checked(request)
        if isinstance(checked, Response):
            return checked
        session, _ = checked
        service = self._services.artifacts
        try:
            artifact_id = _id(request)
            detail = await _run(partial(service.get, artifact_id))
            if detail.artifact.visibility is Visibility.PUBLIC:
                url = f"{self._origin}/a/{artifact_id}/"
            else:
                link = await _run(
                    partial(service.link, artifact_id, client=ADMIN, ttl=OPEN_TTL, label=OPEN_LABEL)
                )
                url = link.url
        except VitrineError as error:
            return self._error(error, session)
        return _see_other(url)

    async def link(self, request: Request) -> Response:
        """``POST …/links``: a recorded private link, shown on the page that answers, once."""
        checked = await self._checked(request)
        if isinstance(checked, Response):
            return checked
        session, form = checked
        label = _text(form, "label")
        own = _text(form, "passcode")
        generate = form.get("generate") == "1"
        try:
            artifact_id = _id(request)
            ttl = EXPIRIES.get(_text(form, "expires_in"))
            if ttl is None:
                raise Invalid("choose when the link expires")
            if len(label) > TITLE_MAX:
                raise Invalid(f"a label is at most {TITLE_MAX} characters")
            passcode: str | bool | None = own or (True if generate else None)
            made = await _run(
                partial(
                    self._services.artifacts.link,
                    artifact_id,
                    client=ADMIN,
                    ttl=ttl,
                    label=label,
                    passcode=passcode,
                )
            )
            view = await self._detail(artifact_id)
        except VitrineError as error:
            return await self._refused(request, error, session)
        minted = Minted(
            label=label, url=made.url, passcode=made.passcode, has_passcode=passcode is not None
        )
        view = replace(view, minted=minted)
        return self._page(pages.artifact(view, csrf=session.csrf), status=201)

    async def revoke(self, request: Request) -> Response:
        """``POST …/revoke``: one link by ``lid``, or every private link."""
        checked = await self._checked(request)
        if isinstance(checked, Response):
            return checked
        session, form = checked
        lid = _text(form, "link") or None
        try:
            artifact_id = _id(request)
            if lid is not None and not is_valid_lid(lid):
                raise NotFound()
            await _run(
                partial(self._services.artifacts.revoke_links, artifact_id, client=ADMIN, lid=lid)
            )
        except VitrineError as error:
            return await self._refused(request, error, session)
        done = "revoked" if lid else "revoked-all"
        return _see_other(f"{_artifact_url(artifact_id)}?done={done}")

    async def meta(self, request: Request) -> Response:
        """``POST …/meta``: the title, the project and the tags; nothing else can change here."""
        checked = await self._checked(request)
        if isinstance(checked, Response):
            return checked
        session, form = checked
        title = _text(form, "title").strip()
        tags = tuple(tag.strip() for tag in _text(form, "tags").split(",") if tag.strip())
        try:
            artifact_id = _id(request)
            try:
                change = MetaChange(
                    title=title or None, tags=tags, project=_text(form, "project").strip()
                )
            except ValidationError as error:
                raise Invalid(_describe(error)) from None
            await _run(
                partial(self._services.artifacts.set_meta, artifact_id, change, client=ADMIN)
            )
        except VitrineError as error:
            return await self._refused(request, error, session)
        return _see_other(f"{_artifact_url(artifact_id)}?done=saved")

    async def private(self, request: Request) -> Response:
        """``POST …/private``: make it private. This narrows, so it needs no gate (spec § 6)."""
        checked = await self._checked(request)
        if isinstance(checked, Response):
            return checked
        session, _ = checked
        try:
            artifact_id = _id(request)
            await _run(
                partial(
                    self._services.artifacts.set_visibility,
                    artifact_id,
                    Visibility.PRIVATE,
                    client=ADMIN,
                    gate=GateDecision.NOT_REQUIRED,
                )
            )
        except VitrineError as error:
            return await self._refused(request, error, session)
        return _see_other(f"{_artifact_url(artifact_id)}?done=private")

    async def delete(self, request: Request) -> Response:
        """``POST …/delete``: delete it; every link stops at once."""
        checked = await self._checked(request)
        if isinstance(checked, Response):
            return checked
        session, _ = checked
        try:
            await _run(partial(self._services.artifacts.delete, _id(request), client=ADMIN))
        except VitrineError as error:
            return self._error(error, session)
        return _see_other(f"{ADMIN_PREFIX}/?done=deleted")

    async def _detail(self, artifact_id: str) -> DetailView:
        """What one artifact's page shows, read off the event loop."""
        if not is_valid_id(artifact_id):
            raise NotFound()
        service = self._services.artifacts
        detail = await _run(partial(service.get, artifact_id))
        history = await _run(partial(service.history, artifact_id))
        public = detail.artifact.visibility is Visibility.PUBLIC
        return DetailView(
            detail=detail,
            history=history,
            public_url=f"{self._origin}/a/{artifact_id}/" if public else None,
        )

    async def _refused(self, request: Request, error: VitrineError, session: Session) -> Response:
        """A refused change: the artifact's page with the reason, or the error page."""
        if not isinstance(error, Invalid):
            return self._error(error, session)
        try:
            view = await self._detail(request.path_params["id"])
        except VitrineError as missing:
            return self._error(missing, session)
        view = replace(view, error=str(error))
        return self._page(pages.artifact(view, csrf=session.csrf), status=400)

    async def _checked(self, request: Request) -> tuple[Session, FormData] | Response:
        """The session and the form of a change, or the refusal: origin, session, CSRF, in order.

        The origin is checked before the session, so a foreign page learns nothing; the CSRF
        token is read from the body last, after the cheaper checks passed.
        """
        refused = self._foreign(request)
        if refused is not None:
            return refused
        session = self._session(request)
        if session is None:
            return _to_login()
        form = await _form(request)
        token = form.get("csrf") if form is not None else None
        if (
            form is None
            or not isinstance(token, str)
            or not hmac.compare_digest(token.encode(), session.csrf.encode())
        ):
            return self._page(pages.error(403, "forbidden", csrf=session.csrf), status=403)
        return session, form

    def _foreign(self, request: Request) -> Response | None:
        """403 unless the request comes from vitrine's own pages (spec § 7.6).

        ``Origin`` must be present and be vitrine's own: every browser sends one on a ``POST``.
        ``Sec-Fetch-Site``, where the browser sends it, must say ``same-origin``.
        """
        origin = request.headers.get("origin")
        site = request.headers.get("sec-fetch-site")
        if (
            origin is None
            or origin_of(origin) != self._own
            or (site is not None and site != "same-origin")
        ):
            return self._page(pages.error(403, "forbidden origin", csrf=None), status=403)
        return None

    def _session(self, request: Request) -> Session | None:
        """The session the cookie proves; a bearer token is never one."""
        return self._login.session(request.cookies.get(self._cookie))

    def _error(self, error: VitrineError, session: Session | None) -> Response:
        """A domain error as a dashboard page, worded as ``problem`` words it (spec § 10)."""
        status = status_of(error)
        if status >= 500:  # noqa: PLR2004 - the server-error class
            message = INTERNAL
        elif status == 404:  # noqa: PLR2004 - the not-found status
            message = str(NotFound())
        else:
            message = str(error)
        csrf = session.csrf if session is not None else None
        return self._page(pages.error(status, message, csrf=csrf), status=status)

    def _page(self, html: str, *, status: int = 200) -> HTMLResponse:
        """A dashboard page under its own CSP; never cached, never framed."""
        headers = security_headers()
        headers["Content-Security-Policy"] = DASHBOARD_CSP
        # Under no-referrer a browser posts a form with ``Origin: null``, which the check refuses.
        headers["Referrer-Policy"] = "same-origin"
        # An artifact opened in a new tab cannot reach back into this one.
        headers["Cross-Origin-Opener-Policy"] = "same-origin"
        return HTMLResponse(html, status_code=status, headers=headers)


def build_dashboard(settings: Settings, services: Services) -> list[Route]:
    """The dashboard's routes, or none while ``admin_password`` is unset."""
    if services.admin is None:
        return []
    return DashboardRoutes(settings, services, services.admin).routes()


def _count_public(list_artifacts: Callable[[ArtifactQuery], Any]) -> int:
    """How many artifacts are public now, page by page."""
    count = 0
    cursor: str | None = None
    while True:
        page = list_artifacts(ArtifactQuery(visibility=Visibility.PUBLIC, limit=100, cursor=cursor))
        count += len(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return count


def _static_file(path: str) -> bytes | None:
    """One file of ``static/admin``, by a name that stays inside it, or ``None``."""
    parts = path.split("/")
    if not parts or any(not part or part.startswith(".") for part in parts):
        return None
    found = resources.files("mcp_vitrine.web").joinpath("static", "admin", *parts)
    return found.read_bytes() if found.is_file() else None


async def _form(request: Request) -> FormData | None:
    """The form a request carries, or ``None`` when it is not one."""
    try:
        return await request.form(max_files=0, max_fields=FORM_FIELDS)
    except HTTPException:
        return None


def _text(form: FormData, name: str) -> str:
    """One text field of a form, empty when absent."""
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _id(request: Request) -> str:
    """The artifact id in the path; a malformed one is as unknown as a missing one."""
    artifact_id: str = request.path_params["id"]
    if not is_valid_id(artifact_id):
        raise NotFound()
    return artifact_id


def _visibility(value: str | None) -> Visibility | None:
    """A visibility filter, or none for one that is not a visibility."""
    return Visibility(value) if value in set(Visibility) else None


def _sort(value: str | None) -> ListSort:
    """A sort, or the default for one that is not a sort."""
    return ListSort(value) if value in set(ListSort) else ListSort.UPDATED


def _artifact_url(artifact_id: str) -> str:
    """The dashboard page of one artifact."""
    return f"{ADMIN_PREFIX}/a/{quote(artifact_id, safe='')}"


def _see_other(location: str) -> Response:
    """``303``, so the browser follows with a ``GET``."""
    return RedirectResponse(location, status_code=303, headers=security_headers())


def _to_login() -> Response:
    """Where a request without a session goes."""
    return _see_other(f"{ADMIN_PREFIX}/login")


def _describe(error: ValidationError) -> str:
    """One line naming each refused field, without echoing the value."""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'request'}: {item['msg']}"
        for item in error.errors()
    )


async def _run[T](call: Callable[[], T]) -> T:
    """Run blocking filesystem work off the event loop."""
    return await to_thread.run_sync(call)
