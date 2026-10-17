"""The response policy and the one error mapping of spec § 7.2 and § 10."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.responses import Response

from mcp_vitrine.domain.errors import (
    AuditUnavailable,
    Conflict,
    Expired,
    Invalid,
    NotFound,
    OverQuota,
    RateLimited,
    Refused,
    SchemaMismatch,
    StorageUnavailable,
    TooLarge,
    VitrineError,
)
from mcp_vitrine.web.responses import SANDBOX_CSP, Prefix, artifact_file, problem, status_of


def _served(tmp_path: Path, name: str) -> Response:
    """How one stored file named ``name`` is served on a signed link."""
    file = tmp_path / name
    file.write_bytes(b"x")
    return artifact_file(file, name, prefix=Prefix.SIGNED, download=False)


class DescribeTheSandbox:
    class WhenItIsRead:
        def it_never_allows_the_same_origin(self) -> None:
            assert "allow-same-origin" not in SANDBOX_CSP

        def it_forbids_framing(self) -> None:
            assert "frame-ancestors 'none'" in SANDBOX_CSP


class DescribeTheFilePolicy:
    class WhenTheFileIsAnImage:
        def it_is_sandboxed_without_scripts(self, tmp_path: Path) -> None:
            policy = _served(tmp_path, "chart.png").headers["content-security-policy"]
            assert policy == "sandbox; frame-ancestors 'none'"

    class WhenTheFileIsText:
        def it_is_sandboxed(self, tmp_path: Path) -> None:
            policy = _served(tmp_path, "notes.txt").headers["content-security-policy"]
            assert policy.startswith("sandbox;")

    class WhenTheFileIsAPdf:
        def it_cannot_be_framed(self, tmp_path: Path) -> None:
            policy = _served(tmp_path, "report.pdf").headers["content-security-policy"]
            assert policy == "frame-ancestors 'none'"

    class WhenTheFileIsHtml:
        def it_keeps_the_scripted_sandbox(self, tmp_path: Path) -> None:
            policy = _served(tmp_path, "index.html").headers["content-security-policy"]
            assert policy == SANDBOX_CSP


class DescribeCaching:
    class WhenTheViewIsSigned:
        def it_is_never_stored(self) -> None:
            assert Prefix.SIGNED.cache_control == "private, no-store"

    class WhenTheViewIsPublic:
        def it_is_cached_briefly(self) -> None:
            assert Prefix.PUBLIC.cache_control == "public, max-age=60"


class DescribeStatusOf:
    @pytest.mark.parametrize(
        ("error", "status"),
        [
            (NotFound(), 404),
            (Expired(), 410),
            (Invalid("bad path"), 400),
            (Conflict("used"), 409),
            (TooLarge("big"), 413),
            (RateLimited(3), 429),
            (OverQuota("full"), 507),
            (Refused("declined"), 403),
            (AuditUnavailable("disk"), 500),
            (StorageUnavailable("disk"), 500),
            (SchemaMismatch("v9"), 500),
        ],
    )
    def it_maps_each_domain_error_once(self, error: VitrineError, status: int) -> None:
        assert status_of(error) == status


class DescribeProblem:
    class WhenAViewPathIsMalformed:
        def it_is_a_plain_404(self) -> None:
            assert problem(Invalid("a file path cannot contain '..'"), view=True).status_code == 404

        def it_says_only_not_found(self) -> None:
            assert problem(Invalid("a file path cannot contain '..'"), view=True).body == (
                b"not found"
            )

    class WhenTheApiGetsAnInvalidRequest:
        def it_names_the_problem(self) -> None:
            body = json.loads(bytes(problem(Invalid("a title is required")).body))
            assert body == {"error": "a title is required"}

    class WhenTheErrorIsInternal:
        def it_hides_the_message(self) -> None:
            body = json.loads(bytes(problem(StorageUnavailable("cannot delete /data/x: EIO")).body))
            assert body == {"error": "internal error"}

    class WhenTheCallerIsRateLimited:
        def it_says_when_to_retry(self) -> None:
            assert problem(RateLimited(7)).headers["Retry-After"] == "7"

    class WhenAViewFails:
        def it_still_carries_nosniff(self) -> None:
            assert problem(NotFound(), view=True).headers["X-Content-Type-Options"] == "nosniff"
