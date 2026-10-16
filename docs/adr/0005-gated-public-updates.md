# 0005 — New content on a public artifact goes through the gate, bound to what the human saw

- **Status:** Accepted
- **Date:** 2026-10-15
- **Context:** [`docs/spec.md`](../spec.md) § 4.4, § 6.1, § 7.1;
  [`docs/security.md`](../security.md) § 3 (T19, T20), § 5 (R2); ADR 0002

## Context

ADR 0002 made `artifact_set_visibility` the only path to public. The pre-release threat-model
review found two ways around the spirit of it:

- **Content, not visibility.** A version added to an artifact that is already public is public
  the moment it lands. `artifact_update` needed only `vitrine:write`, and `PUT /api/artifacts/{id}`
  and upload URLs accepted the same write, so a prompt-injected agent with a write token could
  find a public artifact with `artifact_list(visibility="public")` and put private data on it,
  while being told the result was private.
- **An answer is not bound to its question.** On `2026-07-28` the approval arrives on a later
  call. The server believed any `confirm_public` answer, even one it never asked for, and the
  version could change while the human was deciding, so content they never saw went public
  under their approval.

The alternatives were to make any update to a public artifact flip it back to private (simple,
but it silently breaks every public URL), or to accept the risk and document it.

## Decision

- Adding a version to a public artifact needs an approving gate decision. Over MCP,
  `artifact_update` asks the human when the token holds `vitrine:publish`; without that scope it
  is refused unasked. HTTP routes cannot ask a human, so they refuse with 403. Every refusal is
  audited as `publish_refused`.
- The store checks visibility and the expected version under the artifact's lock, in the same
  step as the write, so neither a concurrent publish nor a concurrent version slips between the
  check and the write.
- On `2026-07-28`, the question carries a `requestState` signed (with the link secret, as kind
  `gate`) over the artifact, the version shown, the client and a digest of the call's arguments.
  An answer without that state, or with another request's, is declined.
- On both eras, the write must find the version the human was shown; otherwise it is a
  `conflict` and nothing becomes public.

## Consequences

- `artifact_update` can now return an `InputRequiredResult` and waits up to the gate timeout,
  like `artifact_set_visibility`.
- A write-only client can keep revising private artifacts, but cannot touch public ones; a user
  who wants an agent to maintain a public page grants `vitrine:publish` and answers each update.
- The binding closes forged and stale answers, not a client that answers its own questions:
  elicitation is answered by the client, so a token holder that speaks MCP directly can still
  approve its own publish (security R2). The out-of-band approval link on the roadmap is the
  fix for that.
