"""How the public gate's question reaches a human on each MCP protocol era (spec § 6.1, ADR 0004).

This is the only code that awaits the client. Both branches ask the same question with the same
form and hand the raw answer to ``PublicGate.verdict``, so the eras cannot disagree on a decision.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

import anyio
import mcp.types as mt
from fastmcp import Context
from fastmcp.exceptions import McpError

from mcp_vitrine.domain.errors import NotFound
from mcp_vitrine.domain.models import ArtifactDetail, GateDecision
from mcp_vitrine.services.gate import CONSENT_SCHEMA, PublicGate

# The SEP-2322 key the retried call answers under.
CONFIRM_REQUEST = "confirm_public"
# Longer than a client's own elicitation timeout (Hermes: 300 s), so the client decides first;
# this only catches a client that never answers.
GATE_TIMEOUT = 600.0

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Approval:
    """The gate's verdict and the version it was given for, which the write must still find."""

    decision: GateDecision
    version: int


class Binding(Protocol):
    """Signs what a modern-era question was about, so only its own answer can approve it."""

    def mint(self, version: int) -> str:
        """The state carried by the question about ``version``."""
        ...

    def version_of(self, state: str) -> int:
        """The version a state was minted for; ``NotFound`` when it is not this request's."""
        ...


async def confirm(
    ctx: Context, gate: PublicGate, detail: ArtifactDetail, *, question: str, binding: Binding
) -> Approval | mt.InputRequiredResult:
    """Ask the human, or return the ``InputRequiredResult`` a ``2026-07-28`` client answers."""
    shown = detail.artifact.version
    if not gate.asks_a_human():
        return Approval(gate.without_asking(), shown)
    if not _can_elicit(ctx):
        return Approval(GateDecision.UNSUPPORTED, shown)
    if ctx._is_modern_protocol():
        return _modern(ctx, question, binding, shown)
    return Approval(await _handshake(ctx, question), shown)


def _modern(
    ctx: Context, question: str, binding: Binding, shown: int
) -> Approval | mt.InputRequiredResult:
    """``2026-07-28``: ask by returning, read the answer from the retried call.

    An answer is believed only alongside the state this server minted for this request: a
    client that sends an approval it was never asked for, or one asked about another artifact,
    another request or an older version, is declined (security T20).
    """
    answers = ctx.input_responses
    if answers is None:
        return mt.InputRequiredResult(
            input_requests={
                CONFIRM_REQUEST: mt.ElicitRequest(
                    params=mt.ElicitRequestFormParams(
                        message=question, requested_schema=CONSENT_SCHEMA
                    )
                )
            },
            request_state=binding.mint(shown),
        )
    state = ctx.request_state
    answer = answers.get(CONFIRM_REQUEST)
    if state is None or not isinstance(answer, mt.ElicitResult):
        return Approval(GateDecision.DECLINED, shown)
    try:
        version = binding.version_of(state)
    except NotFound:
        return Approval(GateDecision.DECLINED, shown)
    return Approval(PublicGate.verdict(answer.action, answer.content), version)


async def _handshake(ctx: Context, question: str) -> GateDecision:
    """``2025-11-25`` and earlier: a server-initiated ``elicitation/create``."""
    try:
        async with asyncio.timeout(GATE_TIMEOUT):
            answer = await ctx.session.elicit(
                message=question,
                requested_schema=CONSENT_SCHEMA,
                related_request_id=ctx.request_id,
            )
    except TimeoutError:
        return GateDecision.DECLINED
    except (McpError, anyio.ClosedResourceError, anyio.BrokenResourceError) as error:
        logger.warning("public gate: the client could not answer (%s)", type(error).__name__)
        return GateDecision.UNSUPPORTED
    return PublicGate.verdict(answer.action, answer.content)


def _can_elicit(ctx: Context) -> bool:
    """Whether the client declared form elicitation; one that did not cannot approve anything."""
    return ctx.session.check_client_capability(
        mt.ClientCapabilities(elicitation=mt.ElicitationCapability())
    )
