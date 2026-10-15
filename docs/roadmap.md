# drop roadmap

From an empty repository to `v0.1.0`. Each milestone ends green on `just check`. Each one is
small enough for one or two PRs, and each "Done when" is a test, not a judgement.

```
 M0 bootstrap ──► M1 core ──► M2 MCP ─────────┐
                     │                        ├──► M5 integrations ──► v0.1.0
                     └──────► M3 web ─────────┤
                                              │
                    M4 operations ────────────┘   (M4 can start after M1)
```

## M0: repository bootstrap

**Deliverables**

- `pyproject.toml` and `uv.lock`, with ruff, mypy, import-linter and pytest configured
  ([design § 9–10](design.md)).
- `AGENTS.md`, `CLAUDE.md` and `.claude/rules/`.
- `README.md`, `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`.
- The CI workflow, pre-commit, Dependabot and the `justfile`.
- `docs/` (this design set) and `docs/adr/0001`–`0004`.

**Done when:** CI is green on an empty package, and `just check` runs every gate.

## M1: core (no FastMCP, no Starlette)

**Deliverables**

- `config` (env + TOML, secrets env-only).
- `domain` models and errors.
- `storage.FileStore`: write protocol, retention, quota, path rules, sweeps.
- `services`:
  - `LinkService`;
  - `ArtifactService`, including idempotent create (spec § 4.5);
  - `AuditLog`, append and query (spec § 11.3);
  - `RateLimiter` buckets (spec § 7.5).

**Done when:**

- the unit specs cover every rule in spec § 3–4;
- the `Store` contract suite passes;
- a killed-mid-write test leaves no visible partial version;
- a config file containing a secret is rejected.

## M2: MCP layer

**Deliverables**

- `HashedTokenVerifier` with scopes.
- `LocalProvider`s for tools, resources (including `artifact://{id}/history`) and prompts (`share`).
- The server `instructions` text (spec § 5.4).
- `PublicGate` for both protocol eras.

**Done when:**

- every tool's schema, title, annotations and scope match the design § 4 catalogue (asserted from
  `list_tools`);
- the gate specs pass for both eras, approve-only clients, decline and no support;
- a scoped token can neither see nor call tools outside its scopes;
- every state change writes exactly one audit event.

## M3: web layer

**Deliverables**

- The `/a` and `/s` views with the response policy and renderers.
- `/api/artifacts`, `/u/{sig}` and `/healthz`.
- Middleware: Host/Origin guard, streaming body limits, redacting access log, view rate limit.

**Done when:**

- every spike check is ported (headers, traversal, limits, rebinding, Chromium sandbox);
- the route-table test classifies every route;
- no signed segment appears in captured logs;
- `429` comes with `Retry-After`.

## M4: operations

**Deliverables**

- The CLI: `serve`, `config check`, `token`, `secret`, `sweep`, `audit`, `backup`, `migrate`.
- Optional metrics.
- The Dockerfile (multi-arch, non-root, read-only root filesystem).
- `examples/` (TOML, Compose with Caddy, Kubernetes).
- `docs/operations.md` and a generated `docs/configuration.md`.

**Done when:**

- `mcp-drop backup` taken during concurrent writes restores to a valid `data_dir`;
- `migrate --dry-run` is covered by a fixture with an old schema;
- the image runs with `--read-only` and passes `/healthz`;
- the Kubernetes example passes kubeconform.

## M5: integrations

**Deliverables**

- `integrations/hermes/drop/`: plugin, skill and commands.
- Claude Code and generic MCP client snippets in the README.

**Done when:**

- `hermes plugins validate` passes in CI;
- an end-to-end spec drives a real server through the plugin's multipart upload;
- the README quick start works from a clean machine with `uvx`.

## v0.1.0 release

**Release checklist**

- Every invariant in `AGENTS.md` has a named `it_`.
- Coverage is at least 90%.
- `pip-audit` is clean.
- The image is signed and has an SBOM.
- The PyPI release is published by trusted publishing.
- `CHANGELOG.md` has a `0.1.0` section.
- A threat-model review against `docs/security.md` § 6.

## After 0.1

Ordered by expected value. Each item already has a seam in the design.

1. Content preview in the public-gate prompt (security R3).
2. Out-of-band approval link for clients without elicitation.
3. An S3-compatible `Store` for more than one replica.
4. OAuth/OIDC auth for multi-user deployments.
5. More renderers (ipynb, mermaid, diff).
