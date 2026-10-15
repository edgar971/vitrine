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
