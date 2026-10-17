"""Every state change through MCP writes exactly one audit event (spec § 11.3, security § 6.11)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp_vitrine.services.audit import AuditEvent, AuditEventName
from tests.mcp.conftest import Call

Events = Callable[[], tuple[AuditEvent, ...]]


def _names(events: Events) -> list[AuditEventName]:
    """The events recorded so far, oldest first."""
    return [event.event for event in reversed(events())]


class DescribeTheAuditOfEachChange:
    class WhenAnArtifactIsCreated:
        async def it_records_created_once(self, published: dict[str, Any], events: Events) -> None:
            assert _names(events) == [AuditEventName.CREATED]

    class WhenAnArtifactIsUpdated:
        async def it_records_version_added_once(
            self, call: Call, published: dict[str, Any], events: Events
        ) -> None:
            await call("artifact_update", id=published["id"], files={"index.html": "2"})
            assert _names(events)[1:] == [AuditEventName.VERSION_ADDED]

    class WhenAnArtifactGoesPublic:
        async def it_records_visibility_changed_once(
            self, call: Call, published: dict[str, Any], events: Events
        ) -> None:
            await call("artifact_set_visibility", id=published["id"], visibility="public")
            assert _names(events)[1:] == [AuditEventName.VISIBILITY_CHANGED]

    class WhenLinksAreRevoked:
        async def it_records_links_revoked_once(
            self, call: Call, published: dict[str, Any], events: Events
        ) -> None:
            await call("artifact_revoke_links", id=published["id"])
            assert _names(events)[1:] == [AuditEventName.LINKS_REVOKED]

    class WhenTheMetadataChanges:
        async def it_records_metadata_changed_once(
            self, call: Call, published: dict[str, Any], events: Events
        ) -> None:
            await call("artifact_set_meta", id=published["id"], add_tags=["q3"])
            assert _names(events)[1:] == [AuditEventName.METADATA_CHANGED]

    class WhenAnUploadUrlIsIssued:
        async def it_records_upload_url_issued_once(self, call: Call, events: Events) -> None:
            await call("artifact_upload_url", filename="report.pdf")
            assert _names(events) == [AuditEventName.UPLOAD_URL_ISSUED]

    class WhenAnArtifactIsDeleted:
        async def it_records_deleted_once(
            self, call: Call, published: dict[str, Any], events: Events
        ) -> None:
            await call("artifact_delete", id=published["id"])
            assert _names(events)[1:] == [AuditEventName.DELETED]

    class WhenNothingChanges:
        async def it_records_nothing_for_reads(
            self, call: Call, published: dict[str, Any], events: Events
        ) -> None:
            await call("artifact_get", id=published["id"])
            await call("artifact_list")
            assert _names(events) == [AuditEventName.CREATED]

    class WhenALinkIsMinted:
        async def it_records_link_issued_once(
            self, call: Call, published: dict[str, Any], events: Events
        ) -> None:
            await call("artifact_link", id=published["id"])
            assert _names(events)[1:] == [AuditEventName.LINK_ISSUED]

    class WhenTheTitleIsSecret:
        async def it_keeps_the_title_out_of_the_log(self, call: Call, events: Events) -> None:
            await call("artifact_create", title="Merger plans", files={"index.html": "x"})
            assert "Merger plans" not in events()[0].model_dump_json()
