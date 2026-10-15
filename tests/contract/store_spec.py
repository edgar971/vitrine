"""Every ``Store`` implementation must pass this suite (spec § 4, ``docs/design.md`` § 5)."""

from __future__ import annotations

import base64
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from mcp_drop.domain.clock import Clock
from mcp_drop.domain.errors import Conflict, Invalid, NotFound, OverQuota, Refused, TooLarge
from mcp_drop.domain.ids import new_id
from mcp_drop.domain.models import ArtifactDraft, ArtifactQuery, VersionDraft, Visibility
from mcp_drop.storage.base import Store

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

    class WhenTheCursorCarriesATimeWithoutAZone:
        def it_is_a_bad_request(self, store: Store) -> None:
            _create(store)
            cursor = base64.urlsafe_b64encode(b"2020-01-01T00:00:00|x").decode().rstrip("=")
            with pytest.raises(Invalid):
                store.list(ArtifactQuery(cursor=cursor))


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
            store.drop_version(artifact_id, 2)
            store.replace_meta(original)
            assert store.get(artifact_id).version == 1

        def it_can_restore_the_visibility(self, store: Store) -> None:
            artifact_id = _create(store)
            original = store.get(artifact_id)
            store.set_visibility(artifact_id, Visibility.PUBLIC)
            store.replace_meta(original)
            assert store.get(artifact_id).visibility is Visibility.PRIVATE


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
