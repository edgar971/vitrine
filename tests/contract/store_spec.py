"""Every ``Store`` implementation must pass this suite (spec § 4, ``docs/design.md`` § 5)."""

from __future__ import annotations

import base64
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from mcp_vitrine.domain.clock import Clock
from mcp_vitrine.domain.errors import Conflict, Invalid, NotFound, OverQuota, Refused, TooLarge
from mcp_vitrine.domain.ids import new_id
from mcp_vitrine.domain.models import (
    TAG_LIMIT,
    ArtifactDraft,
    ArtifactQuery,
    LinkRecord,
    ListSort,
    MetaChange,
    Passcode,
    VersionDraft,
    Visibility,
)
from mcp_vitrine.storage.base import Store

ENTRY = "index.html"
CLIENT = "hermes"


def _draft(**changes: object) -> ArtifactDraft:
    """A one-file draft, with ``changes`` applied."""
    fields: dict[str, object] = {"title": "Energy, September", "files": {ENTRY: b"<h1>hi</h1>"}}
    fields.update(changes)
    return ArtifactDraft.model_validate(fields)


def _create(store: Store, **changes: object) -> str:
    """Create an artifact and return its id."""
    artifact_id = new_id()
    store.create(artifact_id, _draft(**changes), source=CLIENT)
    return artifact_id


class DescribeCreatingAVersion:
    class WhenTheDraftIsValid:
        def it_is_private_from_the_first_version(self, store: Store) -> None:
            assert store.get(_create(store)).visibility is Visibility.PRIVATE

        def it_numbers_the_first_version_one(self, store: Store) -> None:
            assert store.get(_create(store)).version == 1

        def it_records_the_calling_client_as_the_source(self, store: Store) -> None:
            assert store.get(_create(store)).source == CLIENT

        def it_prefers_index_html_as_the_entry(self, store: Store) -> None:
            assert store.get(_create(store, files={ENTRY: b"a", "notes.txt": b"b"})).entry == ENTRY

        def it_keeps_the_files_it_was_given(self, store: Store) -> None:
            artifact_id = _create(store, files={ENTRY: b"a", "assets/app.js": b"bb"})
            assert [(entry.path, entry.bytes) for entry in store.files(artifact_id)] == [
                ("assets/app.js", 2),
                (ENTRY, 1),
            ]

        def it_weighs_the_version(self, store: Store) -> None:
            assert store.get(_create(store, files={ENTRY: b"0123456789"})).versions[0].bytes == 10

        def it_carries_the_expiry(self, store: Store, clock: Clock) -> None:
            later = clock.now() + timedelta(days=1)
            assert store.get(_create(store, expires_at=later)).expires_at == later

    class WhenTheIdIsAlreadyTaken:
        def it_conflicts(self, store: Store) -> None:
            artifact_id = _create(store)
            with pytest.raises(Conflict):
                store.create(artifact_id, _draft(), source=CLIENT)

    class WhenTheVersionWouldBeEmpty:
        def it_refuses_the_whole_version(self, store: Store) -> None:
            with pytest.raises(Invalid):
                _create(store, files={})

    class WhenAPathEscapesTheVersion:
        def it_refuses_the_whole_version(self, store: Store) -> None:
            with pytest.raises(Invalid):
                _create(store, files={"../escape.txt": b"x", ENTRY: b"y"})

        def it_leaves_nothing_visible(self, store: Store) -> None:
            with pytest.raises(Invalid):
                _create(store, files={"a/../../escape.txt": b"x"})
            assert store.list(ArtifactQuery()).items == ()

    class WhenTheVersionHasNoEntryToChoose:
        def it_leaves_the_artifact_uncreated(self, store: Store) -> None:
            artifact_id = new_id()
            with pytest.raises(Invalid):
                store.create(
                    artifact_id, _draft(files={"a.html": b"x", "b.html": b"y"}), source=CLIENT
                )
            with pytest.raises(NotFound):
                store.get(artifact_id)

    class WhenAFileIsTooLarge:
        def it_refuses_the_whole_version(self, limited_store: Store) -> None:
            with pytest.raises(TooLarge):
                limited_store.create(new_id(), _draft(files={ENTRY: b"x" * 9}), source=CLIENT)

    class WhenThereAreTooManyFiles:
        def it_refuses_the_whole_version(self, limited_store: Store) -> None:
            files = {f"file{index}.txt": b"x" for index in range(3)}
            with pytest.raises(TooLarge):
                limited_store.create(new_id(), _draft(files=files), source=CLIENT)

    class WhenTheVersionWouldBreakTheQuota:
        def it_refuses_the_write(self, limited_store: Store) -> None:
            limited_store.create(new_id(), _draft(files={ENTRY: b"x" * 8}), source=CLIENT)
            with pytest.raises(OverQuota):
                limited_store.create(new_id(), _draft(files={ENTRY: b"x" * 8}), source=CLIENT)

        def it_leaves_the_quota_unspent(self, limited_store: Store) -> None:
            limited_store.create(new_id(), _draft(files={ENTRY: b"x" * 8}), source=CLIENT)
            with pytest.raises(OverQuota):
                limited_store.create(new_id(), _draft(files={ENTRY: b"x" * 8}), source=CLIENT)
            assert limited_store.usage_bytes() == 8


class DescribeAddingAVersion:
    class WhenFilesReplaceThePreviousOnes:
        def it_keeps_the_id_and_the_version_number_moves(self, store: Store) -> None:
            artifact_id = _create(store)
            updated = store.add_version(artifact_id, VersionDraft(files={"report.html": b"new"}))
            assert updated.id == artifact_id
            assert updated.version == 2

        def it_serves_only_the_new_files(self, store: Store) -> None:
            artifact_id = _create(store)
            store.add_version(artifact_id, VersionDraft(files={"report.html": b"new"}))
            assert [entry.path for entry in store.files(artifact_id)] == ["report.html"]

        def it_keeps_the_previous_version_readable(self, store: Store) -> None:
            artifact_id = _create(store)
            store.add_version(artifact_id, VersionDraft(files={"report.html": b"new"}))
            assert store.read(artifact_id, ENTRY, version=1) == b"<h1>hi</h1>"

        def it_keeps_the_title_it_was_not_given(self, store: Store) -> None:
            artifact_id = _create(store, title="Energy, September")
            updated = store.add_version(artifact_id, VersionDraft(files={"report.html": b"new"}))
            assert updated.title == "Energy, September"

    class WhenFilesMerge:
        def it_overlays_them_on_the_current_version(self, store: Store) -> None:
            artifact_id = _create(store)
            store.add_version(
                artifact_id,
                VersionDraft(files={"report.html": b"new"}, merge=True, entry="report.html"),
            )
            assert [entry.path for entry in store.files(artifact_id)] == [ENTRY, "report.html"]

        def it_carries_the_untouched_bytes_over(self, store: Store) -> None:
            artifact_id = _create(store)
            store.add_version(artifact_id, VersionDraft(files={"report.html": b"new"}, merge=True))
            assert store.read(artifact_id, ENTRY) == b"<h1>hi</h1>"

    class WhenNoFilesAreGiven:
        def it_carries_the_current_files_over(self, store: Store) -> None:
            artifact_id = _create(store)
            store.add_version(artifact_id, VersionDraft(title="Renamed"))
            assert store.read(artifact_id, ENTRY) == b"<h1>hi</h1>"

        def it_still_writes_the_next_version(self, store: Store) -> None:
            artifact_id = _create(store)
            assert store.add_version(artifact_id, VersionDraft(title="Renamed")).version == 2

    class WhenNoEntryIsGiven:
        def it_keeps_the_previous_entry_while_it_exists(self, store: Store) -> None:
            artifact_id = _create(
                store, entry="report.html", files={"report.html": b"a", "notes.txt": b"b"}
            )
            updated = store.add_version(
                artifact_id, VersionDraft(files={"report.html": b"c", "notes.txt": b"d"})
            )
            assert updated.entry == "report.html"

        def it_falls_back_when_the_previous_entry_is_gone(self, store: Store) -> None:
            artifact_id = _create(
                store, entry="report.html", files={"report.html": b"a", "notes.txt": b"b"}
            )
            updated = store.add_version(artifact_id, VersionDraft(files={"notes.txt": b"d"}))
            assert updated.entry == "notes.txt"

    class WhenAnEntryIsNamedThatIsNotThere:
        def it_refuses_the_version(self, store: Store) -> None:
            artifact_id = _create(store)
            with pytest.raises(Invalid):
                store.add_version(artifact_id, VersionDraft(files={"a.html": b"x"}, entry="b.html"))

        def it_leaves_the_artifact_on_its_previous_version(self, store: Store) -> None:
            artifact_id = _create(store)
            with pytest.raises(Invalid):
                store.add_version(artifact_id, VersionDraft(files={"a.html": b"x"}, entry="b.html"))
            assert store.get(artifact_id).version == 1

    class WhenTheSamePathsAreUsedWithoutAMerge:
        def it_accepts_the_replacement(self, store: Store) -> None:
            artifact_id = _create(store, files={ENTRY: b"y", "data": b"x"})
            updated = store.add_version(
                artifact_id, VersionDraft(files={"data/inner.txt": b"z", ENTRY: b"y"})
            )
            assert updated.version == 2

    class WhenAMergeNeedsAFileAsADirectory:
        def it_refuses_the_version(self, store: Store) -> None:
            artifact_id = _create(store, files={ENTRY: b"y", "data": b"x"})
            with pytest.raises(Invalid):
                store.add_version(
                    artifact_id, VersionDraft(files={"data/inner.txt": b"z"}, merge=True)
                )

    class WhenAMergeNeedsADirectoryAsAFile:
        def it_refuses_the_version(self, store: Store) -> None:
            artifact_id = _create(store, files={ENTRY: b"y", "data/inner.txt": b"x"})
            with pytest.raises(Invalid):
                store.add_version(artifact_id, VersionDraft(files={"data": b"z"}, merge=True))

    class WhenTheArtifactIsUnknown:
        def it_is_not_found(self, store: Store) -> None:
            with pytest.raises(NotFound):
                store.add_version(new_id(), VersionDraft(files={"a.html": b"x"}))

    class WhenTheExpiryMoves:
        def it_takes_the_new_one(self, store: Store, clock: Clock) -> None:
            artifact_id = _create(store)
            later = clock.now() + timedelta(days=1)
            updated = store.add_version(
                artifact_id, VersionDraft(files={"a.html": b"x"}, expires_at=later)
            )
            assert updated.expires_at == later

        def it_is_cleared_on_request(self, store: Store, clock: Clock) -> None:
            artifact_id = _create(store, expires_at=clock.now() + timedelta(days=1))
            updated = store.add_version(
                artifact_id, VersionDraft(files={"a.html": b"x"}, clear_expiry=True)
            )
            assert updated.expires_at is None


class DescribeTheWritePreconditions:
    class WhenOnlyAPrivateArtifactMayBeUpdated:
        def it_refuses_a_public_one(self, store: Store) -> None:
            artifact_id = _create(store)
            store.set_visibility(artifact_id, Visibility.PUBLIC)
            with pytest.raises(Refused):
                store.add_version(artifact_id, VersionDraft(title="x"), if_private=True)

        def it_writes_a_private_one(self, store: Store) -> None:
            artifact_id = _create(store)
            assert (
                store.add_version(artifact_id, VersionDraft(title="x"), if_private=True).version
                == 2
            )

    class WhenTheExpectedVersionIsNoLongerCurrent:
        def it_refuses_the_version(self, store: Store) -> None:
            artifact_id = _create(store)
            store.add_version(artifact_id, VersionDraft(title="x"))
            with pytest.raises(Conflict):
                store.add_version(artifact_id, VersionDraft(title="y"), if_version=1)

        def it_refuses_the_visibility_change(self, store: Store) -> None:
            artifact_id = _create(store)
            store.add_version(artifact_id, VersionDraft(title="x"))
            with pytest.raises(Conflict):
                store.set_visibility(artifact_id, Visibility.PUBLIC, if_version=1)


class DescribePruning:
    class WhenMoreVersionsThanTheRetentionAreKept:
        def it_keeps_the_newest_ones(self, two_version_store: Store) -> None:
            artifact_id = _create(two_version_store)
            for index in range(2):
                two_version_store.add_version(
                    artifact_id, VersionDraft(files={f"v{index}.html": b"x"})
                )
            assert len(two_version_store.prune(artifact_id).versions) == 2

        def it_removes_the_dropped_version_directory(self, two_version_store: Store) -> None:
            artifact_id = _create(two_version_store)
            for index in range(2):
                two_version_store.add_version(
                    artifact_id, VersionDraft(files={f"v{index}.html": b"x"})
                )
            two_version_store.prune(artifact_id)
            with pytest.raises(NotFound):
                two_version_store.files(artifact_id, version=1)

        def it_keeps_everything_within_the_retention(self, store: Store) -> None:
            artifact_id = _create(store)
            store.add_version(artifact_id, VersionDraft(files={"a.html": b"x"}))
            assert len(store.prune(artifact_id).versions) == 2


class DescribeReading:
    class WhenTheFileIsThere:
        def it_returns_its_bytes(self, store: Store) -> None:
            artifact_id = _create(store)
            assert store.read(artifact_id, ENTRY) == b"<h1>hi</h1>"

    class WhenThePathIsUnsafe:
        def it_is_a_bad_request(self, store: Store) -> None:
            artifact_id = _create(store)
            with pytest.raises(Invalid):
                store.read(artifact_id, "../meta.json")

    class WhenTheFileIsNotThere:
        def it_is_not_found(self, store: Store) -> None:
            artifact_id = _create(store)
            with pytest.raises(NotFound):
                store.read(artifact_id, "missing.txt")

    class WhenTheVersionIsNotThere:
        def it_is_not_found(self, store: Store) -> None:
            artifact_id = _create(store)
            with pytest.raises(NotFound):
                store.files(artifact_id, version=7)

    class WhenTheIdIsMalformed:
        def it_is_not_found(self, store: Store) -> None:
            with pytest.raises(NotFound):
                store.get("not-an-id")

        def it_is_not_found_even_when_the_shape_is_close(self, store: Store) -> None:
            with pytest.raises(NotFound):
                store.get("k7m2q9x4tz3wbn6i")


class DescribeListing:
    class WhenNothingHasBeenCreated:
        def it_returns_an_empty_page(self, store: Store) -> None:
            assert store.list(ArtifactQuery()).items == ()

    class WhenSeveralArtifactsExist:
        def it_returns_the_newest_first(
            self, store: Store, advance: Callable[[timedelta], None]
        ) -> None:
            older = _create(store, title="Older")
            advance(timedelta(minutes=5))
            newer = _create(store, title="Newer")
            assert [item.id for item in store.list(ArtifactQuery()).items] == [newer, older]

        def it_matches_a_title_substring(self, store: Store) -> None:
            _create(store, title="Energy, September")
            _create(store, title="Notes")
            page = store.list(ArtifactQuery(query="sep"))
            assert [item.title for item in page.items] == ["Energy, September"]

        def it_matches_a_tag_substring(self, store: Store) -> None:
            _create(store, title="Chart", tags=("energy",))
            _create(store, title="Minutes", tags=("misc",))
            assert len(store.list(ArtifactQuery(query="ener")).items) == 1

        def it_filters_by_tag(self, store: Store) -> None:
            _create(store, tags=("energy",))
            _create(store, tags=("notes",))
            assert len(store.list(ArtifactQuery(tag="ENERGY")).items) == 1

        def it_filters_by_source(self, store: Store) -> None:
            _create(store)
            other = new_id()
            store.create(other, _draft(), source="ci-bot")
            assert [item.id for item in store.list(ArtifactQuery(source="ci-bot")).items] == [other]

        def it_filters_by_visibility(self, store: Store) -> None:
            artifact_id = _create(store)
            store.set_visibility(artifact_id, Visibility.PUBLIC)
            _create(store)
            assert len(store.list(ArtifactQuery(visibility=Visibility.PUBLIC)).items) == 1

        def it_never_carries_a_url(self, store: Store) -> None:
            _create(store)
            assert "url" not in store.list(ArtifactQuery()).items[0].model_dump()

    class WhenThePageIsLimited:
        def it_hands_back_a_cursor(
            self, store: Store, advance: Callable[[timedelta], None]
        ) -> None:
            for index in range(3):
                _create(store, title=f"Artifact {index}")
                advance(timedelta(minutes=1))
            assert store.list(ArtifactQuery(limit=2)).next_cursor is not None

        def it_returns_the_rest_on_the_next_page(
            self, store: Store, advance: Callable[[timedelta], None]
        ) -> None:
            for index in range(3):
                _create(store, title=f"Artifact {index}")
                advance(timedelta(minutes=1))
            first = store.list(ArtifactQuery(limit=2))
            second = store.list(ArtifactQuery(limit=2, cursor=first.next_cursor))
            assert len(second.items) == 1
            assert second.next_cursor is None

        def it_never_repeats_a_row(
            self, store: Store, advance: Callable[[timedelta], None]
        ) -> None:
            for index in range(3):
                _create(store, title=f"Artifact {index}")
                advance(timedelta(minutes=1))
            first = store.list(ArtifactQuery(limit=2))
            second = store.list(ArtifactQuery(limit=2, cursor=first.next_cursor))
            assert not {item.id for item in first.items} & {item.id for item in second.items}

    class WhenTheCursorIsNotACursor:
        def it_is_a_bad_request(self, store: Store) -> None:
            with pytest.raises(Invalid):
                store.list(ArtifactQuery(cursor="not-a-cursor"))

    class WhenTheCursorIsNotAList:
        def it_is_a_bad_request(self, store: Store) -> None:
            cursor = base64.urlsafe_b64encode(b'{"sort":"updated"}').decode()
            with pytest.raises(Invalid):
                store.list(ArtifactQuery(cursor=cursor.rstrip("=")))

    class WhenTheCursorCarriesAKeyOfTheWrongType:
        def it_is_a_bad_request(self, store: Store) -> None:
            _create(store)
            cursor = base64.urlsafe_b64encode(b'["updated","2020-01-01T00:00:00","x"]').decode()
            with pytest.raises(Invalid):
                store.list(ArtifactQuery(cursor=cursor.rstrip("=")))


class DescribeTheQuotaOfAMerge:
    class WhenOnlyTheNewBytesMustFit:
        def it_charges_the_merge_its_new_bytes(self, merge_quota_store: Store) -> None:
            artifact_id = new_id()
            merge_quota_store.create(artifact_id, _draft(files={"a.txt": b"x" * 4}), source=CLIENT)
            updated = merge_quota_store.add_version(
                artifact_id, VersionDraft(files={"b.txt": b"y" * 4}, merge=True)
            )
            assert updated.version == 2

    class WhenTheNewBytesDoNotFit:
        def it_refuses_the_write(self, merge_quota_store: Store) -> None:
            artifact_id = new_id()
            merge_quota_store.create(artifact_id, _draft(files={"a.txt": b"x" * 4}), source=CLIENT)
            with pytest.raises(OverQuota):
                merge_quota_store.add_version(
                    artifact_id, VersionDraft(files={"b.txt": b"y" * 6}, merge=True)
                )


class DescribeDeleting:
    class WhenTheArtifactExists:
        def it_is_gone(self, store: Store) -> None:
            artifact_id = _create(store)
            store.delete(artifact_id)
            with pytest.raises(NotFound):
                store.get(artifact_id)

    class WhenTheArtifactIsUnknown:
        def it_is_not_found(self, store: Store) -> None:
            with pytest.raises(NotFound):
                store.delete(new_id())

    class WhenTheIdIsMalformed:
        def it_is_not_found(self, store: Store) -> None:
            with pytest.raises(NotFound):
                store.delete("not-an-id")


class DescribeVisibilityAndRevocation:
    class WhenItBecomesPublic:
        def it_increments_the_link_epoch(self, store: Store) -> None:
            artifact_id = _create(store)
            assert store.set_visibility(artifact_id, Visibility.PUBLIC).link_epoch == 1

        def it_records_the_visibility(self, store: Store) -> None:
            artifact_id = _create(store)
            assert (
                store.set_visibility(artifact_id, Visibility.PUBLIC).visibility is Visibility.PUBLIC
            )

        def it_increments_again_on_the_way_back(self, store: Store) -> None:
            artifact_id = _create(store)
            store.set_visibility(artifact_id, Visibility.PUBLIC)
            assert store.set_visibility(artifact_id, Visibility.PRIVATE).link_epoch == 2

    class WhenTheVisibilityDoesNotChange:
        def it_leaves_the_epoch_alone(self, store: Store) -> None:
            artifact_id = _create(store)
            assert store.set_visibility(artifact_id, Visibility.PRIVATE).link_epoch == 0

    class WhenLinksAreRevoked:
        def it_increments_the_epoch(self, store: Store) -> None:
            assert store.revoke_links(_create(store)).link_epoch == 1

        def it_leaves_the_visibility_alone(self, store: Store) -> None:
            artifact_id = _create(store)
            store.set_visibility(artifact_id, Visibility.PUBLIC)
            assert store.revoke_links(artifact_id).visibility is Visibility.PUBLIC

    class WhenTheArtifactIsUnknown:
        def it_is_not_found(self, store: Store) -> None:
            with pytest.raises(NotFound):
                store.set_visibility(new_id(), Visibility.PUBLIC)
            with pytest.raises(NotFound):
                store.revoke_links(new_id())


def _link(
    clock: Clock, lid: str = "AbCdEfGh_-0", *, ttl: timedelta = timedelta(days=7)
) -> LinkRecord:
    """A link record, live for ``ttl``."""
    return LinkRecord(lid=lid, created_at=clock.now(), expires_at=clock.now() + ttl)


class DescribeLinkRecords:
    class WhenALinkIsAdded:
        def it_is_recorded(self, store: Store, clock: Clock) -> None:
            artifact_id = _create(store)
            store.add_link(artifact_id, _link(clock), limit=10)
            assert [record.lid for record in store.get(artifact_id).links] == ["AbCdEfGh_-0"]

    class WhenTheLimitIsReached:
        def it_is_too_large(self, store: Store, clock: Clock) -> None:
            artifact_id = _create(store)
            store.add_link(artifact_id, _link(clock), limit=1)
            with pytest.raises(TooLarge):
                store.add_link(artifact_id, _link(clock, "BbCdEfGh_-0"), limit=1)

    class WhenAnExpiredRecordIsInTheWay:
        def it_is_dropped_first(
            self, store: Store, clock: Clock, advance: Callable[[timedelta], None]
        ) -> None:
            artifact_id = _create(store)
            store.add_link(artifact_id, _link(clock, ttl=timedelta(hours=1)), limit=1)
            advance(timedelta(hours=2))
            store.add_link(artifact_id, _link(clock, "BbCdEfGh_-0"), limit=1)
            assert [record.lid for record in store.get(artifact_id).links] == ["BbCdEfGh_-0"]

    class WhenOneIsRemoved:
        def it_keeps_the_others(self, store: Store, clock: Clock) -> None:
            artifact_id = _create(store)
            store.add_link(artifact_id, _link(clock), limit=10)
            store.add_link(artifact_id, _link(clock, "BbCdEfGh_-0"), limit=10)
            store.remove_link(artifact_id, "AbCdEfGh_-0")
            assert [record.lid for record in store.get(artifact_id).links] == ["BbCdEfGh_-0"]

        def it_keeps_the_epoch(self, store: Store, clock: Clock) -> None:
            artifact_id = _create(store)
            store.add_link(artifact_id, _link(clock), limit=10)
            assert store.remove_link(artifact_id, "AbCdEfGh_-0").link_epoch == 0

    class WhenTheRecordIsUnknown:
        def it_is_not_found(self, store: Store) -> None:
            with pytest.raises(NotFound):
                store.remove_link(_create(store), "AbCdEfGh_-0")

    class WhenEveryLinkIsRevoked:
        def it_drops_every_record(self, store: Store, clock: Clock) -> None:
            artifact_id = _create(store)
            store.add_link(artifact_id, _link(clock), limit=10)
            assert store.revoke_links(artifact_id).links == ()

    class WhenTheVisibilityChanges:
        def it_drops_every_record(self, store: Store, clock: Clock) -> None:
            artifact_id = _create(store)
            store.add_link(artifact_id, _link(clock), limit=10)
            assert store.set_visibility(artifact_id, Visibility.PUBLIC).links == ()

    class WhenTheSweepRuns:
        def it_drops_the_expired_records(
            self, store: Store, clock: Clock, advance: Callable[[timedelta], None]
        ) -> None:
            artifact_id = _create(store)
            store.add_link(artifact_id, _link(clock, ttl=timedelta(hours=1)), limit=10)
            store.add_link(artifact_id, _link(clock, "BbCdEfGh_-0"), limit=10)
            advance(timedelta(hours=2))
            assert (store.sweep_links(), len(store.get(artifact_id).links)) == (1, 1)


class DescribeThePublicWindow:
    class WhenItGoesPublicWithAnEnd:
        def it_records_the_end(self, store: Store, clock: Clock) -> None:
            end = clock.now() + timedelta(days=1)
            artifact_id = _create(store)
            assert (
                store.set_visibility(artifact_id, Visibility.PUBLIC, public_until=end).public_until
                == end
            )

    class WhenTheEndIsMoved:
        def it_keeps_the_epoch(self, store: Store, clock: Clock) -> None:
            artifact_id = _create(store)
            store.set_visibility(artifact_id, Visibility.PUBLIC, public_until=clock.now())
            moved = store.set_visibility(
                artifact_id, Visibility.PUBLIC, public_until=clock.now() + timedelta(days=7)
            )
            assert moved.link_epoch == 1

    class WhenItGoesPrivate:
        def it_clears_the_end(self, store: Store, clock: Clock) -> None:
            artifact_id = _create(store)
            store.set_visibility(artifact_id, Visibility.PUBLIC, public_until=clock.now())
            assert store.set_visibility(artifact_id, Visibility.PRIVATE).public_until is None

    class WhenTheEndHasPassed:
        def it_is_lapsed(
            self, store: Store, clock: Clock, advance: Callable[[timedelta], None]
        ) -> None:
            artifact_id = _create(store)
            store.set_visibility(
                artifact_id, Visibility.PUBLIC, public_until=clock.now() + timedelta(hours=1)
            )
            advance(timedelta(hours=1))
            assert store.lapsed_ids() == (artifact_id,)

        def it_lists_nothing_while_it_is_open(self, store: Store, clock: Clock) -> None:
            artifact_id = _create(store)
            store.set_visibility(
                artifact_id, Visibility.PUBLIC, public_until=clock.now() + timedelta(hours=1)
            )
            assert store.lapsed_ids() == ()


class DescribeClaimingAClientRef:
    class WhenTheRefIsFree:
        def it_reports_a_new_claim(self, store: Store) -> None:
            claim = store.claim_ref(
                client=CLIENT, ref="weekly", request_hash="aa", artifact_id=_create(store)
            )
            assert claim.reused is False
            assert claim.request_hash == "aa"

    class WhenTheSameRequestComesBack:
        def it_points_at_the_artifact_of_the_first_call(self, store: Store) -> None:
            artifact_id = _create(store)
            store.claim_ref(client=CLIENT, ref="weekly", request_hash="aa", artifact_id=artifact_id)
            again = store.claim_ref(
                client=CLIENT, ref="weekly", request_hash="aa", artifact_id=new_id()
            )
            assert again.reused is True
            assert again.id == artifact_id

    class WhenADifferentRequestUsesTheSameRef:
        def it_conflicts(self, store: Store) -> None:
            store.claim_ref(
                client=CLIENT, ref="weekly", request_hash="aa", artifact_id=_create(store)
            )
            with pytest.raises(Conflict):
                store.claim_ref(
                    client=CLIENT, ref="weekly", request_hash="bb", artifact_id=new_id()
                )

    class WhenTheRefBelongsToAnotherClient:
        def it_is_independent(self, store: Store) -> None:
            artifact_id = _create(store)
            store.claim_ref(client=CLIENT, ref="weekly", request_hash="aa", artifact_id=artifact_id)
            claim = store.claim_ref(
                client="ci-bot", ref="weekly", request_hash="aa", artifact_id=artifact_id
            )
            assert claim.reused is False

    class WhenTheRefTtlHasPassed:
        def it_frees_the_key(self, store: Store, advance: Callable[[timedelta], None]) -> None:
            store.claim_ref(
                client=CLIENT, ref="weekly", request_hash="aa", artifact_id=_create(store)
            )
            advance(timedelta(hours=25))
            claim = store.claim_ref(
                client=CLIENT, ref="weekly", request_hash="bb", artifact_id=new_id()
            )
            assert claim.reused is False

    class WhenTheArtifactWasDeleted:
        def it_frees_the_key(self, store: Store) -> None:
            artifact_id = _create(store)
            store.claim_ref(client=CLIENT, ref="weekly", request_hash="aa", artifact_id=artifact_id)
            store.delete(artifact_id)
            claim = store.claim_ref(
                client=CLIENT, ref="weekly", request_hash="bb", artifact_id=new_id()
            )
            assert claim.reused is False

    class WhenTheArtifactNeverArrived:
        def it_keeps_the_key_until_the_ref_ttl(self, store: Store) -> None:
            pending = new_id()
            store.claim_ref(client=CLIENT, ref="weekly", request_hash="aa", artifact_id=pending)
            again = store.claim_ref(
                client=CLIENT, ref="weekly", request_hash="aa", artifact_id=new_id()
            )
            assert (again.reused, again.id) == (True, pending)

    class WhenTwoCallsRace:
        def it_lets_exactly_one_of_them_win(self, store: Store) -> None:
            artifact_id = _create(store)

            def claim() -> bool:
                return store.claim_ref(
                    client=CLIENT, ref="weekly", request_hash="aa", artifact_id=artifact_id
                ).reused

            with ThreadPoolExecutor(max_workers=8) as pool:
                outcomes = list(pool.map(lambda _: claim(), range(8)))
            assert outcomes.count(False) == 1


class DescribeReleasingAClientRef:
    class WhenTheRefStillNamesTheArtifact:
        def it_frees_the_key(self, store: Store) -> None:
            store.claim_ref(client=CLIENT, ref="weekly", request_hash="a", artifact_id="first")
            store.release_ref(client=CLIENT, ref="weekly", artifact_id="first")
            claim = store.claim_ref(
                client=CLIENT, ref="weekly", request_hash="b", artifact_id="next"
            )
            assert (claim.id, claim.reused) == ("next", False)

    class WhenTheRefNamesAnotherArtifact:
        def it_keeps_the_key(self, store: Store) -> None:
            artifact_id = _create(store)
            store.claim_ref(client=CLIENT, ref="weekly", request_hash="a", artifact_id=artifact_id)
            store.release_ref(client=CLIENT, ref="weekly", artifact_id=new_id())
            claim = store.claim_ref(
                client=CLIENT, ref="weekly", request_hash="a", artifact_id=new_id()
            )
            assert claim.reused


class DescribeSweeping:
    class WhenARefIsStale:
        def it_removes_the_record(self, store: Store, advance: Callable[[timedelta], None]) -> None:
            store.claim_ref(
                client=CLIENT, ref="weekly", request_hash="aa", artifact_id=_create(store)
            )
            advance(timedelta(hours=25))
            assert store.sweep_refs() == 1

    class WhenAnArtifactHasExpired:
        def it_names_it_for_deletion(
            self, store: Store, clock: Clock, advance: Callable[[timedelta], None]
        ) -> None:
            artifact_id = _create(store, expires_at=clock.now() + timedelta(minutes=1))
            advance(timedelta(minutes=2))
            assert artifact_id in store.expired_ids()

        def it_does_not_name_a_live_artifact(self, store: Store) -> None:
            _create(store)
            assert store.expired_ids() == ()


class DescribeCompensation:
    class WhenAMutationFailedAfterItLanded:
        def it_can_be_rolled_back(self, store: Store) -> None:
            artifact_id = _create(store)
            original = store.get(artifact_id)
            store.add_version(artifact_id, VersionDraft(files={"report.html": b"new"}))
            store.vitrine_version(artifact_id, 2)
            store.replace_meta(original)
            assert store.get(artifact_id).version == 1

        def it_can_restore_the_visibility(self, store: Store) -> None:
            artifact_id = _create(store)
            original = store.get(artifact_id)
            store.set_visibility(artifact_id, Visibility.PUBLIC)
            store.replace_meta(original)
            assert store.get(artifact_id).visibility is Visibility.PRIVATE

    class WhenTheArtifactChangedAgainSince:
        def it_writes_nothing(self, store: Store) -> None:
            artifact_id = _create(store)
            original = store.get(artifact_id)
            published = store.set_visibility(artifact_id, Visibility.PUBLIC)
            store.revoke_links(artifact_id)
            assert store.replace_meta(original, if_current=published) is False

        def it_keeps_the_later_change(self, store: Store) -> None:
            artifact_id = _create(store)
            original = store.get(artifact_id)
            published = store.set_visibility(artifact_id, Visibility.PUBLIC)
            store.revoke_links(artifact_id)
            store.replace_meta(original, if_current=published)
            assert store.get(artifact_id).link_epoch == 2

    class WhenItIsStillWhatTheChangeLeft:
        def it_restores_it(self, store: Store) -> None:
            artifact_id = _create(store)
            original = store.get(artifact_id)
            published = store.set_visibility(artifact_id, Visibility.PUBLIC)
            assert store.replace_meta(original, if_current=published) is True


class DescribeUsage:
    class WhenNothingIsStored:
        def it_is_zero(self, store: Store) -> None:
            assert store.usage_bytes() == 0

    class WhenAVersionIsWritten:
        def it_counts_its_bytes(self, store: Store) -> None:
            _create(store, files={ENTRY: b"0123456789"})
            assert store.usage_bytes() == 10

    class WhenAMergeHardLinksThePreviousFiles:
        def it_counts_the_shared_bytes_once(self, store: Store) -> None:
            artifact_id = _create(store, files={ENTRY: b"0123456789"})
            store.add_version(artifact_id, VersionDraft(files={"b.txt": b"12345"}, merge=True))
            assert store.usage_bytes() == 15

    class WhenTheArtifactIsDeleted:
        def it_frees_its_bytes(self, store: Store) -> None:
            artifact_id = _create(store, files={ENTRY: b"0123456789"})
            store.delete(artifact_id)
            assert store.usage_bytes() == 0


class DescribeUploadNonces:
    class WhenACreateSpendsOne:
        def it_records_it(self, store: Store) -> None:
            assert store.get(_create(store, upload_nonce="n1")).used_upload_nonces == ("n1",)

    class WhenAVersionSpendsOne:
        def it_records_it_after_the_earlier_ones(self, store: Store) -> None:
            artifact_id = _create(store, upload_nonce="n1")
            store.add_version(artifact_id, VersionDraft(merge=True, upload_nonce="n2"))
            assert store.get(artifact_id).used_upload_nonces == ("n1", "n2")

    class WhenTheNonceWasAlreadySpent:
        def it_conflicts(self, store: Store) -> None:
            artifact_id = _create(store, upload_nonce="n1")
            with pytest.raises(Conflict):
                store.add_version(artifact_id, VersionDraft(merge=True, upload_nonce="n1"))

        def it_writes_no_version(self, store: Store) -> None:
            artifact_id = _create(store, upload_nonce="n1")
            with pytest.raises(Conflict):
                store.add_version(artifact_id, VersionDraft(merge=True, upload_nonce="n1"))
            assert store.get(artifact_id).version == 1

    class WhenMoreThanFiftyAreSpent:
        def it_keeps_the_newest_fifty(self, store: Store) -> None:
            artifact_id = _create(store, upload_nonce="n0")
            for number in range(1, 51):
                store.add_version(artifact_id, VersionDraft(upload_nonce=f"n{number}"))
            assert store.get(artifact_id).used_upload_nonces[0] == "n1"


class DescribeLocate:
    class WhenThePathNamesAFile:
        def it_returns_a_readable_file(self, store: Store) -> None:
            artifact_id = _create(store)
            assert store.locate(artifact_id, ENTRY).read_bytes() == b"<h1>hi</h1>"

    class WhenAVersionIsPinned:
        def it_returns_that_version_s_file(self, store: Store) -> None:
            artifact_id = _create(store)
            store.add_version(artifact_id, VersionDraft(files={ENTRY: b"<h1>two</h1>"}))
            assert store.locate(artifact_id, ENTRY, version=1).read_bytes() == b"<h1>hi</h1>"

    class WhenThePathIsMissing:
        def it_is_not_found(self, store: Store) -> None:
            artifact_id = _create(store)
            with pytest.raises(NotFound):
                store.locate(artifact_id, "missing.html")

    class WhenThePathEscapes:
        def it_refuses_it(self, store: Store) -> None:
            artifact_id = _create(store)
            with pytest.raises(Invalid):
                store.locate(artifact_id, "../meta.json")


class DescribeTheReleaseFields:
    class WhenAnArtifactIsCreatedInAProject:
        def it_keeps_the_project(self, store: Store) -> None:
            assert store.get(_create(store, project="q3-report")).project == "q3-report"

    class WhenAnArtifactIsCreatedWithoutOne:
        def it_starts_every_release_field_at_its_default(self, store: Store) -> None:
            artifact = store.get(_create(store))
            assert (artifact.links, artifact.fallback, artifact.public_until, artifact.project) == (
                (),
                None,
                None,
                None,
            )

    class WhenEveryFieldIsSet:
        def it_round_trips_them(self, store: Store, clock: Clock) -> None:
            artifact = store.get(_create(store))
            link = LinkRecord(
                lid="l1",
                label="for Ana",
                created_at=clock.now(),
                expires_at=clock.now() + timedelta(days=1),
                passcode=Passcode(hash="aGFzaA", salt="c2FsdA", n=2**14, r=8, p=1),
            )
            full = artifact.model_copy(
                update={
                    "links": (link,),
                    "fallback": ENTRY,
                    "public_until": clock.now() + timedelta(days=2),
                    "project": "q3",
                }
            )
            store.replace_meta(full)
            assert store.get(artifact.id) == full

    class WhenAVersionMovesTheProject:
        def it_takes_the_new_one(self, store: Store) -> None:
            artifact_id = _create(store, project="q3")
            store.add_version(artifact_id, VersionDraft(project="q4"))
            assert store.get(artifact_id).project == "q4"

        def it_clears_it_on_an_empty_name(self, store: Store) -> None:
            artifact_id = _create(store, project="q3")
            store.add_version(artifact_id, VersionDraft(project=""))
            assert store.get(artifact_id).project is None

        def it_leaves_it_alone_when_none_is_named(self, store: Store) -> None:
            artifact_id = _create(store, project="q3")
            store.add_version(artifact_id, VersionDraft())
            assert store.get(artifact_id).project == "q3"


class DescribeSettingMetadata:
    class WhenTheTitleChanges:
        def it_writes_it(self, store: Store) -> None:
            artifact_id = _create(store)
            store.set_meta(artifact_id, MetaChange(title="Renamed"))
            assert store.get(artifact_id).title == "Renamed"

        def it_writes_no_version(self, store: Store) -> None:
            artifact_id = _create(store)
            store.set_meta(artifact_id, MetaChange(title="Renamed"))
            assert store.get(artifact_id).version == 1

        def it_keeps_the_link_epoch(self, store: Store) -> None:
            artifact_id = _create(store)
            store.set_meta(artifact_id, MetaChange(title="Renamed"))
            assert store.get(artifact_id).link_epoch == 0

        def it_moves_updated_at(
            self, store: Store, clock: Clock, advance: Callable[[timedelta], None]
        ) -> None:
            artifact_id = _create(store)
            advance(timedelta(minutes=5))
            store.set_meta(artifact_id, MetaChange(title="Renamed"))
            assert store.get(artifact_id).updated_at == clock.now()

        def it_hands_back_the_artifact_as_it_was(self, store: Store) -> None:
            artifact_id = _create(store)
            before, _ = store.set_meta(artifact_id, MetaChange(title="Renamed"))
            assert before.title == "Energy, September"

    class WhenNothingWouldChange:
        def it_writes_nothing(self, store: Store, advance: Callable[[timedelta], None]) -> None:
            artifact_id = _create(store)
            original = store.get(artifact_id)
            advance(timedelta(minutes=5))
            store.set_meta(artifact_id, MetaChange(title=original.title))
            assert store.get(artifact_id) == original

    class WhenTheTagsWouldPassTheLimit:
        def it_refuses_the_change(self, store: Store) -> None:
            artifact_id = _create(store, tags=tuple(f"t{index}" for index in range(TAG_LIMIT)))
            with pytest.raises(Invalid):
                store.set_meta(artifact_id, MetaChange(add_tags=("one-more",)))

    class WhenTheArtifactIsUnknown:
        def it_is_not_found(self, store: Store) -> None:
            with pytest.raises(NotFound):
                store.set_meta(new_id(), MetaChange(title="Renamed"))


class DescribeSearching:
    class WhenTheQueryHasSeveralTerms:
        def it_needs_every_one_of_them(self, store: Store) -> None:
            _create(store, title="Energy, September")
            _create(store, title="Energy, October")
            page = store.list(ArtifactQuery(query="  energy   OCT "))
            assert [item.title for item in page.items] == ["Energy, October"]

        def it_matches_each_term_anywhere(self, store: Store) -> None:
            _create(store, title="Energy", tags=("final",), project="household")
            assert len(store.list(ArtifactQuery(query="final house energy")).items) == 1

    class WhenATermNamesTheEntry:
        def it_matches_the_file_name(self, store: Store) -> None:
            _create(store, title="Chart", files={"report.html": b"x"})
            assert len(store.list(ArtifactQuery(query="report.html")).items) == 1

    class WhenFilteredByProject:
        def it_keeps_only_that_project(self, store: Store) -> None:
            inside = _create(store, project="q3")
            _create(store, project="q4")
            _create(store)
            page = store.list(ArtifactQuery(project="q3"))
            assert [item.id for item in page.items] == [inside]

    class WhenFilteredByUpdateTime:
        def it_keeps_only_the_later_ones(
            self, store: Store, clock: Clock, advance: Callable[[timedelta], None]
        ) -> None:
            _create(store)
            cutoff = clock.now()
            advance(timedelta(minutes=1))
            later = _create(store)
            page = store.list(ArtifactQuery(updated_after=cutoff))
            assert [item.id for item in page.items] == [later]

    class WhenFilteredByExpiry:
        def it_keeps_only_the_ones_expiring_soon(self, store: Store, clock: Clock) -> None:
            soon = _create(store, expires_at=clock.now() + timedelta(hours=1))
            _create(store, expires_at=clock.now() + timedelta(days=30))
            _create(store)
            page = store.list(ArtifactQuery(expiring_within=timedelta(days=1)))
            assert [item.id for item in page.items] == [soon]

    class WhenSortedByCreation:
        def it_ignores_later_updates(
            self, store: Store, advance: Callable[[timedelta], None]
        ) -> None:
            first = _create(store)
            advance(timedelta(minutes=1))
            second = _create(store)
            advance(timedelta(minutes=1))
            store.add_version(first, VersionDraft(note="later"))
            page = store.list(ArtifactQuery(sort=ListSort.CREATED))
            assert [item.id for item in page.items] == [second, first]

    class WhenSortedByTitle:
        def it_orders_them_a_to_z_ignoring_case(self, store: Store) -> None:
            for title in ("beta", "Alpha", "gamma"):
                _create(store, title=title)
            page = store.list(ArtifactQuery(sort=ListSort.TITLE))
            assert [item.title for item in page.items] == ["Alpha", "beta", "gamma"]

        def it_pages_without_repeating_a_row(self, store: Store) -> None:
            for title in ("same", "same", "same"):
                _create(store, title=title)
            first = store.list(ArtifactQuery(sort=ListSort.TITLE, limit=2))
            second = store.list(
                ArtifactQuery(sort=ListSort.TITLE, limit=2, cursor=first.next_cursor)
            )
            assert len({item.id for item in first.items + second.items}) == 3

    class WhenTheCursorBelongsToAnotherSort:
        def it_is_a_bad_request(self, store: Store) -> None:
            _create(store)
            _create(store)
            first = store.list(ArtifactQuery(limit=1))
            with pytest.raises(Invalid):
                store.list(ArtifactQuery(sort=ListSort.TITLE, cursor=first.next_cursor))

    class WhenFacetsAreAsked:
        def it_counts_projects_over_the_whole_match(self, store: Store) -> None:
            for project in ("q3", "q3", "q4"):
                _create(store, project=project)
            _create(store)
            facets = store.list(ArtifactQuery(facets=True, limit=1)).facets
            assert facets is not None
            assert facets.projects == {"q3": 2, "q4": 1}

        def it_counts_tags_over_the_whole_match(self, store: Store) -> None:
            _create(store, tags=("energy", "final"))
            _create(store, tags=("energy",))
            facets = store.list(ArtifactQuery(facets=True, limit=1)).facets
            assert facets is not None
            assert facets.tags == {"energy": 2, "final": 1}

        def it_counts_only_what_the_filters_match(self, store: Store) -> None:
            _create(store, title="Energy", project="q3")
            _create(store, title="Notes", project="q4")
            facets = store.list(ArtifactQuery(query="energy", facets=True)).facets
            assert facets is not None
            assert facets.projects == {"q3": 1}

    class WhenFacetsAreNotAsked:
        def it_leaves_them_out(self, store: Store) -> None:
            _create(store, project="q3")
            assert store.list(ArtifactQuery()).facets is None


class DescribeTheRunningUsage:
    class WhenPruningDropsAVersion:
        def it_frees_the_bytes_only_that_version_held(self, two_version_store: Store) -> None:
            artifact_id = _create(two_version_store, files={ENTRY: b"0123456789"})
            two_version_store.add_version(artifact_id, VersionDraft(files={ENTRY: b"12345"}))
            two_version_store.add_version(artifact_id, VersionDraft(files={ENTRY: b"123"}))
            two_version_store.prune(artifact_id)
            assert two_version_store.usage_bytes() == 8

        def it_keeps_the_bytes_a_later_version_shares(self, two_version_store: Store) -> None:
            artifact_id = _create(two_version_store, files={ENTRY: b"0123456789"})
            two_version_store.add_version(
                artifact_id, VersionDraft(files={"b.txt": b"12345"}, merge=True)
            )
            two_version_store.add_version(
                artifact_id, VersionDraft(files={"c.txt": b"1"}, merge=True)
            )
            two_version_store.prune(artifact_id)
            assert two_version_store.usage_bytes() == 16

    class WhenAVersionIsRolledBack:
        def it_frees_its_bytes(self, store: Store) -> None:
            artifact_id = _create(store, files={ENTRY: b"0123456789"})
            store.add_version(artifact_id, VersionDraft(files={ENTRY: b"12345"}))
            store.vitrine_version(artifact_id, 2)
            assert store.usage_bytes() == 10

    class WhenOnlyTheMetadataChanges:
        def it_stays_the_same(self, store: Store) -> None:
            artifact_id = _create(store, files={ENTRY: b"0123456789"})
            store.set_meta(artifact_id, MetaChange(title="Renamed"))
            assert store.usage_bytes() == 10
