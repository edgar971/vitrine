# vitrine roadmap

`0.1.0` shipped on 2026-10-16, and `0.2.0` is complete on `main`; the release procedure is in
[CONTRIBUTING.md § Releasing](../CONTRIBUTING.md#releasing), and the per-version history is in
[CHANGELOG.md](../CHANGELOG.md).

## Shipped

- **`0.1.0`** — the M0–M5 build: `config`, `domain`, `storage`, `services`, the MCP layer, the web
  layer (`/a`, `/s`, `/api`, `/u`), the `mcp-vitrine` CLI, metrics, the container image,
  `examples/`, and the Hermes plugin.
- **`0.2.0`** — one `meta.json` schema bump with `mcp-vitrine migrate` for upgraders, projects,
  metadata edits without a new version, multi-term search and facets, per-link passcodes with
  per-link revoke, scheduled unpublish, anonymous view counts, `/api` parity and the `vitrine`
  client CLI, GFM markdown, the offline JSX renderer, zip site uploads with an SPA fallback, and the
  operator dashboard at `/_admin/`. Tracked in [#30](https://github.com/edgar971/vitrine/issues/30)
  and its milestone.

The threat model in [security.md](security.md) was reviewed against `main` once `0.2.0` had landed.
The review's fixes are in [#55](https://github.com/edgar971/vitrine/pull/55),
[#56](https://github.com/edgar971/vitrine/pull/56) and
[#57](https://github.com/edgar971/vitrine/pull/57), and § 3 records the controls they added.

## Next

Two things are deliberately not in `0.2.0`:

| Issue | What it is | Why it waits |
|---|---|---|
| [#53](https://github.com/edgar971/vitrine/issues/53) | Notifications over MCP: subscriptions on `artifact://{id}/views` and `.../history` | FastMCP 4.1.0 has no server-side `resources/subscribe` on a `LocalProvider` ([research](research.md) § 3). Waiting for it rather than working around it |
| [#54](https://github.com/edgar971/vitrine/issues/54) | Dashboard phase 2: approve a public request out of band | Phase 1 narrows access only; phase 2 amends [ADR 0002](adr/0002-one-gate-to-public.md) |

## Not planned

Each has a seam in the [design](design.md):

- a content preview in the public-gate prompt, so the human approves what they actually saw
  (security R3);
- an S3-compatible `Store`, for more than one replica;
- OAuth/OIDC auth, for multi-user deployments;
- the remaining renderers: ipynb, mermaid, diff.
