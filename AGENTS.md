# AGENTS.md

Guide for coding agents and contributors working on **drop** (`mcp-drop`). Humans start at
`README.md`. The contract is `docs/spec.md`; how it is built is `docs/design.md`; the threat model
is `docs/security.md`. When this file and the spec disagree, the spec wins: fix this file.

## What drop is

A self-hosted MCP server that turns agent output into **private** (signed, expiring, revocable)
or **public** links. Status: M1–M5 done (the core, the MCP layer, the web layer assembled with
`/mcp` by `mcp_drop.app.build_app`, the `mcp-drop` CLI, metrics, the image, `examples/` and the
Hermes plugin); `v0.1.0` is released by pushing a `v*` tag (`.github/workflows/release.yml`).

## Commands

Only `uv` and `just` are required. `just check` runs every gate CI runs, and it is green.

| Task | Command |
|---|---|
| Install | `uv sync` |
| Every gate CI runs (**before every commit**) | `just check` |
| One test | `uv run pytest -k <name> --no-cov` |
| Serve locally | `uv run mcp-drop serve` |

`just` alone lists the recipes (`just lint`, `just types`, `just layers`, `just test`,
`just test-browser`, `just audit`, `just docs`, `just examples`, `just image-test`). The last two
need Docker; CI runs them as their own jobs. After changing a `Settings` field, run `just docs`:
`docs/configuration.md` is generated, and a spec fails while it is stale. CI runs the same recipes, so the two cannot drift.
`just test-browser` needs Chromium once: `uv run playwright install chromium`.

## Layout

`src/mcp_drop/{domain,storage,services,mcp,web}` (`docs/design.md` § 3). Dependencies point one
way: `mcp`/`web` → `services` → `storage`/`domain`. `services`, `storage` and `domain` never
import FastMCP or Starlette, so the behaviour is testable without a server; import-linter
enforces it.

`integrations/hermes/drop/` is the Hermes plugin. It is not part of the package: it runs on
Hermes's interpreter (Python 3.11+), so it uses the standard library only, imports itself
relatively, and has its own ruff target (`integrations/hermes/ruff.toml`). The specs import it as
`drop` and drive a real server through it.

## Conventions

- MCP tools follow `docs/design.md` § 4: one `LocalProvider` per feature, all four
  `ToolAnnotations` hints, `Annotated[T, Field(...)]` parameters, `Depends`/`CurrentAccessToken`
  injection and a per-tool scope. Each rule exists because a client or the model reads it.
- Domain errors are typed and mapped once per edge (MCP: `@domain_errors`; HTTP:
  `web/responses.problem`), because a hand-written message in a tool or route is how "this
  private artifact exists" leaks.
- Every HTTP route is classified in `web/routes.ROUTE_ACCESS`; the route-table spec fails on one
  that is not.
- Tests are specs: `*_spec.py`, `Describe*`/`When*` classes, one outcome per `it_*`
  (`docs/design.md` § 9). The names are the documentation of behaviour.
- Comments say *why*, never *what*. A tool's docstring is its description for the model, so keep
  it accurate and short.
- Conventional Commits; `CHANGELOG.md` (Keep a Changelog) once the package exists.

## Invariants (never break; each has a test in `docs/security.md` § 6)

- **Private by default.** Nothing becomes public without the gate.
- **One path to public:** `PublicGate`. It fails closed on decline, timeout, error or no client
  support.
- **Every HTTP route authenticates itself.** A reverse proxy is optional, never relied on.
- **Signed link segments and tokens are never logged.**
- **Every state change is audited**, and the audit log never holds artifact content.
- **No `allow-same-origin`** in the sandbox CSP for agent-written HTML.

Do not weaken a check or widen a default to make a test pass. Fix the test or ask.

## Definition of done

1. `just check` is green.
2. New behaviour has an `it_*` that would fail without it; security changes also cover the
   rejection path.
3. If the contract changed: `docs/spec.md` (and `docs/security.md` for a new threat or control)
   updated in the same change.
4. A decision that is expensive to reverse gets an ADR in `docs/adr/`.
