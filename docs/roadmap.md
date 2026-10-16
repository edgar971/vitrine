# drop roadmap

`0.1.0` is built and about to be released: everything below M0–M5 is merged, `just check` is
green on Python 3.15, and the release procedure lives in
[CONTRIBUTING.md § Releasing](../CONTRIBUTING.md).

| Milestone | Delivered |
|---|---|
| M0 bootstrap | `uv` + `just` gates, CI, pre-commit, Dependabot, this docs set, ADRs 0001–0004 |
| M1 core | `config`, `domain`, `storage.FileStore`, `services` (artifacts, links, audit, limits) |
| M2 MCP layer | hashed-token auth and scopes, tools, resources, the `share` prompt, the two-era public gate |
| M3 web layer | `/a` and `/s` views, `/api/artifacts`, `/u/{sig}`, `/healthz`, the edge middleware |
| M4 operations | the `mcp-drop` CLI, metrics, the container image, `examples/` |
| M5 integrations | the Hermes plugin, client snippets, the release workflow |

## The `0.1.0` release

- [ ] Every invariant in [AGENTS.md](../AGENTS.md) has a named spec (held by
      `tests/unit/invariants_spec.py`).
- [ ] Coverage is at least 90%, `pip-audit` is clean, the image is signed with an SBOM.
- [ ] A rehearsal pre-release on TestPyPI, installed and checked (CONTRIBUTING.md § Releasing).
- [ ] The threat model in [security.md](security.md) reviewed against the shipped code.
- [ ] The final tag: PyPI, the signed image, and a GitHub release.

## After 0.1

Ordered by expected value. Each item already has a seam in the [design](design.md).

1. A content preview in the public-gate prompt, so the human approves what they actually saw
   (security R3).
2. An out-of-band approval link, for clients that cannot elicit — and for the agent that holds a
   `drop:publish` token and can answer its own prompt (security R2).
3. An S3-compatible `Store` for more than one replica.
4. OAuth/OIDC auth for multi-user deployments.
5. More renderers (ipynb, mermaid, diff).
