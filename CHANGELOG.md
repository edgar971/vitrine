# Changelog

All notable changes to this project are documented here. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each version's section is generated from its Conventional Commits by git-cliff
([CONTRIBUTING.md](CONTRIBUTING.md#changelog)); `0.1.0`'s was written by hand, before that.

## [Unreleased]

The first release. Everything below is new.

### Added

- **MCP server** over Streamable HTTP at `/mcp`: nine `artifact_*` tools (create, update, get,
  list, link, set visibility, revoke links, upload URL, delete), the `artifact://` resources for
  reading an artifact back, its files and its history, a `share` prompt, and server instructions
  that carry the sharing policy. Every tool has a title, all four annotation hints and described,
  bounded parameters.
- **Private by default.** Every artifact starts private, behind signed, expiring, revocable
  `/s/…` links. It becomes public at `/a/<id>/` only through `artifact_set_visibility`, after a
  human approves through MCP elicitation. Both protocol eras are supported (`2025-11-25`
  `elicitation/create` and `2026-07-28` `InputRequiredResult`), and the gate fails closed on
  decline, timeout, error or a client that cannot ask. New content on a public artifact goes
  through the same gate.
- **Versions behind one URL.** Updating keeps the id and the link; `@<n>` pins a version, and
  the newest `retain_versions` are kept.
- **Viewing in any browser**: multi-file artifacts with relative links, `Range` for media,
  Markdown, CSV/TSV and JSON renderers, and agent-written HTML in a CSP sandbox with an opaque
  origin (no `allow-same-origin`).
- **Uploads for files a model should not emit byte by byte**: multipart `POST /api/artifacts`
  and `PUT /api/artifacts/{id}`, and single-use signed upload URLs (`artifact_upload_url`, then
  `curl -T`).
- **Hashed bearer tokens with scopes** (`read`, `write`, `share`, `publish`), one per client, so
  revocation and the audit log stay per client.
- **Idempotent create** with `client_ref`, so a retried call never makes a duplicate.
- **Audit log** of every state change in `audit/<YYYY-MM>.jsonl`, fsync'd before the change is
  acknowledged, holding ids and client names, never content.
- **Limits**: streaming body limits, per-version size and file count, a storage quota, and
  per-client and per-IP rate limits.
- **The `mcp-vitrine` CLI**: `serve`, `config check`, `token new|hash`, `secret new`, `sweep`,
  `audit`, an online `backup` and forward-only `migrate`.
- **Configuration** from `VITRINE_*` variables or a TOML file, with secrets accepted from the
  environment only and unknown keys refused. Every key is in
  [`docs/configuration.md`](docs/configuration.md).
- **Optional Prometheus metrics** on a separate `metrics_port` (the `metrics` extra), never
  labelled by artifact id or title.
- **A container image**, `ghcr.io/edgar971/vitrine`, for amd64 and arm64: non-root, fine with a
  read-only root filesystem and no capabilities, signed with cosign and shipped with an SBOM.
- **Deployment examples** in [`examples/`](examples/): a TOML file, Compose with Caddy for TLS,
  and Kubernetes manifests (Pod Security `restricted`, default-deny network policy).
- **A Hermes Agent plugin** ([`integrations/hermes/`](integrations/hermes/)) that publishes files
  already on disk, with a skill and the `/share` and `/artifacts` commands.
- **Documentation**: the [spec](docs/spec.md), [design](docs/design.md), the
  [threat model](docs/security.md), [operations](docs/operations.md) and the
  [decision records](docs/adr/).
