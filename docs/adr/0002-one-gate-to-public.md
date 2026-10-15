# 0002 — `PublicGate` is the only path to public, and it fails closed

- **Status:** Accepted
- **Date:** 2026-10-15
- **Context:** [`docs/design.md`](../design.md) § 1, § 4, § 6; [`docs/spec.md`](../spec.md) § 6.1;
  [`docs/security.md`](../security.md) § 3 (T1)

## Context

The one change that cannot be taken back is making an artifact readable by anyone with the link.
The threat is not a malicious user but a prompt-injected or careless agent: content that came from
a web page or a file can steer the model into publishing something private. A token proves which
client is calling, not that a human wanted the action.

The tempting designs are a `visibility` parameter on create (fewer round trips), an HTTP route that
publishes, or a "public unless told otherwise" default. Each of them puts the decision where no
human sees it.

## Decision

- Everything is created **private**. No create tool and no upload route accepts a `visibility`.
- `artifact_set_visibility` is the only path to public, and it goes through `PublicGate`, which
  asks the human with MCP elicitation and fails closed on decline, timeout, error, a client that
  cannot elicit, and unattended contexts such as cron.
- On approval the artifact's visibility and link epoch change together, so every existing private
  link is revoked as it goes public.
- The gate is one code path, implemented for both protocol eras (ADR 0004), and the tools that can
  reach it need the `drop:publish` scope — a client that never publishes simply does not have it.

## Consequences

- Clients without elicitation cannot publish at all until the out-of-band approval link exists
  ([`docs/roadmap.md`](../roadmap.md), after 0.1).
- The gate is the place to review, test and instrument; a second path to public is a bug, and the
  invariants in [`AGENTS.md`](../../AGENTS.md) say so.
- `artifact_set_visibility` is annotated open-world in the tool catalogue, because the effect leaves
  the system.
- The spec tests both eras for accept, decline, approve-only clients and no support.
