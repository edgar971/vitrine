"""The response policy of spec § 7.2 and the one place domain errors become HTTP (spec § 10).

Every artifact byte leaves through :func:`artifact_file` or :func:`rendered`, so the security
headers cannot be forgotten by a route, and every error leaves through :func:`problem`, so a
route cannot word one that says whether a private artifact exists.
"""

from __future__ import annotations

from enum import StrEnum
from html import escape
from http import HTTPStatus
from pathlib import Path, PurePosixPath

from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)

from mcp_vitrine.domain.errors import (
    Conflict,
    Expired,
    Invalid,
    Locked,
    NotFound,
    OverQuota,
    RateLimited,
    Refused,
    TooLarge,
    UnsupportedType,
    VitrineError,
)
from mcp_vitrine.domain.media import OCTET_STREAM, TEXT_TYPES, content_type

# Never `allow-same-origin`: the content gets an opaque origin, so it cannot read vitrine's cookies
# or storage, or act as the viewer against vitrine (spec § 7.2, security T7).
SANDBOX_CSP = (
    "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads; "
    "frame-ancestors 'none'"
)
# The types a browser runs script in. XML is here because an XHTML namespace makes it a page.
ACTIVE_TYPES = frozenset({"text/html", "image/svg+xml", "application/xml"})
TEXT_CHARSET = "; charset=utf-8"
NO_STORE = "private, no-store"
PUBLIC_CACHE = "public, max-age=60"
INTERNAL = "internal error"
# The unlock form is vitrine's own page: nothing loads, nothing frames it, it posts only home.
UNLOCK_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
)
WRONG_PASSCODE = "That passcode did not work."
UNLOCK_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Passcode required</title>
<style>
body {{ font: 16px/1.5 system-ui, sans-serif; margin: 0; display: grid; place-items: center;
  min-height: 100vh; background: #f6f6f4; color: #1d1d1b; }}
form {{ display: grid; gap: 12px; padding: 24px; max-width: 320px; width: calc(100% - 32px); }}
input {{ font: inherit; padding: 8px 10px; border: 1px solid #999; border-radius: 6px; }}
button {{ font: inherit; padding: 8px 10px; border: 0; border-radius: 6px; background: #1d1d1b;
  color: #fff; }}
p {{ margin: 0; }}
.error {{ color: #a1260d; }}
@media (prefers-color-scheme: dark) {{
  body {{ background: #1b1b1a; color: #ececea; }}
  input {{ background: #2a2a28; color: inherit; border-color: #666; }}
  button {{ background: #ececea; color: #1b1b1a; }}
  .error {{ color: #f08a70; }}
}}
</style>
</head>
<body>
<form method="post" action="{action}">
<p>This link needs a passcode.</p>
{message}<input type="password" name="passcode" aria-label="Passcode" autocomplete="off"
  autofocus required maxlength="128">
<input type="hidden" name="path" value="{path}">
<button type="submit">Open</button>
</form>
</body>
</html>
"""

_STATUS: tuple[tuple[type[VitrineError], int], ...] = (
    (NotFound, 404),
    (Expired, 410),
    (Locked, 401),
    (UnsupportedType, 415),
    (Invalid, 400),
    (Conflict, 409),
    (TooLarge, 413),
    (RateLimited, 429),
    (OverQuota, 507),
    (Refused, 403),
)


class Prefix(StrEnum):
    """Which view a response belongs to; it decides how long a cache may keep it (spec § 7.2)."""

    PUBLIC = "a"
    SIGNED = "s"

    @property
    def cache_control(self) -> str:
        """A public view may be cached briefly; a signed one never, because its URL is a key."""
        return PUBLIC_CACHE if self is Prefix.PUBLIC else NO_STORE


def security_headers(cache_control: str = NO_STORE) -> dict[str, str]:
    """The headers every artifact response and every view error carries (spec § 7.2)."""
    return {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Robots-Tag": "noindex, nofollow",
        "Cache-Control": cache_control,
    }


def artifact_file(
    file: Path, path: str, *, prefix: Prefix, download: bool, status: int = HTTPStatus.OK
) -> Response:
    """Stream one stored file with the allowlisted type; ``Range`` and ``HEAD`` come with it."""
    kind = content_type(path)
    headers = security_headers(prefix.cache_control)
    if kind in ACTIVE_TYPES:
        headers["Content-Security-Policy"] = SANDBOX_CSP
    attachment = download or kind == OCTET_STREAM
    return FileResponse(
        file,
        status_code=status,
        media_type=kind + TEXT_CHARSET if _is_text(kind) else kind,
        headers=headers,
        filename=PurePosixPath(path).name if attachment else None,
        content_disposition_type="attachment",
    )


def rendered(html: str, *, prefix: Prefix, status: int = HTTPStatus.OK) -> Response:
    """Renderer output: HTML, so it carries the sandbox like agent-written HTML (spec § 7.2)."""
    headers = security_headers(prefix.cache_control)
    headers["Content-Security-Policy"] = SANDBOX_CSP
    return HTMLResponse(html, status_code=status, headers=headers)


def status_of(error: VitrineError) -> int:
    """The one status each domain error maps to (spec § 10)."""
    return next((status for kind, status in _STATUS if isinstance(error, kind)), 500)


def problem(error: VitrineError, *, view: bool = False) -> Response:
    """An error response; views answer in text, the API and uploads in JSON.

    On a view a malformed path is just a missing file, so it is a 404 like any other. A 5xx
    hides its message, which can carry filesystem details.
    """
    status = HTTPStatus.NOT_FOUND if view and isinstance(error, Invalid) else status_of(error)
    if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        message = INTERNAL
    elif status == HTTPStatus.NOT_FOUND:
        message = str(NotFound())
    else:
        message = str(error)
    headers = security_headers()
    if isinstance(error, RateLimited):
        headers["Retry-After"] = str(error.retry_after)
    if view:
        return PlainTextResponse(message, status_code=status, headers=headers)
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def unlock_form(ref: str, path: str, *, wrong: bool = False) -> Response:
    """401 and vitrine's own passcode form: no title, no file list, nothing from the artifact.

    ``path`` is the one the viewer asked for, so a right passcode lands them on it.
    """
    headers = security_headers()
    headers["Content-Security-Policy"] = UNLOCK_CSP
    # Under no-referrer a browser posts the form with ``Origin: null``, which the guard refuses;
    # same-origin sends vitrine's own origin to vitrine and nothing to anyone else.
    headers["Referrer-Policy"] = "same-origin"
    message = f'<p class="error" role="alert">{WRONG_PASSCODE}</p>\n' if wrong else ""
    page = UNLOCK_PAGE.format(action=escape(f"/s/{ref}/unlock"), path=escape(path), message=message)
    return HTMLResponse(page, status_code=401, headers=headers)


def unauthorized() -> Response:
    """401 for a missing or unknown bearer token (spec § 10)."""
    headers = security_headers()
    headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse({"error": "unauthorized"}, status_code=401, headers=headers)


def forbidden() -> Response:
    """403 for a valid token without the route's scope."""
    return JSONResponse({"error": "forbidden"}, status_code=403, headers=security_headers())


def _is_text(kind: str) -> bool:
    """Whether a type is one of the text types, which are always served as UTF-8."""
    return kind in TEXT_TYPES.values()
