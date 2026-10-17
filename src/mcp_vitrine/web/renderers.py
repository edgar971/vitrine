"""The renderers of spec § 7.2: Markdown, CSV/TSV and JSON, each turned into escaped HTML.

A renderer is ``(bytes, filename) -> html`` (design § 7). Its output is served under the same
sandbox CSP as agent-written HTML, so escaping here is a second wall, not the only one.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
from collections.abc import Callable, Iterable
from html import escape
from pathlib import PurePosixPath

from markdown_it import MarkdownIt
from mdit_py_plugins.anchors import anchors_plugin
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.front_matter import front_matter_plugin
from mdit_py_plugins.tasklists import tasklists_plugin

# Rendering is for reading, not for bulk data: a larger file is served as itself.
RENDER_LIMIT = 2 * 1024 * 1024

Renderer = Callable[[bytes, str], str]

_STYLE = (
    "body{font:16px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:60rem;padding:0 1rem}"
    "pre{overflow:auto;padding:1rem;background:#f4f4f4}"
    "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.25rem .5rem}"
    "@media (prefers-color-scheme:dark){body{background:#111;color:#eee}pre{background:#222}}"
)

# What agents write is GitHub-flavoured, so the GFM extensions are on. Raw HTML stays off: an
# agent's <script> in Markdown is shown, never run (spec § 7.2).
_MARKDOWN = (
    MarkdownIt("commonmark", {"html": False})
    .enable(["table", "strikethrough"])
    .use(tasklists_plugin)
    .use(footnote_plugin)
    .use(front_matter_plugin)
    .use(anchors_plugin, max_level=6)
)
FRONT_MATTER = "front_matter"


def render_markdown(data: bytes, filename: str) -> str:
    """GFM-style Markdown with raw HTML disabled; a front-matter ``title`` names the page."""
    env: dict[str, object] = {}
    tokens = _MARKDOWN.parse(_text(data), env)
    matter = next((token.content for token in tokens if token.type == FRONT_MATTER), "")
    body = _MARKDOWN.renderer.render(tokens, _MARKDOWN.options, env)
    return _page(filename, body, title=_front_matter(matter).get("title"))


def render_csv(data: bytes, filename: str) -> str:
    """A comma-separated file as a table."""
    return _page(filename, _delimited(_text(data), "excel"))


def render_tsv(data: bytes, filename: str) -> str:
    """A tab-separated file as a table."""
    return _page(filename, _delimited(_text(data), "excel-tab"))


def render_json(data: bytes, filename: str) -> str:
    """JSON indented and escaped; anything that does not parse is shown as it is."""
    text = _text(data)
    with contextlib.suppress(ValueError, RecursionError):
        text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    return _page(filename, f"<pre>{escape(text)}</pre>")


RENDERERS: dict[str, Renderer] = {
    "md": render_markdown,
    "csv": render_csv,
    "tsv": render_tsv,
    "json": render_json,
}


def renderer_for(path: str, enabled: Iterable[str]) -> Renderer | None:
    """The renderer for ``path``'s extension when the operator enabled it, else ``None``."""
    extension = PurePosixPath(path).suffix.removeprefix(".").lower()
    return RENDERERS.get(extension) if extension in set(enabled) else None


def _text(data: bytes) -> str:
    """Decode as UTF-8, showing undecodable bytes rather than refusing the file."""
    return data.decode("utf-8", errors="replace")


def _delimited(text: str, dialect: str) -> str:
    """Rows as an HTML table, every cell escaped and the first row the header.

    A file the csv module refuses (a field past its size limit) is shown as escaped text.
    """
    try:
        rows = list(csv.reader(io.StringIO(text), dialect=dialect))
    except csv.Error:
        return f"<pre>{escape(text)}</pre>"
    lines = []
    for index, row in enumerate(rows):
        cell = "th" if index == 0 else "td"
        cells = "".join(f"<{cell}>{escape(value)}</{cell}>" for value in row)
        lines.append(f"<tr>{cells}</tr>")
    return "<table>" + "".join(lines) + "</table>"


def _front_matter(text: str) -> dict[str, str]:
    """Simple ``key: value`` lines, quotes stripped; anything richer is ignored, not parsed.

    Not YAML: the one field used is the title, and a YAML parser is more surface than it is worth.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, colon, value = line.partition(":")
        if colon and key.strip() and not key.startswith((" ", "\t", "-", "#")):
            fields[key.strip().lower()] = _unquoted(value.strip())
    return fields


def _unquoted(value: str) -> str:
    """A value without the one pair of matching quotes around it, if it has them."""
    quoted = len(value) > 1 and value[0] == value[-1] and value[0] in "\"'"
    return value[1:-1] if quoted else value


def _page(filename: str, body: str, *, title: str | None = None) -> str:
    """A complete, self-contained document around rendered content."""
    name = title or PurePosixPath(filename).name
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(name)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )
