"""The dashboard's HTML (spec § 7.6), rendered on the server with no template engine.

The dashboard is vitrine's own page and is not sandboxed, so everything an agent wrote (titles,
tags, projects, notes, labels, file names, client names) passes through :func:`_e` on its way in
(security T25). Nothing here sets an inline style or script: the CSP allows neither, so the look
lives in ``static/admin/admin.css`` and the one script in ``static/admin/copy.js``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from html import escape
from urllib.parse import urlencode

from mcp_vitrine import __version__
from mcp_vitrine.domain.models import (
    ArtifactDetail,
    ArtifactPage,
    ArtifactSummary,
    LinkSummary,
    Visibility,
)
from mcp_vitrine.services.audit import AuditEvent, AuditEventName

ADMIN_PREFIX = "/_admin"
STATIC = f"{ADMIN_PREFIX}/static"
PROJECT_COLOURS = 8
HISTORY_SHOWN = 8
FILES_SHOWN = 12
EXPIRY_CHOICES = (("1d", "1 day"), ("7d", "7 days"), ("30d", "30 days"))
DEFAULT_EXPIRY = "7d"
KIB = 1024

NOTICES = {
    "revoked": "Link revoked. Anyone holding it now gets a 404.",
    "revoked-all": "Every private link was revoked.",
    "private": "It is private now. The public URL answers 404.",
    "saved": "Details saved.",
    "deleted": "Artifact deleted.",
}

_EVENTS = {
    AuditEventName.CREATED: ("Created", "version"),
    AuditEventName.VERSION_ADDED: ("Version written", "version"),
    AuditEventName.VISIBILITY_CHANGED: ("Visibility changed", "visibility"),
    AuditEventName.PUBLISH_REFUSED: ("Publish refused", "refused"),
    AuditEventName.LINK_ISSUED: ("Link created", "link"),
    AuditEventName.LINKS_REVOKED: ("Links revoked", "link"),
    AuditEventName.METADATA_CHANGED: ("Details changed", "meta"),
    AuditEventName.UPLOAD_URL_ISSUED: ("Upload URL issued", "other"),
    AuditEventName.DELETED: ("Deleted", "delete"),
    AuditEventName.EXPIRED: ("Expired", "other"),
    AuditEventName.ACCESS_DENIED: ("Access denied", "denied"),
    AuditEventName.MIGRATED: ("Migrated", "other"),
}

# Stroke icons, inline so they take the text colour; every one is decoration (aria-hidden).
_ICONS = {
    "window": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18"/>'
    '<path d="M8 16.5l3.5-4"/><path d="M11.5 17l2-2.3"/>',
    "lock": '<path d="M7 11V8a5 5 0 0 1 10 0v3"/>'
    '<rect x="5" y="11" width="14" height="10" rx="2"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    "out": '<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3"/><path d="M10 17l-5-5 5-5"/>'
    '<path d="M5 12h11"/>',
    "back": '<path d="M15 6l-6 6 6 6"/>',
    "open": '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/>'
    '<path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    "trash": '<path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="M6 7l1 13h10l1-13"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/>',
    "alert": '<circle cx="12" cy="12" r="9"/><path d="M12 8v5"/><path d="M12 16.5v.01"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "unlink": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/>'
    '<path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/><path d="M4 4l16 16"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "eye": '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/>'
    '<circle cx="12" cy="12" r="3"/>',
}


@dataclass(frozen=True, slots=True)
class Minted:
    """A link just made: shown on the page that answers the form, and never again."""

    label: str
    url: str
    passcode: str | None
    has_passcode: bool


def _e(value: object) -> str:
    """Escape for text and for a quoted attribute alike."""
    return escape(str(value), quote=True)


def _icon(name: str, size: int = 18) -> str:
    """One stroke icon."""
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
        f'aria-hidden="true">{_ICONS[name]}</svg>'
    )


def _when(moment: datetime | None) -> str:
    """A time as the dashboard shows every time: UTC, to the minute."""
    if moment is None:
        return "never"
    return moment.strftime("%d %b %Y, %H:%M UTC")


def _colour(project: str) -> str:
    """A project's colour class, the same on every page and every restart."""
    digest = hashlib.sha256(project.encode()).digest()
    return f"p{int.from_bytes(digest[:4]) % PROJECT_COLOURS}"


def _project(project: str | None) -> str:
    """A project name behind its colour dot."""
    if project is None:
        return '<span class="project muted"><span class="dot"></span>No project</span>'
    dot = f'<span class="dot {_colour(project)}"></span>'
    return f'<span class="project">{dot}{_e(project)}</span>'


def _badge(visibility: Visibility, until: datetime | None) -> str:
    """Public in terracotta behind the window, private in grey behind the lock."""
    if visibility is Visibility.PUBLIC:
        end = f"until {_when(until)}" if until else "no end date"
        return (
            f'<span class="badge public">{_icon("window", 14)}Public</span>'
            f'<span class="until">{_e(end)}</span>'
        )
    return f'<span class="badge private">{_icon("lock", 14)}Private</span>'


def _csrf(token: str) -> str:
    """The hidden field every dashboard form carries."""
    return f'<input type="hidden" name="csrf" value="{_e(token)}">'


def _title(title: str) -> str:
    """An artifact's title, or a placeholder for one that is blank."""
    return _e(title) if title.strip() else "(untitled)"


def page(title: str, body: str, *, section: str | None = None, csrf: str | None = None) -> str:
    """The document every dashboard page is: the bar (when signed in), then ``body``."""
    bar = ""
    if section is not None and csrf is not None:
        tabs = "".join(
            f'<a href="{href}"{' aria-current="page"' if name == section else ""}>{label}</a>'
            for name, href, label in (
                ("artifacts", f"{ADMIN_PREFIX}/", "Artifacts"),
                ("audit", f"{ADMIN_PREFIX}/audit", "Audit"),
            )
        )
        bar = (
            '<header class="bar"><div class="bar-in">'
            f'<a class="brand" href="{ADMIN_PREFIX}/">{_icon("window", 26)}vitrine</a>'
            f'<nav class="tabs" aria-label="Main">{tabs}</nav>'
            f'<form class="signout" method="post" action="{ADMIN_PREFIX}/logout">{_csrf(csrf)}'
            f'<button type="submit" aria-label="Sign out">{_icon("out", 20)}</button></form>'
            "</div></header>"
        )
        body = f'<main class="wrap">{body}</main>'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        f"<title>{_e(title)} · vitrine</title>"
        f'<link rel="stylesheet" href="{STATIC}/admin.css?v={_e(__version__)}">'
        f'<script src="{STATIC}/copy.js?v={_e(__version__)}" defer></script>'
        f"</head><body>{bar}{body}</body></html>"
    )


def login(*, message: str | None = None) -> str:
    """The sign-in form; ``message`` is one of the two fixed ones, never the input."""
    alert = (
        f'<div class="notice error" role="alert">{_icon("alert")}<span>{_e(message)}</span></div>'
        if message
        else ""
    )
    body = (
        '<main class="center"><div class="stack">'
        f'<div class="mark"><div class="mark-tile">{_icon("window", 36)}</div>'
        '<p class="name">vitrine</p><p class="what">Operator dashboard</p></div>'
        f'<form class="panel" method="post" action="{ADMIN_PREFIX}/login">'
        f"<h1>Sign in</h1>{alert}"
        '<label class="field">Operator password<input type="password" name="password" '
        'autocomplete="current-password" required maxlength="1024" autofocus></label>'
        '<button class="button primary" type="submit">Sign in</button></form>'
        '<p class="foot">Set the password with <code>mcp-vitrine admin password</code>. '
        "Client tokens can't sign in here.</p>"
        "</div></main>"
    )
    return page("Sign in", body)


@dataclass(frozen=True, slots=True)
class ListView:
    """What the artifact list shows: one page, the filters it was asked for and the totals."""

    page: ArtifactPage
    filters: Mapping[str, str]
    total: int
    public: int
    notice: str | None = None


def artifact_list(view: ListView, *, csrf: str) -> str:
    """The list: totals, the search, the facets, and one row per artifact."""
    filters = dict(view.filters)
    projects = view.page.facets.projects if view.page.facets else {}
    tags = view.page.facets.tags if view.page.facets else {}
    tiles = (
        '<section class="tiles" aria-label="Summary">'
        f'<div class="tile"><span class="tile-label">Artifacts</span>'
        f'<span class="tile-value">{view.total}</span>'
        f'<span class="tile-note">in {len(projects)} project{"" if len(projects) == 1 else "s"}'
        "</span></div>"
        f'<div class="tile public"><span class="tile-label">{_icon("window", 14)}Public</span>'
        f'<span class="tile-value">{view.public}</span>'
        '<span class="tile-note">in the window now</span></div>'
        "</section>"
    )
    hidden = "".join(
        f'<input type="hidden" name="{key}" value="{_e(filters[key])}">'
        for key in ("visibility", "project", "tag")
        if filters.get(key)
    )
    sorts = "".join(
        f'<option value="{value}"{" selected" if filters.get("sort", "updated") == value else ""}>'
        f"{label}</option>"
        for value, label in (
            ("updated", "Recently updated"),
            ("created", "Recently created"),
            ("title", "Title A-Z"),
        )
    )
    segments = "".join(
        f'<a href="{_href(filters, visibility=value)}"'
        f"{' aria-current="true"' if filters.get('visibility', '') == value else ''}>{label}</a>"
        for value, label in (("", "All"), ("private", "Private"), ("public", "Public"))
    )
    facets = "".join(
        f'<a class="facet" href="{_href(filters, project=name)}"'
        f"{' aria-current="true"' if filters.get('project') == name else ''}>"
        f'<span class="dot {_colour(name)}"></span>{_e(name)}<span class="n">{count}</span></a>'
        for name, count in sorted(projects.items(), key=lambda item: (-item[1], item[0]))
    )
    tag_links = "".join(
        f'<a class="facet" href="{_href(filters, tag=name)}"'
        f"{' aria-current="true"' if filters.get('tag') == name else ''}>"
        f'{_e(name)}<span class="n">{count}</span></a>'
        for name, count in sorted(tags.items(), key=lambda item: (-item[1], item[0]))[:12]
    )
    search = (
        f'<form class="search" role="search" method="get" action="{ADMIN_PREFIX}/">'
        '<div class="search-row"><label class="q"><span class="sr">Search</span>'
        f'{_icon("search")}<input type="search" name="q" value="{_e(filters.get("q", ""))}" '
        'placeholder="Search titles and tags" maxlength="200"></label>'
        f'<label class="sort"><span class="sr">Sort</span><select name="sort">{sorts}</select>'
        f'</label>{hidden}<button class="button primary" type="submit">Search</button></div>'
        f'<div class="filters"><div class="segments" role="group" aria-label="Visibility">'
        f"{segments}</div>"
        + (f'<span class="divider" aria-hidden="true"></span>{facets}' if facets else "")
        + (f'<span class="divider" aria-hidden="true"></span>{tag_links}' if tag_links else "")
        + "</div></form>"
    )
    if view.page.items:
        rows = "".join(_row(item) for item in view.page.items)
        listing = f'<ul class="rows">{rows}</ul>'
        older = (
            f'<a class="button small" href="{_href(filters, cursor=view.page.next_cursor)}">'
            "Next page</a>"
            if view.page.next_cursor
            else ""
        )
        first = (
            f'<a class="button small" href="{_href(filters, cursor="")}">First page</a>'
            if filters.get("cursor")
            else ""
        )
        listing += f'<nav class="pages" aria-label="Pages"><span>{first}</span>{older}</nav>'
    else:
        listing = (
            f'<div class="empty">{_icon("window", 44)}<h2>The window is empty</h2>'
            "<p>No artifacts match. Agents publish through MCP or the API; nothing is created "
            f'here.</p><a href="{ADMIN_PREFIX}/">Clear the filters</a></div>'
        )
    lead = (
        '<div class="lead"><h1>Artifacts</h1><p>Everything your agents have published. You can '
        "narrow access here; nothing becomes public from this page.</p></div>"
    )
    return page(
        "Artifacts",
        _notice(view.notice) + lead + tiles + search + listing,
        section="artifacts",
        csrf=csrf,
    )


def _row(item: ArtifactSummary) -> str:
    """One artifact in the list."""
    tags = "".join(f'<span class="tag">{_e(tag)}</span>' for tag in item.tags[:6])
    return (
        '<li><div class="main">'
        f'<a class="title" href="{ADMIN_PREFIX}/a/{_e(item.id)}">{_title(item.title)}</a>'
        f'<div class="sub"><span class="mono">{_e(item.id)}</span>{tags}</div></div>'
        '<div class="meta">'
        f'<span class="w-project">{_project(item.project)}</span>'
        f'<span class="w-badge">{_badge(item.visibility, item.public_until)}</span>'
        f'<span class="w-version" title="Current version">v{item.version}</span>'
        f'<span class="w-date">{_e(_when(item.updated_at))}</span>'
        "</div></li>"
    )


def _href(filters: Mapping[str, str], **changes: str | None) -> str:
    """The list's URL with ``changes`` applied; a filter change starts at the first page."""
    merged = {**filters, **changes}
    if "cursor" not in changes:
        merged.pop("cursor", None)
    query = urlencode({key: value for key, value in merged.items() if value})
    return _e(f"{ADMIN_PREFIX}/" + (f"?{query}" if query else ""))


def _notice(key: str | None) -> str:
    """The line an action leaves behind; only the fixed ones in ``NOTICES`` are shown."""
    text = NOTICES.get(key or "")
    if text is None:
        return ""
    return f'<div class="notice ok" role="status">{_icon("check")}<span>{_e(text)}</span></div>'


@dataclass(frozen=True, slots=True)
class DetailView:
    """What one artifact's page shows."""

    detail: ArtifactDetail
    history: Sequence[AuditEvent]
    public_url: str | None
    notice: str | None = None
    minted: Minted | None = None
    error: str | None = None


def artifact(view: DetailView, *, csrf: str) -> str:
    """One artifact: what it is, who can open it, its links, versions, details and history."""
    detail = view.detail
    meta = detail.artifact
    base = f"{ADMIN_PREFIX}/a/{_e(meta.id)}"
    tags = "".join(f'<span class="chip">{_e(tag)}</span>' for tag in meta.tags)
    size = sum(file.bytes for file in detail.files)
    views = "off" if detail.views is None else str(detail.views.total)
    where = (
        f'<span class="mono">{_e(view.public_url)}</span>'
        if view.public_url
        else "Private: only its links open it"
    )
    facts = "".join(
        f"<div><dt>{label}</dt><dd>{value}</dd></div>"
        for label, value in (
            ("Open to", where),
            ("Views", _e(views)),
            ("Current version", f"v{detail.version} · {_e(_size(size))}"),
            ("Created by", f'<span class="mono">{_e(meta.source)}</span>'),
            ("Expires", _e(_when(meta.expires_at)) if meta.expires_at else "Never"),
        )
    )
    public = meta.visibility is Visibility.PUBLIC
    actions = (
        f'<form method="post" action="{base}/open" target="_blank" rel="noopener">{_csrf(csrf)}'
        f'<button class="button" type="submit">{_icon("open", 17)}Open</button></form>'
        + (
            f'<a class="button primary" href="{base}/confirm/private">{_icon("lock", 17)}'
            "Make private</a>"
            if public
            else ""
        )
        + f'<a class="button danger" href="{base}/confirm/delete">{_icon("trash", 17)}Delete</a>'
    )
    hero = (
        '<section class="card hero">'
        f'<div class="chips">{_project(meta.project)}{tags}</div>'
        f"<h1>{_title(meta.title)}</h1>"
        f'<div class="chips">{_badge(meta.visibility, meta.public_until)}'
        f'<span class="mono">{_e(meta.id)}</span></div>'
        f'<dl class="facts">{facts}</dl><div class="actions">{actions}</div></section>'
    )
    error = (
        f'<div class="notice error" role="alert">{_icon("alert")}'
        f"<span>{_e(view.error)}</span></div>"
        if view.error
        else ""
    )
    body = (
        f'<a class="back" href="{ADMIN_PREFIX}/">{_icon("back")}All artifacts</a>'
        + error
        + _notice(view.notice)
        + _minted(view.minted)
        + hero
        + '<div class="columns"><div class="wide">'
        + _links(detail, csrf=csrf, base=base, public=public)
        + _versions(detail)
        + '</div><aside class="side">'
        + _details(detail, csrf=csrf, base=base)
        + _history(view.history, artifact_id=meta.id)
        + "</aside></div>"
    )
    return page(meta.title or "Artifact", body, section="artifacts", csrf=csrf)


def _minted(minted: Minted | None) -> str:
    """The new link, once, with a Copy button that the script reveals."""
    if minted is None:
        return ""
    secret = (
        f'<p class="hint">Its passcode, also shown only now:</p>'
        f'<p class="secret">{_e(minted.passcode)}</p>'
        if minted.passcode
        else ""
    )
    hint = (
        "Send the passcode apart from the link."
        if minted.has_passcode
        else "Anyone holding it can open the artifact until it expires."
    )
    label = f"Link “{_e(minted.label)}” created" if minted.label else "Link created"
    return (
        '<div class="minted" role="status">'
        f'<p class="lede">{label}</p>'
        f'<p class="hint">Copy it now: it is shown only this once. {_e(hint)}</p>'
        '<div class="copy"><label class="sr" for="minted-url">New link</label>'
        f'<input id="minted-url" class="mono" type="text" readonly value="{_e(minted.url)}">'
        '<button class="button small" type="button" data-copy="minted-url" hidden>Copy</button>'
        f"</div>{secret}</div>"
    )


def _links(detail: ArtifactDetail, *, csrf: str, base: str, public: bool) -> str:
    """The live private links, each revocable, and the form for a new one."""
    if public:
        return (
            '<section class="card"><div class="card-head"><h2>Private links</h2></div>'
            '<p class="note">While it is public, its public URL is the link. Make it private to '
            "share it through private links again.</p></section>"
        )
    counts = {} if detail.views is None else {link.lid: link.views for link in detail.views.links}
    rows = "".join(_link(link, counts.get(link.lid), csrf=csrf, base=base) for link in detail.links)
    listing = (
        f'<ul class="rows">{rows}</ul>'
        if rows
        else '<p class="note">No live links. Make one below.</p>'
    )
    revoke_all = (
        f'<a class="link-button" href="{base}/confirm/revoke">Revoke all</a>' if rows else ""
    )
    expiries = "".join(
        f'<option value="{value}"{" selected" if value == DEFAULT_EXPIRY else ""}>{label}</option>'
        for value, label in EXPIRY_CHOICES
    )
    form = (
        f'<form class="new-link" method="post" action="{base}/links">{_csrf(csrf)}'
        f"<h3>{_icon('plus', 16)}New private link</h3>"
        '<div class="row"><label class="field grow">Label<input type="text" name="label" '
        'maxlength="200" placeholder="e.g. legal-check"></label>'
        f'<label class="field some">Expires in<select name="expires_in">{expiries}</select>'
        "</label>"
        '<label class="field grow">Passcode (optional, 8 to 128)<input type="password" '
        'name="passcode" autocomplete="new-password" minlength="8" maxlength="128"></label></div>'
        '<label class="check"><input type="checkbox" name="generate" value="1">'
        "Generate a passcode instead</label>"
        '<div class="row"><button class="button primary" type="submit">Create link</button></div>'
        "</form>"
    )
    return (
        '<section class="card"><div class="card-head">'
        f'<h2>Private links <span class="count">{len(detail.links)} live</span></h2>'
        f"{revoke_all}</div>{listing}{form}</section>"
    )


def _link(link: LinkSummary, views: int | None, *, csrf: str, base: str) -> str:
    """One live link: its label, passcode, expiry and views, and a revoke button."""
    passcode = f'<span class="chip">{_icon("lock", 13)}Passcode</span>' if link.passcode else ""
    seen = "" if views is None else f'<span class="chip">{_icon("eye", 13)}{views} views</span>'
    label = _e(link.label) if link.label else '<span class="muted">No label</span>'
    return (
        f'<li><div class="main"><span><strong>{label}</strong> '
        f'<span class="mono muted">{_e(link.lid)}</span></span></div>'
        f'<div class="meta">{passcode}'
        f'<span class="chip">{_icon("clock", 13)}Expires {_e(_when(link.expires_at))}</span>'
        f"{seen}</div>"
        f'<form class="inline end" method="post" action="{base}/revoke">{_csrf(csrf)}'
        f'<input type="hidden" name="link" value="{_e(link.lid)}">'
        '<button class="button small danger" type="submit">Revoke</button></form></li>'
    )


def _versions(detail: ArtifactDetail) -> str:
    """The retained versions, newest first, and the current version's files."""
    meta = detail.artifact
    rows = "".join(
        f'<li><span class="w-version"><strong>v{version.n}</strong></span>'
        + ('<span class="badge public">Current</span>' if version.n == meta.version else "")
        + f'<span class="main muted">{_e(_when(version.at))}'
        + (f" · {_e(version.note)}" if version.note else "")
        + f'</span><span class="mono muted">{_count(version.files, "file")} · '
        f"{_e(_size(version.bytes))}</span></li>"
        for version in sorted(meta.versions, key=lambda version: -version.n)
    )
    files = "".join(
        f'<span class="chip mono">{_e(file.path)}</span>' for file in detail.files[:FILES_SHOWN]
    )
    more = len(detail.files) - FILES_SHOWN
    if more > 0:
        files += f'<span class="chip">and {more} more</span>'
    return (
        f'<section class="card"><h2>Versions</h2><ul class="rows">{rows}</ul>'
        f'<div class="pad"><div class="chips">{files}</div></div></section>'
    )


def _details(detail: ArtifactDetail, *, csrf: str, base: str) -> str:
    """The title, project and tags, editable; nothing else is."""
    meta = detail.artifact
    return (
        f'<form class="card pad" method="post" action="{base}/meta">{_csrf(csrf)}'
        "<h2>Details</h2>"
        '<label class="field">Title<input type="text" name="title" maxlength="200" required '
        f'value="{_e(meta.title)}"></label>'
        '<label class="field">Project<input type="text" name="project" maxlength="64" '
        f'value="{_e(meta.project or "")}" placeholder="none"></label>'
        '<label class="field">Tags, comma-separated<input type="text" name="tags" '
        f'value="{_e(", ".join(meta.tags))}"></label>'
        '<button class="button" type="submit">Save details</button></form>'
    )


def _history(events: Sequence[AuditEvent], *, artifact_id: str) -> str:
    """The newest audit events of one artifact, as a timeline."""
    items = "".join(
        '<li><div class="rail" aria-hidden="true">'
        f'<span class="dot e-{_EVENTS[event.event][1]}"></span><span></span></div>'
        f'<div class="what"><strong>{_e(_describe(event))}</strong>'
        f"<span>{_e(_when(event.ts))} · {_e(event.client)}</span></div></li>"
        for event in events[:HISTORY_SHOWN]
    )
    query = urlencode({"artifact": artifact_id})
    return (
        '<section class="card pad"><h2>History</h2>'
        f'<ol class="timeline">{items}</ol>'
        f'<a href="{ADMIN_PREFIX}/audit?{_e(query)}">Full audit log</a></section>'
    )


def _describe(event: AuditEvent) -> str:
    """An event in words, from its ids and field names only, as the log holds them."""
    label = _EVENTS[event.event][0]
    match event.event:
        case AuditEventName.VISIBILITY_CHANGED:
            return f"Made {event.to_visibility} ({event.gate})"
        case AuditEventName.LINK_ISSUED:
            return f"Link {event.lid} created" + (", with a passcode" if event.passcode else "")
        case AuditEventName.LINKS_REVOKED:
            return f"Link {event.lid} revoked" if event.lid else "Every private link revoked"
        case AuditEventName.CREATED | AuditEventName.VERSION_ADDED:
            return f"{label}: v{event.version}"
        case AuditEventName.METADATA_CHANGED:
            return f"{label}: {', '.join(event.fields or ())}"
        case _:
            return label


def _detail_line(event: AuditEvent) -> str:
    """The extras of an event, as ``key value`` pairs."""
    extras = event.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
        exclude={"ts", "event", "artifact", "client"},
    )
    return " · ".join(
        f"{key} {', '.join(value) if isinstance(value, list) else value}"
        for key, value in extras.items()
    )


def confirm(action: str, detail: ArtifactDetail, *, csrf: str) -> str:
    """The step before a change that cannot be taken back."""
    meta = detail.artifact
    base = f"{ADMIN_PREFIX}/a/{_e(meta.id)}"
    title = _title(meta.title)
    links = len(detail.links)
    texts = {
        "private": (
            f"Take “{title}” out of the window?",
            [
                "The public URL stops working right away.",
                "It stays private until an agent asks again and a person approves.",
            ],
            "lock",
            "Make private",
            "primary",
        ),
        "delete": (
            f"Delete “{title}”?",
            [
                f"All {len(meta.versions)} retained versions and {links} live links are removed. "
                "Every link to it answers 404.",
                "The audit log keeps a record that it was deleted, and by whom.",
                "This can't be undone.",
            ],
            "trash",
            "Delete artifact",
            "danger solid",
        ),
        "revoke": (
            "Revoke every private link?",
            [
                f"{links} live link{'' if links == 1 else 's'} stop working right away.",
                "New links can be made afterwards.",
            ],
            "unlink",
            f"Revoke {links} link{'' if links == 1 else 's'}",
            "danger solid",
        ),
    }
    heading, points, icon, verb, style = texts[action]
    tile = "icon-tile" if action == "private" else "icon-tile danger"
    items = "".join(f"<li>{point}</li>" for point in points)
    body = (
        f'<form class="panel confirm" method="post" action="{base}/{action}">{_csrf(csrf)}'
        f'<div class="{tile}">{_icon(icon, 26)}</div><h1>{heading}</h1><ul>{items}</ul>'
        f'<div class="actions"><button class="button {style}" type="submit">{verb}</button>'
        f'<a class="button" href="{base}">Cancel</a></div></form>'
    )
    return page("Confirm", body, section="artifacts", csrf=csrf)


@dataclass(frozen=True, slots=True)
class AuditView:
    """One page of the audit log and the filters that chose it."""

    events: Sequence[AuditEvent]
    filters: Mapping[str, str]
    page: int
    more: bool


def audit(view: AuditView, *, csrf: str) -> str:
    """The audit log, newest first, grouped by day."""
    filters = dict(view.filters)
    events = "".join(
        f'<option value="{name.value}"{" selected" if filters.get("event") == name.value else ""}>'
        f"{label}</option>"
        for name, (label, _) in _EVENTS.items()
    )
    form = (
        f'<form class="search" role="search" method="get" action="{ADMIN_PREFIX}/audit">'
        '<div class="row">'
        '<label class="field some">Client<input type="text" name="client" maxlength="64" '
        f'value="{_e(filters.get("client", ""))}" placeholder="anyone"></label>'
        f'<label class="field some">Event<select name="event"><option value="">Any event</option>'
        f"{events}</select></label>"
        '<label class="field grow">Artifact id<input class="mono" type="text" name="artifact" '
        f'maxlength="32" value="{_e(filters.get("artifact", ""))}"></label>'
        '<button class="button primary" type="submit">Filter</button></div></form>'
    )
    days: dict[str, list[AuditEvent]] = {}
    for event in view.events:
        days.setdefault(event.ts.strftime("%A, %d %B %Y"), []).append(event)
    sections = "".join(
        f'<section class="day"><h2>{_e(day)}</h2><ol class="rows">'
        + "".join(_audit_row(event) for event in group)
        + "</ol></section>"
        for day, group in days.items()
    )
    if not sections:
        sections = '<div class="empty"><h2>No events match</h2></div>'
    older = (
        f'<a class="button small" href="{_audit_href(filters, view.page + 1)}">Older entries</a>'
        if view.more
        else ""
    )
    newer = (
        f'<a class="button small" href="{_audit_href(filters, view.page - 1)}">Newer entries</a>'
        if view.page > 1
        else ""
    )
    lead = (
        '<div class="lead"><h1>Audit log</h1><p>Every change, newest first, in UTC. Entries hold '
        "ids and field names, never artifact content.</p></div>"
    )
    pages = f'<nav class="pages" aria-label="Pages"><span>{newer}</span>{older}</nav>'
    return page("Audit log", lead + form + sections + pages, section="audit", csrf=csrf)


def _audit_row(event: AuditEvent) -> str:
    """One event."""
    label, kind = _EVENTS[event.event]
    client = "client admin" if event.client.startswith("@admin") else "client"
    target = (
        f'<a class="w-artifact" href="{ADMIN_PREFIX}/a/{_e(event.artifact)}">{_e(event.artifact)}'
        "</a>"
        if event.artifact
        else '<span class="w-artifact muted">—</span>'
    )
    return (
        f'<li><span class="w-time">{event.ts.strftime("%H:%M:%S")}</span>'
        f'<span class="w-event"><span class="dot e-{kind}"></span>{_e(label)}</span>'
        f'<span class="w-client"><span class="{client}">{_e(event.client)}</span></span>'
        f'{target}<span class="w-detail">{_e(_detail_line(event))}</span></li>'
    )


def _audit_href(filters: Mapping[str, str], number: int) -> str:
    """The audit log's URL at page ``number``."""
    query = urlencode({**{k: v for k, v in filters.items() if v}, "page": str(number)})
    return _e(f"{ADMIN_PREFIX}/audit?{query}")


def error(status: int, message: str, *, csrf: str | None) -> str:
    """A refusal, worded by the one mapping in ``responses``, inside the dashboard."""
    body = (
        f'<a class="back" href="{ADMIN_PREFIX}/">{_icon("back")}All artifacts</a>'
        f'<div class="notice error" role="alert">{_icon("alert")}'
        f"<span>{status}: {_e(message)}</span></div>"
    )
    if csrf is None:
        return page("Error", f'<main class="wrap">{body}</main>')
    return page("Error", body, section="artifacts", csrf=csrf)


def _count(number: int, noun: str) -> str:
    """``1 file``, ``2 files``."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _size(count: int) -> str:
    """A byte count as people read it."""
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < KIB or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= KIB
    return f"{count} B"
