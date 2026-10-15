"""The response policy and the one error mapping of spec § 7.2 and § 10."""

from __future__ import annotations

import json

import pytest

from mcp_drop.domain.errors import (
    AuditUnavailable,
    Conflict,
    DropError,
    Expired,
    Invalid,
    NotFound,
    OverQuota,
    RateLimited,
    Refused,
    SchemaMismatch,
    StorageUnavailable,
    TooLarge,
)
from mcp_drop.web.responses import SANDBOX_CSP, Prefix, problem, status_of


class DescribeTheSandbox:
    class WhenItIsRead:
        def it_never_allows_the_same_origin(self) -> None:
            assert "allow-same-origin" not in SANDBOX_CSP

        def it_forbids_framing(self) -> None:
            assert "frame-ancestors 'none'" in SANDBOX_CSP


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
    def it_maps_each_domain_error_once(self, error: DropError, status: int) -> None:
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
