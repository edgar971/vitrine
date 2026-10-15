"""The extension allowlist (spec § 7.2)."""

from __future__ import annotations

from mcp_drop.domain.media import OCTET_STREAM, content_type, is_text


class DescribeContentType:
    class WhenTheExtensionIsAllowlisted:
        def it_names_the_type(self) -> None:
            assert content_type("report.PDF") == "application/pdf"

    class WhenTheExtensionIsUnknown:
        def it_falls_back_to_octet_stream(self) -> None:
            assert content_type("tool.exe") == OCTET_STREAM

    class WhenThereIsNoExtension:
        def it_falls_back_to_octet_stream(self) -> None:
            assert content_type("Makefile") == OCTET_STREAM


class DescribeIsText:
    class WhenTheFileIsMarkdown:
        def it_is_text(self) -> None:
            assert is_text("notes/readme.md") is True

    class WhenTheFileIsAnImage:
        def it_is_not_text(self) -> None:
            assert is_text("chart.png") is False
