"""What each tool does, through the MCP client (spec § 5.1, § 10)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from mcp_drop.config import Settings
from mcp_drop.domain.ids import new_id
from mcp_drop.domain.models import Visibility
from mcp_drop.services.audit import AuditEvent, AuditEventName
from mcp_drop.services.container import Services
from tests.mcp.conftest import CLIENT, Call

Events = Callable[[], tuple[AuditEvent, ...]]
FILES = {"index.html": "<h1>hi</h1>"}


class DescribeArtifactCreate:
    class WhenTheFilesAreValid:
        async def it_creates_a_private_artifact(
            self, published: dict[str, Any], services: Services
        ) -> None:
            artifact = services.artifacts.get(published["id"]).artifact
            assert artifact.visibility is Visibility.PRIVATE

        async def it_answers_with_a_signed_link(
            self, published: dict[str, Any], settings: Settings
        ) -> None:
            assert published["url"].startswith(f"{settings.public_base_url}/s/")

        async def it_records_the_caller_as_the_source(
            self, published: dict[str, Any], services: Services
        ) -> None:
            assert services.artifacts.get(published["id"]).artifact.source == CLIENT

    class WhenTheCallIsRetriedWithTheSameClientRef:
        async def it_returns_the_same_artifact_not_created(self, call: Call) -> None:
            first = await call("artifact_create", title="T", files=FILES, client_ref="weekly")
            again = await call("artifact_create", title="T", files=FILES, client_ref="weekly")
            assert (again["id"], again["created"]) == (first["id"], False)

    class WhenTheClientRefIsReusedForOtherContent:
        async def it_fails_with_a_conflict(self, call: Call) -> None:
            await call("artifact_create", title="T", files=FILES, client_ref="weekly")
            with pytest.raises(ToolError, match=r"client_ref"):
                await call("artifact_create", title="U", files=FILES, client_ref="weekly")

    class WhenAPathEscapes:
        async def it_fails_naming_the_problem(self, call: Call) -> None:
            with pytest.raises(ToolError, match=r"\.\."):
                await call("artifact_create", title="T", files={"../x.html": "x"})

    class WhenAnExpiryIsGiven:
        async def it_sets_expires_at_on_the_services_clock(
            self, call: Call, services: Services
        ) -> None:
            created = await call("artifact_create", title="T", files=FILES, expires_in="1h")
            artifact = services.artifacts.get(created["id"]).artifact
            assert artifact.expires_at == artifact.created_at + timedelta(hours=1)

    class WhenAVisibilityIsPassed:
        async def it_refuses_the_argument(self, call: Call) -> None:
            with pytest.raises(ToolError):
                await call("artifact_create", title="T", files=FILES, visibility="public")


class DescribeArtifactUpdate:
    class WhenFilesAreReplaced:
        async def it_keeps_the_id(self, call: Call, published: dict[str, Any]) -> None:
            updated = await call("artifact_update", id=published["id"], files={"index.html": "2"})
            assert updated["id"] == published["id"]

        async def it_writes_the_next_version(self, call: Call, published: dict[str, Any]) -> None:
            updated = await call("artifact_update", id=published["id"], files={"index.html": "2"})
            assert updated["version"] == 2

    class WhenTheExpiryIsCleared:
        async def it_removes_expires_at(
            self, call: Call, published: dict[str, Any], services: Services
        ) -> None:
            await call("artifact_update", id=published["id"], expires_in="1h")
            await call("artifact_update", id=published["id"], expires_in="never")
            assert services.artifacts.get(published["id"]).artifact.expires_at is None

    class WhenTheIdIsUnknown:
        async def it_fails_as_not_found(self, call: Call) -> None:
            with pytest.raises(ToolError, match=r"^not found$"):
                await call("artifact_update", id=new_id(), files=FILES)


class DescribeArtifactGet:
    class WhenTheArtifactExists:
        async def it_lists_the_files(self, call: Call, published: dict[str, Any]) -> None:
            detail = await call("artifact_get", id=published["id"])
            assert [file["path"] for file in detail["files"]] == ["index.html", "notes/readme.md"]

    class WhenTheArtifactHasExpired:
        async def it_fails_as_expired(
            self, call: Call, advance: Callable[[timedelta], None]
        ) -> None:
            created = await call("artifact_create", title="T", files=FILES, expires_in="1m")
            advance(timedelta(minutes=2))
            with pytest.raises(ToolError, match=r"^expired$"):
                await call("artifact_get", id=created["id"])

    class WhenTheIdIsUnknown:
        async def it_answers_exactly_as_for_any_missing_artifact(self, call: Call) -> None:
            with pytest.raises(ToolError, match=r"^not found$"):
                await call("artifact_get", id=new_id())


class DescribeArtifactList:
    class WhenArtifactsExist:
        async def it_returns_them_newest_first(
            self, call: Call, advance: Callable[[timedelta], None]
        ) -> None:
            first = await call("artifact_create", title="first", files=FILES)
            advance(timedelta(minutes=1))
            second = await call("artifact_create", title="second", files=FILES)
            page = await call("artifact_list")
            assert [item["id"] for item in page["items"]] == [second["id"], first["id"]]

        async def it_carries_no_links(self, call: Call, published: dict[str, Any]) -> None:
            page = await call("artifact_list")
            assert "url" not in page["items"][0]


class DescribeArtifactLink:
    class WhenTheArtifactIsPrivate:
        async def it_mints_a_signed_link(
            self, call: Call, published: dict[str, Any], settings: Settings
        ) -> None:
            link = await call("artifact_link", id=published["id"], ttl="1h")
            assert link["url"].startswith(f"{settings.public_base_url}/s/")

        async def it_expires_after_the_ttl(
            self, call: Call, published: dict[str, Any], services: Services
        ) -> None:
            link = await call("artifact_link", id=published["id"], ttl="1h")
            created = services.artifacts.get(published["id"]).artifact.created_at
            assert link["expires_at"] == (created + timedelta(hours=1)).isoformat().replace(
                "+00:00", "Z"
            )

    class WhenTheArtifactIsPublic:
        async def it_returns_the_public_url(
            self, call: Call, published: dict[str, Any], settings: Settings
        ) -> None:
            await call("artifact_set_visibility", id=published["id"], visibility="public")
            link = await call("artifact_link", id=published["id"])
            assert link["url"] == f"{settings.public_base_url}/a/{published['id']}/"


class DescribeArtifactRevokeLinks:
    class WhenLinksAreRevoked:
        async def it_moves_the_link_epoch(self, call: Call, published: dict[str, Any]) -> None:
            revoked = await call("artifact_revoke_links", id=published["id"])
            assert revoked["link_epoch"] == 1


class DescribeArtifactUploadUrl:
    class WhenNoIdIsGiven:
        async def it_pre_allocates_an_id(self, call: Call, services: Services) -> None:
            grant = await call("artifact_upload_url", filename="report.pdf")
            assert services.audit.query(artifact=grant["id"])[0].event is (
                AuditEventName.UPLOAD_URL_ISSUED
            )

        async def it_signs_an_upload_url(self, call: Call, settings: Settings) -> None:
            grant = await call("artifact_upload_url", filename="report.pdf")
            assert grant["put_url"].startswith(f"{settings.public_base_url}/u/")

        async def it_names_the_file_in_the_url(self, call: Call) -> None:
            grant = await call("artifact_upload_url", filename="report.pdf")
            assert grant["put_url"].endswith("?filename=report.pdf")

        async def it_gives_a_curl_command_that_puts_the_file(self, call: Call) -> None:
            grant = await call("artifact_upload_url", filename="report.pdf")
            assert grant["curl"].startswith("curl --fail -T report.pdf ")

    class WhenAnIdIsGiven:
        async def it_keeps_that_id(self, call: Call, published: dict[str, Any]) -> None:
            grant = await call("artifact_upload_url", filename="b.pdf", id=published["id"])
            assert grant["id"] == published["id"]

    class WhenTheIdIsUnknown:
        async def it_fails_as_not_found(self, call: Call) -> None:
            with pytest.raises(ToolError, match=r"^not found$"):
                await call("artifact_upload_url", filename="b.pdf", id=new_id())

    class WhenTheFilenameEscapes:
        async def it_fails_naming_the_problem(self, call: Call) -> None:
            with pytest.raises(ToolError, match=r"\.\."):
                await call("artifact_upload_url", filename="../b.pdf")


class DescribeArtifactDelete:
    class WhenTheArtifactExists:
        async def it_answers_with_the_deleted_id(
            self, call: Call, published: dict[str, Any]
        ) -> None:
            assert (await call("artifact_delete", id=published["id"])) == {
                "deleted": published["id"]
            }

        async def it_removes_the_artifact(self, call: Call, published: dict[str, Any]) -> None:
            await call("artifact_delete", id=published["id"])
            with pytest.raises(ToolError, match=r"^not found$"):
                await call("artifact_get", id=published["id"])
