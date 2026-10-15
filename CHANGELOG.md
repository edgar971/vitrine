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
- M4 — the container image: a uv-built CPython 3.15 on `debian:trixie-slim`, uid 10001, data on
  `/data`, fine with `--read-only` and `--cap-drop ALL`, a `HEALTHCHECK` that sends an allowed
  `Host`, and an optional `ca_bundle` build secret for builds behind a TLS-intercepting proxy.
  CI builds amd64 and arm64 and smoke-tests the image read-only against `/healthz`.
- M4 — `examples/`: `drop.toml`, Compose with Caddy, and Kubernetes manifests (Pod Security
  `restricted`, one `Recreate` replica, RWO volume, probes with a `Host` header, default-deny
  network policy without egress), validated in CI with kubeconform, `docker compose config` and
  `caddy adapt`; a spec loads the example configs with the server's own rules.
- M4 — `docs/configuration.md`, generated from `Settings` (`just docs`); every setting now
  carries its group and description, and a spec fails when the committed file is stale.
- M5 — the Hermes Agent plugin (`integrations/hermes/drop/`): `artifact_publish_files` streams
  files or a folder to `/api/artifacts` as a private artifact (or a new version, with `merge`),
  the `drop:drop` skill, and the `/share` and `/artifacts` commands. Standard library only, for
  Python 3.11+; a spec drives a real server through it, and CI runs `hermes plugins validate`.
- M5 — the README quick start with `uvx`, and connection snippets for Claude Code, Hermes Agent
  and other MCP clients.
- The release workflow: on a `v*` tag, every CI job, then PyPI by trusted publishing, the
  multi-arch image on GHCR with an SBOM and a keyless cosign signature, and a GitHub release from
  this changelog. `ci.yml` is reusable for it.
- `docs/security.md` § 6 names the specs that hold each `AGENTS.md` invariant, and a spec fails
  when one of them is renamed or removed.
- `TokenBook`: the hashed-token check, shared by `/mcp` and `/api`.
- `Store.locate`, so a view streams a file instead of reading it into memory.
- The release process: `just release <version>` bumps every copy of the version, re-locks and
  writes the changelog section; pre-release tags (`v0.2.0rc1`) publish to TestPyPI and a GitHub
  pre-release, so a release can be rehearsed end to end. After `0.1.0`, the changelog is generated
  from Conventional Commits by git-cliff, which a new `commits` CI job enforces.
- `just wheel-test`, in CI: the built wheel, installed alone, serves `/api` and `/mcp`.

### Security

- Adding a version to a **public** artifact now goes through the public gate (ADR 0005, security
  T19). `artifact_update` asks the human when the token holds `drop:publish` and is refused
  without it; `PUT /api/artifacts/{id}` and upload URLs answer 403. Before, a `write` token could
  put new content live on a public artifact.
- A gate answer is bound to its question (security T20): on `2026-07-28` the question's
  `requestState` is signed over the artifact, the version shown, the client and the arguments,
  and on both eras the write must find the version the human saw, else `conflict`.
- An upload URL stops working once its client is removed from `DROP_TOKENS` or loses
  `drop:write` (security T21).
- MCP tool errors mask every failure that is not worded for the caller as `internal error`, as
  HTTP already did; filesystem and parser detail no longer reach the client.
- `config check` and `serve` warn when `public_base_url` is `http://` and the bind address is not
  loopback (security R1).
- Every GitHub Action and both Docker base images are pinned by digest, and the release image is
  built without the shared CI cache. The Chromium sandbox specs run in CI.

### Changed

- The release workflow pushes, smoke-tests and signs the image before publishing to PyPI, and
  runs `twine check` first, so a failed release no longer spends a PyPI version.
- `mcp_drop.__version__` reads the installed package's metadata; `pyproject.toml` is the only
  place the version is written.
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
- A create whose `client_ref` was claimed but whose write failed left the key bound for
  `ref_ttl`, so the retry answered `not found` or `conflict`; the key is now released.
- `expires_in`/`ttl` of `0`, too large to represent, or thousands of digits long returned 500;
  it is now 400 (or a tool error), and a duration is capped at 36500 days.
- A view pinned with thousands of digits (`/a/<id>@999…/`) returned 500; it is now 404.
- A pinned older version opened the current entry, which it may not have; it opens its own.
- A nested entry's `307` sent the raw path as `Location`, so `a:b/index.html` read as a URL
  scheme; it is now `./`-relative and percent-encoded.
- A list cursor with a time without a zone returned 500; it is now 400.
- One torn line in the audit log made every read of it fail, which stopped new uploads and every
  sweep. Unreadable lines are now skipped and logged, and the next append starts a fresh line.
- `serve` refused to start in a shell that also sets the client-side `DROP_URL`/`DROP_TOKEN`.
- `just image` failed on macOS's bash 3.2 when `CA` was not set.
