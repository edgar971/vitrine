"""Which scope a component needs and which bucket it spends, carried on the component's tags.

The tags are the single record of both: ``require_scopes`` reads the scope when FastMCP hides a
tool, and the edge middleware reads the scope and the bucket when it audits or rate-limits a call.
"""

from __future__ import annotations

from collections.abc import Iterable

from mcp_drop.domain.models import Scope
from mcp_drop.services.limits import Bucket


def tags(scope: Scope, bucket: Bucket) -> set[str]:
    """The tags that put a tool under ``scope`` and ``bucket``."""
    return {scope.value, f"bucket:{bucket.value}"}


def scope_of(component_tags: Iterable[str]) -> Scope | None:
    """The scope a component's tags name, if any."""
    scopes = {scope.value: scope for scope in Scope}
    return next((scopes[tag] for tag in component_tags if tag in scopes), None)


def bucket_of(component_tags: Iterable[str]) -> Bucket | None:
    """The rate-limit bucket a component's tags name, if any."""
    buckets = {f"bucket:{bucket.value}": bucket for bucket in Bucket}
    return next((buckets[tag] for tag in component_tags if tag in buckets), None)
