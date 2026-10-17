"""Build the JSX renderer's browser runtime from the pinned npm lockfile into the package.

``just vendor-jsx`` runs this. It needs Node.js and npm; nothing else in the repository does,
because the output is committed: the wheel ships it, and ``tests/unit/web/jsx_spec.py`` checks
every file against the hashes recorded here, so CI needs neither.

1. ``npm ci`` the lockfile in ``scripts/jsx/`` into a scratch directory (no install scripts).
2. ``node build.mjs`` bundles React, ReactDOM, Sucrase, recharts and lucide-react as ES modules,
   and copies Tailwind's browser build.
3. The files land in ``web/static/jsx/<bundle>/`` with ``mount.js``, every bundled package's
   licence goes into ``LICENSES.txt``, and ``manifest.json`` records each file's SRI hash. The
   bundle id is a digest of those hashes, so a changed file is a new, immutable URL.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "jsx"
TARGET = ROOT / "src" / "mcp_vitrine" / "web" / "static" / "jsx"
MANIFEST = "manifest.json"
LICENSES = "LICENSES.txt"
# The bare specifiers a component may import, and the file each resolves to.
IMPORTS = {
    "react": "react.js",
    "react-dom": "react-dom.js",
    "react-dom/client": "react-dom-client.js",
    "react/jsx-runtime": "jsx-runtime.js",
    "recharts": "recharts.js",
    "lucide-react": "lucide-react.js",
}
LICENCE_NAMES = (
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
    "LICENCE",
    "LICENCE.md",
    "license",
    "license.md",
)


def integrity(data: bytes) -> str:
    """The Subresource Integrity value of ``data``."""
    return "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()


def bundle_id(files: dict[str, str]) -> str:
    """A short digest of every file's hash: the path segment that makes the URLs immutable."""
    listing = "".join(f"{name}:{value}\n" for name, value in sorted(files.items()))
    return hashlib.sha256(listing.encode()).hexdigest()[:12]


def package_of(path: str) -> str | None:
    """The npm package an esbuild input path belongs to, by its innermost ``node_modules``."""
    parts = path.split("/")
    if "node_modules" not in parts:
        return None
    rest = parts[len(parts) - parts[::-1].index("node_modules") :]
    return "/".join(rest[:2]) if rest[0].startswith("@") else rest[0]


def licences(scratch: Path, inputs: list[str]) -> tuple[dict[str, dict[str, str]], str]:
    """Each bundled package's version and licence, and the licence texts to ship beside them."""
    packages: dict[str, dict[str, str]] = {}
    texts: list[str] = []
    for name in sorted({package for path in inputs if (package := package_of(path))}):
        folder = scratch / "node_modules" / name
        meta = json.loads((folder / "package.json").read_text(encoding="utf-8"))
        packages[name] = {"version": meta["version"], "license": str(meta.get("license", ""))}
        text = next(
            (
                (folder / candidate).read_text(encoding="utf-8")
                for candidate in LICENCE_NAMES
                if (folder / candidate).is_file()
            ),
            f"{meta.get('license', 'UNKNOWN')} (no licence file in the package)\n",
        )
        texts.append(f"{'=' * 72}\n{name} {meta['version']}\n{'=' * 72}\n\n{text.strip()}\n")
    return packages, "\n".join(texts)


def run(command: list[str], cwd: Path) -> None:
    """Run one build step, failing loudly."""
    subprocess.run(command, cwd=cwd, check=True)


def vendor() -> str:
    """Build, then replace the committed bundle; returns the new bundle id."""
    with tempfile.TemporaryDirectory(prefix="vitrine-jsx-") as directory:
        scratch = Path(directory)
        for name in ("package.json", "package-lock.json", "build.mjs"):
            shutil.copy(SOURCE / name, scratch / name)
        run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], scratch)
        out = scratch / "out"
        run(["node", "build.mjs", str(out), str(scratch / "inputs.json")], scratch)
        shutil.copy(SOURCE / "mount.js", out / "mount.js")
        inputs = json.loads((scratch / "inputs.json").read_text(encoding="utf-8"))
        packages, texts = licences(scratch, inputs)
        (out / LICENSES).write_text(texts, encoding="utf-8")
        files = {path.name: integrity(path.read_bytes()) for path in sorted(out.iterdir())}
        bundle = bundle_id(files)
        shutil.rmtree(TARGET, ignore_errors=True)
        TARGET.mkdir(parents=True)
        shutil.copytree(out, TARGET / bundle)
    manifest = {"bundle": bundle, "imports": IMPORTS, "files": files, "packages": packages}
    (TARGET / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return bundle


def main() -> int:
    """``vendor_jsx.py``: rebuild the bundle and print its id."""
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    print(f"vendored jsx bundle {vendor()} into {TARGET.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
