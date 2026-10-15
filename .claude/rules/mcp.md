---
paths:
  - "src/mcp_drop/mcp/**"
---

# MCP layer gotchas

- Set all four `ToolAnnotations` hints explicitly. FastMCP's defaults say destructive and
  open-world, which is wrong for most drop tools and makes clients prompt needlessly.
- A parameter injected with `Depends(...)` or `CurrentAccessToken()` is hidden from the schema.
  Anything else is visible to the model, so it needs a `Field(description=...)` and limits.
- `PublicGate` is the only tool code that awaits the client. On `2026-07-28` connections it
  returns `InputRequiredResult`; on handshake-era connections it calls `ctx.elicit()`. Both
  branches have tests per era; if FastMCP changes how the era is exposed, those tests fail first.
- Decide on the elicitation `action`, not on its content: some clients approve with empty data.
  An explicit `confirm: false` declines.
- Tools stay thin: translate arguments, call a service, return a pydantic model. No try/except
  here; errors map to `ToolError` in one place.
