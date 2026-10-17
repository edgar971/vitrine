# vitrine roadmap

`0.1.0` was released on 2026-10-16 ([release](https://github.com/edgar971/vitrine/releases/tag/v0.1.0)):
milestones M0–M5 below shipped in it. The release procedure lives in
[CONTRIBUTING.md § Releasing](../CONTRIBUTING.md#releasing), and the hardening planned before the
next release is tracked in [#15](https://github.com/edgar971/vitrine/issues/15).

| Milestone | Delivered |
|---|---|
| M0 bootstrap | `uv` + `just` gates, CI, pre-commit, Dependabot, this docs set, ADRs 0001–0004 |
| M1 core | `config`, `domain`, `storage.FileStore`, `services` (artifacts, links, audit, limits) |
| M2 MCP layer | hashed-token auth and scopes, tools, resources, the `share` prompt, the two-era public gate |
| M3 web layer | `/a` and `/s` views, `/api/artifacts`, `/u/{sig}`, `/healthz`, the edge middleware |
| M4 operations | the `mcp-vitrine` CLI, metrics, the container image, `examples/` |
| M5 integrations | the Hermes plugin, client snippets, the release workflow |

## The `0.1.0` release (shipped)

- [x] Every invariant in [AGENTS.md](../AGENTS.md) has a named spec (held by
      `tests/unit/invariants_spec.py`).
- [x] Coverage is at least 90% (97%), and `pip-audit` is clean.
- [x] The release image is signed and carries an SBOM (`release.yml` does both).
- [ ] The threat model in [security.md](security.md) reviewed against the shipped code.
- [x] The final tag: PyPI, the signed image, and a GitHub release.

## After 0.1

Ordered by expected value. Each item already has a seam in the [design](design.md).

The publishing features planned for 0.2 to 0.4 (viewer passcodes, JSX and zip sites, scheduled
unpublish, a dashboard, a client CLI, projects and search, view counts, notifications) are drafted
in [proposals/0.2-publishing.md](proposals/0.2-publishing.md).

1. A content preview in the public-gate prompt, so the human approves what they actually saw
   (security R3).
2. An out-of-band approval link, for clients that cannot elicit — and for the agent that holds a
   `vitrine:publish` token and can answer its own prompt (security R2).
3. An S3-compatible `Store` for more than one replica.
4. OAuth/OIDC auth for multi-user deployments.
5. More renderers (ipynb, mermaid, diff).
