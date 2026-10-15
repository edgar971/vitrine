---
paths:
  - "tests/**"
---

# Test gotchas

- Files are `*_spec.py`; classes `Describe<Unit>` with nested `When<Situation>`; functions `it_*`.
  pytest is configured to collect only those, so a `test_*` function silently never runs.
- One observable outcome per `it_*`; no branching or loops in a test.
- Givens are fixtures in the nearest `conftest.py`; outer-class fixtures reach nested classes.
- MCP tests use FastMCP's in-memory `Client`; HTTP tests run real uvicorn on a free port;
  sandbox isolation tests use the `browser` marker (Playwright Chromium).
- Assert error messages do not reveal whether a private artifact exists.
