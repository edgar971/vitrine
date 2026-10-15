---
paths:
  - "src/mcp_drop/web/**"
  - "src/mcp_drop/app.py"
---

# Web layer gotchas

- A new route goes in `WebRoutes.routes()` **and** `ROUTE_ACCESS`; `tests/http/routes_spec.py`
  fails on a route that is not classified, and probes each one without credentials.
- Never word an error in a route: raise or let a `DropError` reach `problem()`, the one
  status mapping. A view maps a malformed path to the same 404 as a missing artifact.
- Artifact bytes leave only through `artifact_file()` or `rendered()`, which set the security
  headers and the sandbox CSP. Never add `allow-same-origin`.
- Check auth, scope and the rate bucket before reading a body, so a refused call does no work.
- The service layer is blocking filesystem code: call it through `_run` (a worker thread).
- Middleware is plain ASGI (never `BaseHTTPMiddleware`), so bodies stream to the limit check.
- uvicorn must run with `access_log=False, proxy_headers=False`: drop writes the redacted access
  log and applies `forwarded_allow_ips` itself.
