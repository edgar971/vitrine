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

## Next release

Everything planned after `0.1.0` ships in one release, **`0.2.0`**. Its scope, its order and what
blocks what live in the [milestone](https://github.com/edgar971/vitrine/milestones) and in
[#30](https://github.com/edgar971/vitrine/issues/30). The work is cut into four pull requests, one
issue each:

- **`meta.json` schema 2, projects, metadata edits and search.** One `SCHEMA_VERSION = 2` bump and
  one `mcp-vitrine migrate` for upgraders, covering every field the release needs.
- **Per-link passcodes, per-link revoke, scheduled unpublish and view counts.** A link becomes a
  record, so it can carry a passcode, be revoked on its own and be counted.
- **`/api` parity, the `vitrine` client CLI, markdown, JSX and zip sites.** Everything a shell, a
  browser or a CI job needs that MCP alone does not give them.
- **The web dashboard and MCP notifications.** Phase 1 of the dashboard only narrows.

Each pull request merges to `main` as it is green; the release is cut once the milestone is empty.
`0.2.0` also closes the threat-model review left unticked above, and it is the release that moves
the new threats into [security.md](security.md) § 3.

Unscheduled, each with a seam in the [design](design.md): phase 2 of the dashboard (out-of-band
approvals, for clients that cannot elicit), a content preview in the public-gate prompt so the human
approves what they actually saw (security R3), an S3-compatible `Store` for more than one replica,
OAuth/OIDC auth for multi-user deployments, and the remaining renderers (ipynb, mermaid, diff).
