"""The ASGI middleware in front of every route, ``/mcp`` included (design § 2).

Each is plain ASGI rather than Starlette's ``BaseHTTPMiddleware``, so a request body streams
through untouched and nothing is buffered on the way to the limit check.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp_vitrine.web.responses import PASSIVE_CSP, security_headers

ACCESS_LOGGER = "mcp_vitrine.access"
MCP_PATH = "/mcp"
SIGNED_PATH = "/s"
PUBLIC_PATH = "/a"
# An unlock posts one short form; nothing else under /s/ takes a body.
UNLOCK_BODY = 1024
REDACTED = "[redacted]"
# Leading slashes are collapsed first: "//s/<sig>" is not a route, but it is still a credential.
_SIGNED_SEGMENT = re.compile(r"^/+(s|u)/+[^/]+")

access_logger = logging.getLogger(ACCESS_LOGGER)


def hostname(value: str) -> str:
    """The host part of a ``Host`` header or a URL netloc, lowercased, without port or dot."""
    host = value.strip().lower()
    if host.startswith("["):
        host = host[1 : host.find("]")] if "]" in host else host
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def redact(path: str) -> str:
    """Replace a signed segment (``/s/<sig>``, ``/u/<sig>``) before a path is logged (§ 11.1)."""
    return _SIGNED_SEGMENT.sub(lambda match: f"/{match.group(1)}/{REDACTED}", path)


class HostGuard:
    """421 for a ``Host`` outside the allowed set; 403 for a foreign ``Origin`` where it matters.

    ``Origin`` is checked on ``/mcp`` and on a ``POST`` under ``/s/`` (the passcode unlock).

    The ``Host`` check is what defeats DNS rebinding: a page on an attacker's name that resolves
    to vitrine still sends the attacker's name (spec § 7.4, security T8).
    """

    def __init__(self, app: ASGIApp, *, base_url: str, allowed_hosts: Iterable[str]) -> None:
        """Allow the host of ``public_base_url`` plus ``allowed_hosts``."""
        self._app = app
        self._origin = origin_of(base_url)
        self._extra = frozenset(hostname(host) for host in allowed_hosts)
        self._hosts = self._extra | {hostname(urlsplit(base_url).netloc)}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Refuse before routing, so no route can forget the check."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if hostname(headers.get("host", "")) not in self._hosts:
            await PlainTextResponse("misdirected request", status_code=421)(scope, receive, send)
            return
        origin = headers.get("origin")
        guarded = _is_mcp(scope["path"]) or _is_unlock(scope)
        if origin is not None and guarded and not self._trusts(origin):
            await PlainTextResponse("forbidden origin", status_code=403)(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _trusts(self, origin: str) -> bool:
        """Vitrine's own origin, or any origin on a host the operator listed."""
        return origin_of(origin) == self._origin or hostname(urlsplit(origin).netloc) in self._extra


class BodyLimit:
    """413 once a body passes its limit, counted while it streams (spec § 7.3, security T9).

    ``Content-Length`` is checked first, but it can lie or be absent (chunked), so every chunk is
    counted too. Past the limit, whatever the app was about to answer is replaced by the 413.
    """

    def __init__(self, app: ASGIApp, *, limits: Mapping[str, int], default: int) -> None:
        """``limits`` maps a path prefix to its byte limit; any other path gets ``default``."""
        self._app = app
        self._limits = sorted(limits.items(), key=lambda item: len(item[0]), reverse=True)
        self._default = default

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Count the body as the app reads it."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        limit = self._limit_for(scope["path"])
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isascii() and declared.isdigit() and int(declared) > limit:
            await _too_large(scope, receive, send)
            return
        received = 0
        exceeded = False
        started = False

        async def counted() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise _BodyTooLargeError
            return message

        async def guarded(message: Message) -> None:
            nonlocal started
            if exceeded:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self._app(scope, counted, guarded)
        except Exception:
            if not exceeded:
                raise
        if exceeded and not started:
            await _too_large(scope, receive, send)

    def _limit_for(self, path: str) -> int:
        """The limit of the longest prefix that matches ``path``."""
        return next(
            (limit for prefix, limit in self._limits if _under(path, prefix)), self._default
        )


class AccessLog:
    """One line per request, with signed segments redacted and no headers at all (spec § 11.1)."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the app."""
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Log the method, the redacted path, the status and the time taken."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def observed(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self._app(scope, receive, observed)
        finally:
            client = scope.get("client")
            access_logger.info(
                '%s "%s %s" %d %.1fms',
                client[0] if client else "-",
                scope["method"],
                redact(scope["path"]),
                status,
                (time.perf_counter() - started) * 1000,
            )


class ViewHeaders:
    """The § 7.2 headers on every response under ``/a/`` and ``/s/``, whoever built it.

    Views set them through ``responses``; this covers what Starlette answers on its own (a 405,
    a 400 or 416 for a bad ``Range``) and what a middleware refuses (421, 413). A header the
    response already carries is kept.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the app."""
        self._app = app
        defaults = security_headers() | {"Content-Security-Policy": PASSIVE_CSP}
        self._defaults = [
            (name.lower().encode(), value.encode()) for name, value in defaults.items()
        ]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Add each missing header to the response start."""
        if scope["type"] != "http" or not (
            _under(scope["path"], PUBLIC_PATH) or _under(scope["path"], SIGNED_PATH)
        ):
            await self._app(scope, receive, send)
            return

        async def completed(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers += [pair for pair in self._defaults if pair[0] not in present]
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, completed)


class _BodyTooLargeError(Exception):
    """Raised into the app from ``receive`` to stop it reading any further."""


async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
    """The 413 itself."""
    await PlainTextResponse("request body too large", status_code=413)(scope, receive, send)


def origin_of(url: str) -> str:
    """``scheme://host[:port]`` with the default port dropped, for comparing two origins."""
    parts = urlsplit(url.strip().lower())
    default = {"http": 80, "https": 443}.get(parts.scheme)
    try:
        port = parts.port
    except ValueError:
        return ""
    suffix = "" if port in (None, default) else f":{port}"
    return f"{parts.scheme}://{hostname(parts.netloc)}{suffix}"


def _is_mcp(path: str) -> bool:
    """Whether a request is for the MCP endpoint."""
    return _under(path, MCP_PATH)


def _is_unlock(scope: Scope) -> bool:
    """Whether a request posts under ``/s/``: a passcode, which a foreign page must not send."""
    return scope["method"] == "POST" and _under(scope["path"], SIGNED_PATH)


def _under(path: str, prefix: str) -> bool:
    """Whether ``path`` is ``prefix`` or below it."""
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")
