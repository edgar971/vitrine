"""The JSX renderer's shell, and the vendored runtime it loads (spec § 7.2)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

import pytest

from mcp_vitrine.web.jsx import JsxBundle, render_jsx
from mcp_vitrine.web.renderers import renderer_for

STATIC = Path(__file__).parents[3] / "src" / "mcp_vitrine" / "web" / "static" / "jsx"
MANIFEST = json.loads((STATIC / "manifest.json").read_text(encoding="utf-8"))
FILES = sorted(MANIFEST["files"])
SOURCE = b"export default function App() { return <p>hi</p>; }\n"
HOST = re.compile(r"""(?:src|href)=["']?(?:https?:)?//""", re.IGNORECASE)


def source_of(shell: str) -> str:
    """The source the shell inlines, as the browser's JSON.parse reads it."""
    text = shell.split('id="vitrine-source"', 1)[1].split(">", 1)[1].split("</script>", 1)[0]
    return str(json.loads(text))


class DescribeTheJsxRenderer:
    class WhenTheComponentHasADefaultExport:
        def it_returns_a_shell_with_the_source_inlined(self) -> None:
            assert source_of(render_jsx(SOURCE, "App.jsx")) == SOURCE.decode()

        def it_loads_the_mount_script_from_vitrine(self) -> None:
            bundle = JsxBundle.load()
            assert f'src="{bundle.url("mount.js")}"' in render_jsx(SOURCE, "App.jsx")

        def it_pins_every_script_by_its_hash(self) -> None:
            assert render_jsx(SOURCE, "App.jsx").count('integrity="sha384-') == 2

    class WhenTheSourceContainsAClosingScriptTag:
        def it_escapes_it(self) -> None:
            shell = render_jsx(b"const s = '</script><script>alert(1)</script>';", "App.jsx")
            assert shell.count("</script>") == 4

        def it_still_inlines_it_unchanged(self) -> None:
            raw = b"const s = '</script><!--';"
            assert source_of(render_jsx(raw, "App.jsx")) == raw.decode()

    class WhenTheFileIsTsx:
        def it_asks_for_the_typescript_transform(self) -> None:
            assert 'data-kind="tsx"' in render_jsx(SOURCE, "App.tsx")

    class WhenTheFilenameCarriesMarkup:
        def it_escapes_the_title(self) -> None:
            assert "<title>&lt;b&gt;.jsx</title>" in render_jsx(SOURCE, "<b>.jsx")

    def it_references_only_vitrine_urls(self) -> None:
        assert not HOST.search(render_jsx(SOURCE, "App.jsx"))

    class WhenACdnIsConfigured:
        def it_hands_it_to_the_page(self) -> None:
            render = renderer_for("App.jsx", ["jsx"], jsx_cdn="https://esm.sh/")
            assert render is not None
            assert 'data-cdn="https://esm.sh/"' in render(SOURCE, "App.jsx")

    class WhenTheRendererIsNotEnabled:
        @pytest.mark.parametrize("name", ["App.jsx", "App.tsx"])
        def it_serves_the_source_raw(self, name: str) -> None:
            assert renderer_for(name, ["md"]) is None


class DescribeTheVendoredBundle:
    @pytest.mark.parametrize("name", FILES)
    def it_matches_the_recorded_hash(self, name: str) -> None:
        data = (STATIC / MANIFEST["bundle"] / name).read_bytes()
        digest = base64.b64encode(hashlib.sha384(data).digest()).decode()
        assert MANIFEST["files"][name] == f"sha384-{digest}"

    def it_holds_no_file_the_manifest_does_not_list(self) -> None:
        assert sorted(path.name for path in (STATIC / MANIFEST["bundle"]).iterdir()) == FILES

    def it_ships_one_bundle(self) -> None:
        assert sorted(path.name for path in STATIC.iterdir()) == sorted(
            [MANIFEST["bundle"], "manifest.json"]
        )

    def it_is_named_by_its_hashes(self) -> None:
        listing = "".join(f"{name}:{MANIFEST['files'][name]}\n" for name in FILES)
        assert hashlib.sha256(listing.encode()).hexdigest()[:12] == MANIFEST["bundle"]

    def it_records_a_licence_for_every_bundled_package(self) -> None:
        assert all(package["license"] for package in MANIFEST["packages"].values())

    def it_ships_the_licence_texts(self) -> None:
        texts = (STATIC / MANIFEST["bundle"] / "LICENSES.txt").read_text(encoding="utf-8")
        assert all(f"\n{name} " in f"\n{texts}" for name in MANIFEST["packages"])

    def it_stays_within_its_size_budget(self) -> None:
        total = sum(path.stat().st_size for path in (STATIC / MANIFEST["bundle"]).iterdir())
        assert total <= 3 * 1024 * 1024
