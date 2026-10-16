---
paths:
  - "src/mcp_vitrine/mcp/**"
---

# MCP layer gotchas

- Set all four `ToolAnnotations` hints explicitly. FastMCP's defaults say destructive and
  open-world, which is wrong for most vitrine tools and makes clients prompt needlessly.
- A parameter injected with `Depends(...)` or `CurrentAccessToken()` is hidden from the schema.
  Anything else is visible to the model, so it needs a `Field(description=...)` and limits.
- `mcp/gate.py` is the only code that awaits the client. On `2026-07-28` connections it returns
  `InputRequiredResult`; on handshake-era connections it sends `ctx.session.elicit()` (raw, so
  the answer is never validated into a model). Both branches hand `action`/`content` to
  `PublicGate.verdict`; `tests/mcp/gate_spec.py` runs every case on both eras.
- Decide on the elicitation `action`, not on its content: some clients approve with empty data.
  An explicit `confirm: false` declines.
- Tools stay thin: translate arguments, call a service, return a pydantic model. No try/except
  here: `@domain_errors(ToolError)` (or `ResourceError`) sits under every decorator. It is a
  wrapper, not middleware, because FastMCP masks an unknown exception before middleware sees it.
- A tool's scope and rate bucket are its `tags=tags(Scope…, Bucket…)`; the edge middleware reads
  them to audit refusals and spend the bucket. `auth=require_scopes(...)` must name the same scope.
