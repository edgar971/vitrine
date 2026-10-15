"""How the public gate's question reaches a human on each MCP protocol era (spec § 6.1, ADR 0004).

This is the only code that awaits the client. Both branches ask the same question with the same
form and hand the raw answer to ``PublicGate.verdict``, so the eras cannot disagree on a decision.
"""

from __future__ import annotations

import asyncio
import logging

import anyio
import mcp.types as mt
from fastmcp import Context
from fastmcp.exceptions import McpError

from mcp_drop.domain.models import ArtifactDetail, GateDecision
from mcp_drop.services.gate import CONSENT_SCHEMA, PublicGate

# The SEP-2322 key the retried call answers under.
CONFIRM_REQUEST = "confirm_public"
# Longer than a client's own elicitation timeout (Hermes: 300 s), so the client decides first;
# this only catches a client that never answers.
GATE_TIMEOUT = 600.0

logger = logging.getLogger(__name__)


async def confirm(
    ctx: Context, gate: PublicGate, detail: ArtifactDetail
) -> GateDecision | mt.InputRequiredResult:
    """Ask the human, or return the ``InputRequiredResult`` a ``2026-07-28`` client answers."""
    if not gate.asks_a_human():
        return gate.without_asking()
    if not _can_elicit(ctx):
        return GateDecision.UNSUPPORTED
    question = gate.question(detail)
    if ctx._is_modern_protocol():
        return _modern(ctx, gate, question)
    return await _handshake(ctx, gate, question)


def _modern(ctx: Context, gate: PublicGate, question: str) -> GateDecision | mt.InputRequiredResult:
    """``2026-07-28``: ask by returning, read the answer from the retried call."""
    answers = ctx.input_responses
    if answers is None:
        return mt.InputRequiredResult(
            input_requests={
                CONFIRM_REQUEST: mt.ElicitRequest(
                    params=mt.ElicitRequestFormParams(
                        message=question, requested_schema=CONSENT_SCHEMA
                    )
                )
            }
        )
    answer = answers.get(CONFIRM_REQUEST)
    if not isinstance(answer, mt.ElicitResult):
        return GateDecision.DECLINED
    return gate.verdict(answer.action, answer.content)


async def _handshake(ctx: Context, gate: PublicGate, question: str) -> GateDecision:
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
    return gate.verdict(answer.action, answer.content)


def _can_elicit(ctx: Context) -> bool:
    """Whether the client declared form elicitation; one that did not cannot approve anything."""
    return ctx.session.check_client_capability(
        mt.ClientCapabilities(elicitation=mt.ElicitationCapability())
    )
