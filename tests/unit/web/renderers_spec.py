"""The renderers of spec § 7.2: what agent-written Markdown, tables and JSON become."""

from __future__ import annotations

import csv

from mcp_drop.web.renderers import (
    render_csv,
    render_json,
    render_markdown,
    render_tsv,
    renderer_for,
)


class DescribeTheMarkdownRenderer:
    class WhenTheTextIsCommonMark:
        def it_renders_it_as_html(self) -> None:
            assert "<h1>Energy</h1>" in render_markdown(b"# Energy", "notes.md")

    class WhenTheTextCarriesRawHtml:
        def it_escapes_the_script(self) -> None:
            page = render_markdown(b"<script>alert(1)</script>", "notes.md")
            assert "<script>" not in page

        def it_shows_it_as_text(self) -> None:
            page = render_markdown(b"<script>alert(1)</script>", "notes.md")
            assert "&lt;script&gt;" in page

    class WhenALinkUsesAJavascriptUrl:
        def it_does_not_link_it(self) -> None:
            assert "href" not in render_markdown(b"[x](javascript:alert(1))", "notes.md")

    class WhenTheBytesAreNotUtf8:
        def it_still_renders(self) -> None:
            assert "<p>" in render_markdown(b"caf\xe9", "notes.md")

    class WhenTheFilenameCarriesMarkup:
        def it_escapes_the_title(self) -> None:
            assert "<title>&lt;b&gt;.md</title>" in render_markdown(b"x", "<b>.md")


class DescribeTheTableRenderers:
    class WhenACsvIsRendered:
        def it_makes_the_first_row_the_header(self) -> None:
            assert "<tr><th>month</th><th>kwh</th></tr>" in render_csv(
                b"month,kwh\nsep,12", "a.csv"
            )

        def it_escapes_every_cell(self) -> None:
            assert "&lt;img" in render_csv(b"a\n<img src=x onerror=alert(1)>", "a.csv")

    class WhenATsvIsRendered:
        def it_splits_on_tabs(self) -> None:
            assert "<td>sep</td><td>12</td>" in render_tsv(b"month\tkwh\nsep\t12", "a.tsv")

    class WhenAFieldIsPastTheCsvLimit:
        def it_shows_the_text_instead(self) -> None:
            huge = b'"' + b"x" * (csv.field_size_limit() + 1) + b'"'
            assert render_csv(huge, "a.csv").count("<pre>") == 1


class DescribeTheJsonRenderer:
    class WhenTheJsonParses:
        def it_indents_it(self) -> None:
            assert "{\n  &quot;a&quot;: 1\n}" in render_json(b'{"a":1}', "a.json")

    class WhenTheJsonCarriesMarkup:
        def it_escapes_it(self) -> None:
            assert "<script>" not in render_json(b'{"a":"<script>x</script>"}', "a.json")

    class WhenTheJsonDoesNotParse:
        def it_shows_the_text_escaped(self) -> None:
            assert "&lt;nope" in render_json(b"<nope", "a.json")

    class WhenTheJsonNestsTooDeeply:
        def it_shows_the_text_instead_of_failing(self) -> None:
            assert "<pre>[[[" in render_json(b"[" * 100_000, "a.json")


class DescribeChoosingARenderer:
    class WhenTheExtensionHasOneAndItIsEnabled:
        def it_returns_it(self) -> None:
            assert renderer_for("docs/README.MD", ["md"]) is render_markdown

    class WhenTheRendererIsDisabled:
        def it_returns_none(self) -> None:
            assert renderer_for("notes.md", ["csv"]) is None

    class WhenTheExtensionHasNone:
        def it_returns_none(self) -> None:
            assert renderer_for("index.html", ["md", "csv", "tsv", "json"]) is None
