"""The JSX/TSX renderer: an HTML shell that mounts the component from vitrine's own runtime.

React, ReactDOM, the Sucrase transpiler, Tailwind, recharts and lucide-react ship in the wheel
under ``static/jsx/<bundle>/`` (built by ``just vendor-jsx``), so a component renders offline and
no viewer's browser trusts a third party unless the operator sets ``jsx_cdn`` (spec § 7.2).
The page is served under the same sandbox CSP as every renderer; the source is inlined, never
fetched, because a fetch from the page's opaque origin back to ``/s/`` would need CORS there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from html import escape
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath

STATIC_PREFIX = "/_vitrine"
BUNDLE_DIR = "jsx"
MANIFEST = "manifest.json"
SCRIPT_TYPES = {".js": "text/javascript", ".txt": "text/plain"}
# A JSON string inside <script> must not close it or open a comment, whatever the source holds.
_JSON_ESCAPES = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    "\u2028": "\\u2028",
    "\u2029": "\\u2029",
}
SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script type="importmap">{importmap}</script>
<style>
body {{ margin: 0; font: 16px/1.5 system-ui, sans-serif; }}
.vitrine-message {{ white-space: pre-wrap; margin: 2rem; padding: 1rem; border: 1px solid #b33;
  border-radius: 6px; color: #8a1c1c; background: #fff6f6;
  font: 14px/1.5 ui-monospace, monospace; }}
</style>
<script src="{tailwind}" integrity="{tailwind_sri}" crossorigin="anonymous"></script>
</head>
<body>
<div id="root"></div>
<script type="application/json" id="vitrine-source" data-kind="{kind}" data-packages="{packages}"
  data-cdn="{cdn}">{source}</script>
<script type="module" src="{mount}" integrity="{mount_sri}" crossorigin="anonymous"></script>
</body>
</html>
"""


@dataclass(frozen=True, slots=True)
class JsxBundle:
    """The vendored runtime as ``manifest.json`` records it: its id, imports and file hashes."""

    bundle: str
    imports: dict[str, str]
    integrity: dict[str, str]

    @classmethod
    @cache
    def load(cls) -> JsxBundle:
        """The bundle that ships in this package."""
        manifest = json.loads(_static().joinpath(MANIFEST).read_text(encoding="utf-8"))
        return cls(
            bundle=manifest["bundle"], imports=manifest["imports"], integrity=manifest["files"]
        )

    def url(self, name: str) -> str:
        """The immutable URL of one file of the bundle."""
        return f"{STATIC_PREFIX}/{BUNDLE_DIR}/{self.bundle}/{name}"

    def asset(self, path: str) -> tuple[bytes, str] | None:
        """The bytes and type of ``jsx/<bundle>/<file>``; any other path is ``None``.

        Only names the manifest lists are ever opened, so no request path reaches the disk.
        """
        folder, _, name = path.rpartition("/")
        if folder != f"{BUNDLE_DIR}/{self.bundle}" or name not in self.integrity:
            return None
        kind = SCRIPT_TYPES.get(PurePosixPath(name).suffix)
        if kind is None:
            return None
        return _static().joinpath(self.bundle, name).read_bytes(), kind

    def importmap(self) -> str:
        """The page's import map: every vendored specifier, each module pinned by its hash."""
        scripts = [name for name in self.integrity if name.endswith(".js")]
        return _json_in_script(
            {
                "imports": {spec: self.url(name) for spec, name in self.imports.items()},
                "integrity": {self.url(name): self.integrity[name] for name in scripts},
            }
        )


def render_jsx(data: bytes, filename: str, *, cdn: str = "") -> str:
    """The shell that transpiles and mounts the inlined component in the browser."""
    bundle = JsxBundle.load()
    name = PurePosixPath(filename).name
    return SHELL.format(
        title=escape(name),
        importmap=bundle.importmap(),
        tailwind=bundle.url("tailwind.js"),
        tailwind_sri=bundle.integrity["tailwind.js"],
        mount=bundle.url("mount.js"),
        mount_sri=bundle.integrity["mount.js"],
        kind="tsx" if name.lower().endswith(".tsx") else "jsx",
        packages=escape(json.dumps(sorted(bundle.imports))),
        cdn=escape(cdn),
        source=_json_in_script(data.decode("utf-8", errors="replace")),
    )


def _json_in_script(value: object) -> str:
    """JSON that is safe as the text of a ``<script>`` element."""
    text = json.dumps(value, ensure_ascii=False)
    return "".join(_JSON_ESCAPES.get(character, character) for character in text)


def _static() -> Traversable:
    """The packaged ``static/jsx`` directory, read through importlib so a zipped wheel works."""
    return files("mcp_vitrine.web").joinpath("static", BUNDLE_DIR)
