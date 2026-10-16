"""Scope and bucket tags (``mcp/policy.py``)."""

from __future__ import annotations

from mcp_vitrine.domain.models import Scope
from mcp_vitrine.mcp.policy import bucket_of, scope_of, tags
from mcp_vitrine.services.limits import Bucket


class DescribeTags:
    class WhenAToolIsTagged:
        def it_reads_the_scope_back(self) -> None:
            assert scope_of(tags(Scope.SHARE, Bucket.LINKS)) is Scope.SHARE

        def it_reads_the_bucket_back(self) -> None:
            assert bucket_of(tags(Scope.SHARE, Bucket.LINKS)) is Bucket.LINKS

    class WhenAComponentHasNoPolicyTags:
        def it_names_no_scope(self) -> None:
            assert scope_of({"other"}) is None

        def it_names_no_bucket(self) -> None:
            assert bucket_of({"other"}) is None
