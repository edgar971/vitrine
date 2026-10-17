"""The pure parts of the middleware: log redaction and host parsing (spec § 7.4, § 11.1)."""

from __future__ import annotations

from mcp_vitrine.web.middleware import hostname, origin_of, redact

SIGNATURE = "dmlld3xrN20.MAC"


class DescribeRedact:
    class WhenThePathIsASignedView:
        def it_hides_the_signature(self) -> None:
            assert redact(f"/s/{SIGNATURE}/index.html") == "/s/[redacted]/index.html"

    class WhenThePathIsAPinnedSignedView:
        def it_hides_the_signature_and_the_pin(self) -> None:
            assert redact(f"/s/{SIGNATURE}@2/") == "/s/[redacted]/"

    class WhenThePathIsAnUpload:
        def it_hides_the_signature(self) -> None:
            assert redact(f"/u/{SIGNATURE}") == "/u/[redacted]"

    class WhenThePathDoublesItsSlashes:
        def it_still_hides_the_signature(self) -> None:
            assert SIGNATURE not in redact(f"//s//{SIGNATURE}/x")

    class WhenThePathIsPublic:
        def it_keeps_it(self) -> None:
            assert redact("/a/k7m2q9x4tz3wbn6r/index.html") == "/a/k7m2q9x4tz3wbn6r/index.html"


class DescribeHostname:
    class WhenTheHostCarriesAPort:
        def it_drops_the_port(self) -> None:
            assert hostname("Vitrine.Example.com:8443") == "vitrine.example.com"

    class WhenTheHostIsAnIpv6Literal:
        def it_unwraps_it(self) -> None:
            assert hostname("[::1]:8080") == "::1"

    class WhenTheHostEndsInADot:
        def it_drops_the_dot(self) -> None:
            assert hostname("vitrine.example.com.") == "vitrine.example.com"


class DescribeTheOriginOfAUrl:
    class WhenItNamesTheDefaultPort:
        def it_is_the_origin_without_it(self) -> None:
            assert origin_of("https://Vitrine.example.com:443") == "https://vitrine.example.com"

    class WhenItNamesAnotherPort:
        def it_keeps_the_port(self) -> None:
            assert origin_of("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
