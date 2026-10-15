"""The models of spec § 2-4: ``meta.json``, the drafts and the link claims."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from mcp_drop.domain.errors import ConfigError, SchemaMismatch
from mcp_drop.domain.models import (
    TAG_LIMIT,
    Artifact,
    ArtifactDraft,
    GateDecision,
    LinkClaims,
    LinkKind,
    Scope,
    Version,
    VersionDraft,
    Visibility,
)

NOW = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
META_KEYS = {
    "schema_version",
    "id",
    "title",
    "visibility",
    "entry",
    "version",
    "versions",
    "created_at",
    "updated_at",
    "expires_at",
    "source",
    "client_ref",
    "tags",
    "link_epoch",
    "used_upload_nonces",
}


def _artifact(**changes: object) -> Artifact:
    """A minimal valid artifact, with ``changes`` applied."""
    fields: dict[str, object] = {
        "id": "k7m2q9x4tz3wbn6r",
        "title": "Energy, September",
        "entry": "index.html",
        "version": 1,
        "versions": (Version(n=1, at=NOW, bytes=10, files=1),),
        "created_at": NOW,
        "updated_at": NOW,
        "source": "hermes",
    }
    fields.update(changes)
    return Artifact.model_validate(fields)


def _claims(**changes: object) -> LinkClaims:
    """A minimal valid view claim, with ``changes`` applied."""
    fields: dict[str, object] = {
        "kind": LinkKind.VIEW,
        "id": "k7m2q9x4tz3wbn6r",
        "expires_at": LATER,
        "epoch": 0,
    }
    fields.update(changes)
    return LinkClaims.model_validate(fields)


class DescribeVisibility:
    class WhenItIsRead:
        def it_has_the_two_values_of_the_spec(self) -> None:
            assert [member.value for member in Visibility] == ["private", "public"]


class DescribeScope:
    class WhenTheTokenOmitsScopes:
        def it_grants_all_four(self) -> None:
            assert Scope.of("") == frozenset(Scope)

    class WhenTheTokenListsScopes:
        def it_grants_exactly_those(self) -> None:
            assert Scope.of("read+share") == {Scope.READ, Scope.SHARE}

    class WhenATokenNamesAnUnknownScope:
        def it_refuses_the_configuration(self) -> None:
            with pytest.raises(ConfigError):
                Scope.of("read+admin")


class DescribeGateDecision:
    class WhenTheGateApproved:
        def it_lets_the_publish_through(self) -> None:
            assert GateDecision.APPROVED.approves()

    class WhenPublicIsUnrestricted:
        def it_does_not_require_the_gate(self) -> None:
            assert GateDecision.NOT_REQUIRED.approves()

    class WhenTheHumanDeclined:
        def it_does_not_let_the_publish_through(self) -> None:
            assert not GateDecision.DECLINED.approves()

    class WhenTheClientCannotAsk:
        def it_does_not_let_the_publish_through(self) -> None:
            assert not GateDecision.UNSUPPORTED.approves()


class DescribeTheArtifact:
    class WhenItIsWrittenAndReadBack:
        def it_round_trips(self) -> None:
            artifact = _artifact(expires_at=LATER, tags=("energy",), client_ref="weekly-2026-09")
            assert Artifact.model_validate_json(artifact.model_dump_json()) == artifact

        def it_writes_exactly_the_keys_spec_4_2_documents(self) -> None:
            written = json.loads(_artifact().model_dump_json())
            assert set(written) == META_KEYS

        def it_writes_timestamps_as_utc(self) -> None:
            assert json.loads(_artifact().model_dump_json())["created_at"] == "2026-10-15T10:00:00Z"

    class WhenTheSchemaVersionIsUnknown:
        def it_refuses_to_read_it(self) -> None:
            with pytest.raises(SchemaMismatch):
                _artifact(schema_version=2)

    class WhenTheCurrentVersionIsNotTheLastOne:
        def it_refuses_the_metadata(self) -> None:
            with pytest.raises(ValidationError):
                _artifact(version=2)

    class WhenAVersionNumberIsSkipped:
        def it_refuses_the_metadata(self) -> None:
            with pytest.raises(ValidationError):
                _artifact(
                    version=3,
                    versions=(
                        Version(n=1, at=NOW, bytes=10, files=1),
                        Version(n=3, at=NOW, bytes=10, files=1),
                    ),
                )

    class WhenTheOldestVersionsWerePruned:
        def it_still_reads_the_metadata(self) -> None:
            artifact = _artifact(
                version=3,
                versions=(
                    Version(n=2, at=NOW, bytes=10, files=1),
                    Version(n=3, at=NOW, bytes=10, files=1),
                ),
            )
            assert artifact.versions[0].n == 2

    class WhenTheTagListIsAbsurd:
        def it_refuses_the_metadata(self) -> None:
            with pytest.raises(ValidationError):
                _artifact(tags=tuple(f"tag-{index}" for index in range(TAG_LIMIT + 1)))

    class WhenTheExpiryHasPassed:
        def it_reports_the_artifact_as_expired(self) -> None:
            assert _artifact(expires_at=NOW).is_expired(LATER)

    class WhenThereIsNoExpiry:
        def it_never_reports_the_artifact_as_expired(self) -> None:
            assert not _artifact().is_expired(LATER)

    class WhenItIsSummarised:
        def it_leaves_out_the_files_and_the_urls(self) -> None:
            summary = _artifact(tags=("energy",)).summary()
            assert (summary.id, summary.version, summary.tags) == (
                "k7m2q9x4tz3wbn6r",
                1,
                ("energy",),
            )


class DescribeArtifactDraft:
    class WhenItIsBuilt:
        def it_defaults_to_a_private_expiring_free_artifact(self) -> None:
            draft = ArtifactDraft(title="Report", files={"index.html": b"hi"})
            assert (draft.entry, draft.tags, draft.expires_at, draft.client_ref) == (
                None,
                (),
                None,
                None,
            )


class DescribeVersionDraft:
    class WhenAnUpdateClearsAndSetsTheExpiry:
        def it_refuses_the_contradiction(self) -> None:
            with pytest.raises(ValidationError):
                VersionDraft(expires_at=LATER, clear_expiry=True)


class DescribeLinkClaims:
    class WhenItIsAViewLink:
        def it_carries_the_epoch(self) -> None:
            assert _claims().epoch == 0

        def it_refuses_a_nonce(self) -> None:
            with pytest.raises(ValidationError):
                _claims(nonce="abc")

    class WhenItIsAnUploadLink:
        def it_carries_the_nonce(self) -> None:
            claims = _claims(kind=LinkKind.UPLOAD, epoch=None, nonce="abc")
            assert claims.nonce == "abc"

        def it_refuses_the_epoch(self) -> None:
            with pytest.raises(ValidationError):
                _claims(kind=LinkKind.UPLOAD, nonce="abc")

    class WhenItIsNeither:
        def it_refuses_the_payload(self) -> None:
            with pytest.raises(ValidationError):
                _claims(epoch=None)
