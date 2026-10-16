"""``PublicGate``: the policy half of the one path to public (spec § 6)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mcp_vitrine.domain.models import (
    Artifact,
    ArtifactDetail,
    FileEntry,
    GateDecision,
    PublicMode,
    Version,
)
from mcp_vitrine.services.gate import CONSENT_SCHEMA, PublicGate

NOW = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)


def _detail(files: int) -> ArtifactDetail:
    """An artifact titled ``Energy`` whose current version holds ``files`` files."""
    artifact = Artifact(
        id="k7m2q9x4tz3wbn6r",
        title="Energy",
        entry="index.html",
        version=1,
        versions=(Version(n=1, at=NOW, bytes=1, files=files),),
        created_at=NOW,
        updated_at=NOW,
        source="hermes",
    )
    entries = tuple(FileEntry(path=f"f{index}.html", bytes=1) for index in range(files))
    return ArtifactDetail(artifact=artifact, version=1, files=entries)


@pytest.fixture
def gate() -> PublicGate:
    """The default gate: a human confirms."""
    return PublicGate(PublicMode.CONFIRM)


class DescribePublicGate:
    class WhenTheModeIsConfirm:
        def it_asks_a_human(self, gate: PublicGate) -> None:
            assert gate.asks_a_human() is True

    class WhenTheModeIsDisabled:
        def it_does_not_ask(self) -> None:
            assert PublicGate(PublicMode.DISABLED).asks_a_human() is False

        def it_refuses(self) -> None:
            assert PublicGate(PublicMode.DISABLED).without_asking() is GateDecision.DISABLED

    class WhenTheModeIsUnrestricted:
        def it_does_not_ask(self) -> None:
            assert PublicGate(PublicMode.UNRESTRICTED).asks_a_human() is False

        def it_lets_the_change_through(self) -> None:
            assert PublicGate(PublicMode.UNRESTRICTED).without_asking() is GateDecision.NOT_REQUIRED

    class WhenTheQuestionIsWorded:
        def it_names_the_title(self, gate: PublicGate) -> None:
            assert '"Energy"' in gate.question(_detail(files=3))

        def it_counts_the_files(self, gate: PublicGate) -> None:
            assert "(3 files)" in gate.question(_detail(files=3))

        def it_counts_one_file_in_the_singular(self, gate: PublicGate) -> None:
            assert "(1 file)" in gate.question(_detail(files=1))

        def it_states_the_consequence(self, gate: PublicGate) -> None:
            assert "Anyone with the link" in gate.question(_detail(files=1))

    class WhenTheAnswerIsAnAcceptWithoutData:
        def it_approves(self) -> None:
            assert PublicGate.verdict("accept", {}) is GateDecision.APPROVED

    class WhenTheAnswerIsAnAcceptWithNoContentAtAll:
        def it_approves(self) -> None:
            assert PublicGate.verdict("accept", None) is GateDecision.APPROVED

    class WhenTheAnswerConfirms:
        def it_approves(self) -> None:
            assert PublicGate.verdict("accept", {"confirm": True}) is GateDecision.APPROVED

    class WhenTheAnswerSaysConfirmFalse:
        def it_declines(self) -> None:
            assert PublicGate.verdict("accept", {"confirm": False}) is GateDecision.DECLINED

    class WhenTheAnswerIsADecline:
        def it_declines_whatever_the_content(self) -> None:
            assert PublicGate.verdict("decline", {"confirm": True}) is GateDecision.DECLINED

    class WhenTheAnswerIsACancel:
        def it_declines(self) -> None:
            assert PublicGate.verdict("cancel", {}) is GateDecision.DECLINED

    class WhenTheConsentFormIsSent:
        def it_defaults_confirm_to_true(self) -> None:
            properties = CONSENT_SCHEMA["properties"]
            assert isinstance(properties, dict)
            assert properties["confirm"]["default"] is True

        def it_requires_nothing(self) -> None:
            assert "required" not in CONSENT_SCHEMA
