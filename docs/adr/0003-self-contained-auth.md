# 0003 — Auth is self-contained: hashed tokens and HMAC links, no proxy dependency

- **Status:** Accepted
- **Date:** 2026-10-15
- **Context:** [`docs/design.md`](../design.md) § 1, § 3; [`docs/spec.md`](../spec.md) § 5, § 7;
  [`docs/security.md`](../security.md) § 3 (T2, T3, T5, T12, T13)

## Context

drop is exposed in two directions: agents call `/mcp` and `/api` with a token, and viewers open
signed or public links. The deployment goal is "easy to run", which for a self-hoster usually means
putting it behind a reverse proxy — and proxies are exactly where authentication is usually
delegated, by assumption rather than by configuration.

Two traps were confirmed while validating the design ([`docs/research.md`](../research.md) § 3):
FastMCP's auth covers `/mcp` and **not** the custom routes, and `StaticTokenVerifier` stores
plaintext tokens. A proxy that someone forgot to configure is not a control.

## Decision

- Token auth is `HashedTokenVerifier`: tokens exist server-side only as SHA-256 hashes, compared in
  constant time, with scopes (`drop:read`, `drop:write`, `drop:share`, `drop:publish`) enforced per
  tool. A tool outside the token's scopes is not listed, and calling it fails as an unknown tool.
- Private links are HMAC-signed over the kind, id, expiry and epoch, so a link cannot be
  transplanted, extended or replayed after revocation. Upload links are single use.
- Every HTTP route authenticates itself — MCP-provider auth, a bearer check, a signature check, or
  an explicit public entry (`/a/…`, `/healthz`) — and a route-table spec fails on any route that is
  not classified. The table is the reason this cannot rot.
- Secrets are env-only (`DROP_SECRET`, `DROP_TOKENS`); `config check` rejects them in a TOML file
  and prints the rest with both redacted.

## Consequences

- A reverse proxy stays optional: it can terminate TLS, and it can block `/mcp`, `/api` and `/u`,
  but nothing depends on it.
- One token per client, revoked by removing one hash; rotating `DROP_SECRET` revokes every private
  link at once.
- Scope enforcement makes least privilege the default but puts token management in the operator's
  hands (`mcp-drop token new <name>` prints the token and its hash, and writes nothing to disk).
- Auth, link signing and the rebinding guard are all in drop, so the threat model has one owner.
