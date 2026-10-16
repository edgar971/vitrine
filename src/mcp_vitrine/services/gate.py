"""``PublicGate``: the policy half of the one path to public (spec § 6, ADR 0002).

The protocol half (how a question reaches the human on each MCP era) lives in ``mcp/gate.py``,
because it needs the client; everything that decides lives here, where it is tested without one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from mcp_vitrine.domain.models import ArtifactDetail, GateDecision, PublicMode
from mcp_vitrine.services.links import LinkService

ACCEPT = "accept"
# How long a question's state stays answerable: past the MCP layer's own wait for the human.
GATE_STATE_TTL = timedelta(minutes=15)
CONFIRM_FIELD = "confirm"

# One boolean that defaults to true: approve/deny-only clients answer ``accept`` with ``{}``, and
# a required field would turn every such approval into a validation failure (research F2).
CONSENT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        CONFIRM_FIELD: {
            "type": "boolean",
            "title": "Make public",
            "description": "Anyone with the link will be able to view it.",
            "default": True,
        }
    },
}


class PublicGate:
    """Decides whether ``artifact_set_visibility(public)`` may proceed, failing closed."""

    def __init__(self, mode: PublicMode) -> None:
        """Hold ``public_mode``, the only setting the gate reads (spec § 6)."""
        self._mode = mode

    def asks_a_human(self) -> bool:
        """Whether the decision needs a person's answer (``confirm`` mode)."""
        return self._mode is PublicMode.CONFIRM

    def without_asking(self) -> GateDecision:
        """The verdict of a mode that never asks: ``disabled`` refuses, ``unrestricted`` passes."""
        if self._mode is PublicMode.UNRESTRICTED:
            return GateDecision.NOT_REQUIRED
        return GateDecision.DISABLED

    def question(self, detail: ArtifactDetail) -> str:
        """What the human reads: the title, the file count and the consequence (spec § 6.1)."""
        count = len(detail.files)
        files = "1 file" if count == 1 else f"{count} files"
        return (
            f'Make "{detail.artifact.title}" ({files}) PUBLIC? '
            "Anyone with the link will be able to view it, and existing private links stop working."
        )

    def update_question(self, detail: ArtifactDetail, *, files: int | None) -> str:
        """What the human reads before new content lands on a public artifact (security T19)."""
        change = "its metadata" if files is None else ("1 file" if files == 1 else f"{files} files")
        return (
            f'"{detail.artifact.title}" is PUBLIC. Publish a new version with {change}? '
            "Anyone with the link will see it immediately."
        )

    @staticmethod
    def verdict(action: str, content: Mapping[str, object] | None) -> GateDecision:
        """Decide on the action; only an explicit ``confirm: false`` turns an accept down."""
        if action == ACCEPT and (content or {}).get(CONFIRM_FIELD) is not False:
            return GateDecision.APPROVED
        return GateDecision.DECLINED


@dataclass(frozen=True, slots=True)
class GateBinding:
    """Ties a gate question to its answer: the artifact, the client and the exact request.

    A modern-era answer arrives on a later call, so without this a client could approve a
    question it was never asked, or answer one and send different content (security T20).
    """

    links: LinkService
    artifact_id: str
    client: str
    request: str

    def mint(self, version: int) -> str:
        """The state the question about ``version`` carries."""
        return self.links.gate_state(
            self.artifact_id,
            version=version,
            client=self.client,
            digest=self._digest(),
            ttl=GATE_STATE_TTL,
        )

    def version_of(self, state: str) -> int:
        """The version ``state`` was minted for, or ``NotFound`` when it is not this request's."""
        return self.links.verify_gate_state(
            state, artifact_id=self.artifact_id, client=self.client, digest=self._digest()
        )

    def _digest(self) -> str:
        """A fixed-length stand-in for the request, which may be large."""
        return hashlib.sha256(self.request.encode()).hexdigest()
