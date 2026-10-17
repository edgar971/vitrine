"""``artifact_set_meta``: metadata changes write no version and ask no one (spec § 5.1)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from mcp_vitrine.domain.ids import new_id
from mcp_vitrine.domain.models import ArtifactDraft, GateDecision, Visibility
from mcp_vitrine.services.audit import AuditEvent, AuditEventName
from mcp_vitrine.services.container import Services
from tests.mcp.conftest import CLIENT, Call

Events = Callable[[], tuple[AuditEvent, ...]]


@pytest.fixture
def public(services: Services) -> str:
    """A public artifact, made public through the gate."""
    created = services.artifacts.create(
        ArtifactDraft(title="Launch notes", files={"index.html": b"<p>v1</p>"}), client=CLIENT
    )
    services.artifacts.set_visibility(
        created.id, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.APPROVED
    )
    return created.id


@pytest.fixture
async def tagged(call: Call) -> dict[str, Any]:
    """A private artifact with two tags and a project."""
    return await call(
        "artifact_create",
        title="Energy, September",
        files={"index.html": "<h1>hi</h1>"},
        tags=["energy", "draft"],
        project="household",
    )


def _signature(url: str) -> str:
    """The signed segment of a private link."""
    return url.split("/s/", 1)[1].split("/", 1)[0]


def _changes(events: Events) -> list[AuditEvent]:
    """The ``metadata_changed`` events, newest first."""
    return [event for event in events() if event.event is AuditEventName.METADATA_CHANGED]


class DescribeArtifactSetMeta:
    class WhenTheTitleChanges:
        async def it_answers_with_the_new_title(self, call: Call, tagged: dict[str, Any]) -> None:
            changed = await call("artifact_set_meta", id=tagged["id"], title="Energy, October")
            assert changed["title"] == "Energy, October"

        async def it_names_the_field_in_the_audit(
            self, call: Call, tagged: dict[str, Any], events: Events
        ) -> None:
            await call("artifact_set_meta", id=tagged["id"], title="Energy, October")
            assert _changes(events)[0].fields == ("title",)

        async def it_keeps_the_title_out_of_the_audit_log(
            self, call: Call, tagged: dict[str, Any], events: Events
        ) -> None:
            await call("artifact_set_meta", id=tagged["id"], title="Secret plans")
            assert "Secret plans" not in _changes(events)[0].model_dump_json()

    class WhenTheArtifactIsPublic:
        async def it_does_not_ask_the_human(
            self, call: Call, public: str, asked: list[str]
        ) -> None:
            await call("artifact_set_meta", id=public, title="Renamed")
            assert asked == []

        async def it_does_not_add_a_version(
            self, call: Call, public: str, services: Services
        ) -> None:
            await call("artifact_set_meta", id=public, title="Renamed")
            assert services.artifacts.get(public).artifact.version == 1

        async def it_stays_public(self, call: Call, public: str, services: Services) -> None:
            await call("artifact_set_meta", id=public, title="Renamed")
            assert services.artifacts.get(public).artifact.visibility is Visibility.PUBLIC

        async def it_keeps_the_link_epoch(
            self, call: Call, public: str, services: Services
        ) -> None:
            before = services.artifacts.get(public).artifact.link_epoch
            await call("artifact_set_meta", id=public, title="Renamed")
            assert services.artifacts.get(public).artifact.link_epoch == before

    class WhenTheArtifactIsPrivate:
        async def it_keeps_existing_private_links_working(
            self, call: Call, tagged: dict[str, Any], services: Services
        ) -> None:
            await call("artifact_set_meta", id=tagged["id"], title="Renamed")
            assert (
                services.artifacts.open_signed(_signature(tagged["url"])).artifact.title
                == "Renamed"
            )

    class WhenTagsAreAdded:
        async def it_keeps_the_others(self, call: Call, tagged: dict[str, Any]) -> None:
            changed = await call("artifact_set_meta", id=tagged["id"], add_tags=["q3"])
            assert changed["tags"] == ["energy", "draft", "q3"]

        async def it_does_not_repeat_a_tag_it_holds(
            self, call: Call, tagged: dict[str, Any]
        ) -> None:
            changed = await call("artifact_set_meta", id=tagged["id"], add_tags=["Energy"])
            assert changed["tags"] == ["energy", "draft"]

    class WhenTagsAreRemoved:
        async def it_keeps_the_others(self, call: Call, tagged: dict[str, Any]) -> None:
            changed = await call("artifact_set_meta", id=tagged["id"], remove_tags=["DRAFT"])
            assert changed["tags"] == ["energy"]

    class WhenTheTagsAreReplaced:
        async def it_holds_only_the_new_ones(self, call: Call, tagged: dict[str, Any]) -> None:
            changed = await call("artifact_set_meta", id=tagged["id"], tags=["final"])
            assert changed["tags"] == ["final"]

    class WhenTagsAreReplacedAndAddedAtOnce:
        async def it_refuses_the_ambiguity(self, call: Call, tagged: dict[str, Any]) -> None:
            with pytest.raises(ToolError, match="do not combine"):
                await call("artifact_set_meta", id=tagged["id"], tags=["a"], add_tags=["b"])

    class WhenTheProjectIsCleared:
        async def it_leaves_the_artifact_in_no_project(
            self, call: Call, tagged: dict[str, Any]
        ) -> None:
            changed = await call("artifact_set_meta", id=tagged["id"], project="")
            assert changed["project"] is None

    class WhenTheProjectIsNotAName:
        async def it_refuses_it(self, call: Call, tagged: dict[str, Any]) -> None:
            with pytest.raises(ToolError):
                await call("artifact_set_meta", id=tagged["id"], project="Q3 Report")

    class WhenNothingIsNamed:
        async def it_asks_for_something_to_change(self, call: Call, tagged: dict[str, Any]) -> None:
            with pytest.raises(ToolError, match="name something to change"):
                await call("artifact_set_meta", id=tagged["id"])

    class WhenTheValuesAreAlreadyThere:
        async def it_records_nothing(
            self, call: Call, tagged: dict[str, Any], events: Events
        ) -> None:
            await call("artifact_set_meta", id=tagged["id"], title="Energy, September")
            assert _changes(events) == []

    class WhenTheIdIsUnknown:
        async def it_answers_as_for_any_missing_artifact(self, call: Call) -> None:
            with pytest.raises(ToolError, match=r"^not found$"):
                await call("artifact_set_meta", id=new_id(), title="x")


class DescribeArtifactListSearch:
    class WhenTheQueryHasTwoTerms:
        async def it_returns_only_artifacts_matching_both(self, call: Call) -> None:
            await call("artifact_create", title="Energy, September", files={"a.txt": "a"})
            await call(
                "artifact_create", title="Energy, October", files={"a.txt": "a"}, tags=["final"]
            )
            page = await call("artifact_list", query="energy FINAL")
            assert [item["title"] for item in page["items"]] == ["Energy, October"]

    class WhenATermIsInAVersionNote:
        async def it_matches_it(self, call: Call) -> None:
            await call(
                "artifact_create", title="Report", files={"a.txt": "a"}, note="added the chart"
            )
            page = await call("artifact_list", query="chart")
            assert [item["title"] for item in page["items"]] == ["Report"]

    class WhenFilteredByProject:
        async def it_returns_only_that_project(self, call: Call, tagged: dict[str, Any]) -> None:
            await call("artifact_create", title="Elsewhere", files={"a.txt": "a"})
            page = await call("artifact_list", project="household")
            assert [item["id"] for item in page["items"]] == [tagged["id"]]

    class WhenFacetsAreAsked:
        async def it_counts_projects_over_the_whole_match(self, call: Call) -> None:
            for title in ("One", "Two", "Three"):
                await call("artifact_create", title=title, files={"a.txt": "a"}, project="q3")
            page = await call("artifact_list", facets=True, limit=1)
            assert page["facets"]["projects"] == {"q3": 3}

    class WhenFacetsAreNotAsked:
        async def it_leaves_them_out(self, call: Call, tagged: dict[str, Any]) -> None:
            page = await call("artifact_list")
            assert page["facets"] is None

    class WhenSortedByTitle:
        async def it_pages_from_a_to_z(self, call: Call) -> None:
            for title in ("beta", "Alpha", "gamma"):
                await call("artifact_create", title=title, files={"a.txt": "a"})
            first = await call("artifact_list", sort="title", limit=2)
            second = await call("artifact_list", sort="title", limit=2, cursor=first["next_cursor"])
            titles = [item["title"] for item in first["items"] + second["items"]]
            assert titles == ["Alpha", "beta", "gamma"]

    class WhenTheCursorComesFromAnotherSort:
        async def it_refuses_it(self, call: Call) -> None:
            for title in ("beta", "Alpha"):
                await call("artifact_create", title=title, files={"a.txt": "a"})
            first = await call("artifact_list", sort="title", limit=1)
            with pytest.raises(ToolError, match="cursor belongs to sort=title"):
                await call("artifact_list", sort="created", cursor=first["next_cursor"])
