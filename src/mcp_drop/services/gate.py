"""``PublicGate``: the policy half of the one path to public (spec § 6, ADR 0002).

The protocol half (how a question reaches the human on each MCP era) lives in ``mcp/gate.py``,
because it needs the client; everything that decides lives here, where it is tested without one.
"""

from __future__ import annotations

from collections.abc import Mapping

from mcp_drop.domain.models import ArtifactDetail, GateDecision, PublicMode

ACCEPT = "accept"
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

    @staticmethod
    def verdict(action: str, content: Mapping[str, object] | None) -> GateDecision:
        """Decide on the action; only an explicit ``confirm: false`` turns an accept down."""
        if action == ACCEPT and (content or {}).get(CONFIRM_FIELD) is not False:
            return GateDecision.APPROVED
        return GateDecision.DECLINED
