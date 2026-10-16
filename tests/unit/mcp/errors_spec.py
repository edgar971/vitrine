"""Domain errors reach an MCP client once, and vitrine's failures never in detail (spec § 10)."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from mcp_vitrine.domain.errors import (
    AuditUnavailable,
    ConfigError,
    NotFound,
    SchemaMismatch,
    StorageUnavailable,
    VitrineError,
)
from mcp_vitrine.mcp.errors import domain_errors


def _raising(failure: VitrineError) -> None:
    """A tool body that fails with ``failure``."""

    @domain_errors(ToolError)
    def tool() -> None:
        raise failure

    tool()


class DescribeTheMcpErrorEdge:
    class WhenTheErrorIsWordedForTheCaller:
        def it_passes_the_message_through(self) -> None:
            with pytest.raises(ToolError, match=str(NotFound())):
                _raising(NotFound())

    class WhenVitrineItselfFailed:
        @pytest.mark.parametrize(
            "failure",
            [
                StorageUnavailable("/data/artifacts/k7m2: No space left on device"),
                AuditUnavailable("/data/audit/2026-10.jsonl: Read-only file system"),
                SchemaMismatch("meta.json: field required at /data/artifacts/k7m2"),
                ConfigError("/etc/vitrine.toml: unknown key"),
            ],
        )
        def it_masks_the_detail(self, failure: VitrineError) -> None:
            with pytest.raises(ToolError) as raised:
                _raising(failure)
            assert str(raised.value) == "internal error"
