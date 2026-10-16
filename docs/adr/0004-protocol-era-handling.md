# 0004 — Both MCP protocol eras are implemented, behind one gate

- **Status:** Accepted
- **Date:** 2026-10-15
- **Context:** [`docs/spec.md`](../spec.md) § 6.1; [`docs/design.md`](../design.md) § 4, § 12;
  [`docs/research.md`](../research.md) § 3–4 (F1, F2), § 5

## Context

The public gate needs a human answer, and the two protocol eras disagree about how a server asks
for one. On the handshake-era protocol (`2025-11-25`, what Hermes and most deployed clients
negotiate, measured against FastMCP 4.1) the server calls `ctx.elicit()`. On `2026-07-28`
server-initiated elicitation is gone: the tool returns `InputRequiredResult` and reads the answer
from `ctx.input_responses` on the retried call.

The era is only exposed through the private `Context._is_modern_protocol()`. Supporting one era
would break the other, and "public goes live" is not a feature that can silently stop working — a
decline must stay a decline, and a failure must not look like an approval.

## Decision

- Both branches ship, in `PublicGate`, and each has its own specs: accept, decline, approve-only
  client, no support.
- The answer is decided on the elicitation **action**, not on the returned content: clients accept
  with empty `{}` data, and an explicit `confirm: false` is a decline. The consent model therefore
  has one defaulted boolean field.
- `fastmcp` is pinned to a minor range (`>=4.1,<4.2`) and the per-era specs fail CI if the era
  handling changes underneath us.
- The gate is the only asynchronous tool that awaits the client, and it is annotated accordingly.

## Consequences

- A single gate code path, tested for each era, is the invariant; the era branch is not allowed to
  leak into tools.
- Depending on a private FastMCP method is a known, accepted risk
  ([`docs/security.md`](../security.md) § 5, R6). The mitigation is the pin plus tests that fail
  first, and a FastMCP upgrade is a review item, not a routine bump.
- When clients move to `2026-07-28`, nothing changes for them; when FastMCP changes its internals,
  the era specs break before a deployment does.
