# Changelog

All notable changes to this project are documented here. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Nothing has been released yet: `0.1.0` is the first release, and
[`docs/roadmap.md`](docs/roadmap.md) is the plan that gets there.

## [Unreleased]

### Added

- M0 — repository bootstrap: `pyproject.toml` and `uv.lock` on Python 3.15, ruff, mypy `--strict`
  with the pydantic plugin, import-linter layering contracts, pytest with the BDD naming and a 90%
  coverage gate, and the `justfile` (`just check` runs every gate).
- M0 — CI on every push and pull request (lock check, ruff, mypy, import-linter, pytest with
  coverage, `pip-audit`), pre-commit with the same gates, and Dependabot for uv and GitHub Actions.
- M0 — `LICENSE` (MIT), `CONTRIBUTING.md`, `SECURITY.md`, this changelog, and the first four ADRs
  in [`docs/adr/`](docs/adr/).
- The package skeleton `src/mcp_drop/` (a version and an entry point that reports the build has not
  started) and the spec that covers it.
- M1 — `domain`: the models that mirror `meta.json`, the typed errors, 80-bit Crockford ids, the
  one path rule and the `data_dir` layout.
- M1 — `storage`: the `Store` protocol and `FileStore` (write protocol, retention, quota, path
  rules, sweeps), plus the contract suite every `Store` implementation must pass.
- M1 — `config`: flags, `DROP_*` variables and a TOML file, with secrets accepted from the
  environment only and unknown keys refused.
- M1 — `services`: `LinkService` (view and upload links), `ArtifactService` (including idempotent
  create), `AuditLog` (appended and fsync'd before a change is acknowledged) and `RateLimiter`.
- M1 — the layering contract now names `domain` below `storage`, which is what it has to be:
  storage cannot be written without the models it stores.
- M2 — the MCP layer: `HashedTokenVerifier` (SHA-256 hashes, every entry compared, scopes per
  token); the nine `artifact_*` tools on one `LocalProvider`, each with a title, all four
  annotation hints, a per-tool scope and described, bounded parameters; the `artifact://{id}`,
  `artifact://{id}/{path*}` and `artifact://{id}/history` resources; the `share` prompt; and the
  server instructions of spec § 5.4, asserted against the spec text.
- M2 — `PublicGate`, the one path to public, for both protocol eras: `InputRequiredResult` on
  `2026-07-28`, `elicitation/create` on handshake-era connections, one verdict for both. It fails
  closed on decline, cancel, `confirm: false`, a client without elicitation, a transport error and
  a client that never answers, and every refusal is audited as `publish_refused`.
- M2 — the MCP edge: a scope refusal is recorded as `access_denied`, the per-client buckets are
  spent before any work, and a domain error reaches the client as its own indistinguishable message.
- `ArtifactService` gains `link`, `upload_url` (the id is pre-allocated and the issue audited),
  `read_text` (text types up to 1 MB) and `history`; `Services` wires everything from settings.
- M3 — the web layer on Starlette: `/a/{id}` and `/s/{sig}` views (version pins, `Range`, `HEAD`,
  the extension allowlist, the sandbox CSP without `allow-same-origin`, `nosniff`, `no-referrer`,
  `noindex`, per-prefix caching), the Markdown, CSV/TSV and JSON renderers, single-use
  `PUT /u/{sig}` uploads, the multipart `/api/artifacts` API with per-scope bearer auth, and
  `/healthz`.
- M3 — `mcp_drop.app.build_app`: `/mcp` and the web routes in one app behind one middleware stack
  (trusted proxy headers, a redacting access log, the Host/Origin guard and streaming body
  limits), plus the per-IP view bucket with `Retry-After`.
- M3 — specs over real uvicorn for every ported spike check (headers, traversal, limits,
  rebinding), the route-table spec, log redaction, and a Playwright Chromium spec for the sandbox.
- M4 — the `mcp-drop` CLI (spec § 9): `serve`, `config check`, `token new`/`hash`, `secret new`,
  `sweep`, `audit`, `backup` and `migrate`, with exit status 0, 1 (failed) or 2 (misconfigured).
- M4 — `serve` refuses data with an older or newer `schema_version`, sweeps once before the first
  request and then every `sweep_interval` (a failed sweep is logged and retried, never fatal), and
  logs in `text` or `json`.
- M4 — `backup`: an online, lock-free, restorable copy of `data_dir` (operations § 2), with a spec
  that backs up under concurrent writers and prunes and restores every listed version.
- M4 — `migrate`: forward-only, idempotent `meta.json` upgrades through a step registry, each file
  staged and renamed, with `--dry-run` and one `migrated` audit event per run.
- M4 — optional Prometheus metrics on `metrics_port` (the `metrics` extra): the counters and
  gauges of spec § 11.2, labelled by client, route, outcome and reason, never by id or title.
- `TokenBook`: the hashed-token check, shared by `/mcp` and `/api`.
- `Store.locate`, so a view streams a file instead of reading it into memory.

### Changed

- The staging sweep takes a grace period: a sweep beside a running server (the periodic one and
  `mcp-drop sweep`) spares staging and unreferenced versions younger than 10 minutes, which a live
  write is still placing. Only the startup sweep clears everything.

- An upload URL now signs the client that asked for it (`"upload"|id|expires|nonce|client`), so
  an upload is attributed to, and rate-limited as, that client.
- An upload's nonce is spent in the same write as its version, so two `PUT`s of one URL land at
  most once; an upload never re-creates an id that was deleted or expired.
- `markdown-it-py` is a declared dependency (it was only present through FastMCP).

### Fixed

- A write hung forever when `data_dir` was relative (the default `./data`) or reached through a
  symlink: the staging walk compared a resolved path with an unresolved one.
- An `access_denied` audit event could not be built, because `tool` and `route` were not among the
  fields the event allows.
- An update without files failed as an empty version; the current files now carry over (spec § 5.1).
