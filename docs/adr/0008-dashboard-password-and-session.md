# 0008 — The dashboard has its own operator password and a stateless session cookie

- **Status:** Accepted
- **Date:** 2026-10-17
- **Context:** [`docs/spec.md`](../spec.md) § 7.6, § 8.2; [`docs/security.md`](../security.md)
  § 3 (T24, T25), § 5 (R2); ADR 0002, ADR 0003

## Context

Owners could manage artifacts only through an agent or `/api`: there was no page listing
everything, showing what is public and revoking, deleting or making something private in a
click. A web dashboard needs a login, and vitrine already has one credential, the client token.

Agents hold client tokens. If a token opened the dashboard, an agent could open it too, and the
day the dashboard gains out-of-band approvals (phase 2), an agent could approve its own publish
there: risk R2 moved from MCP to the browser.

## Decision

- **One operator password, which no agent is given.** `mcp-vitrine admin password` reads it
  twice from the terminal and prints a `VITRINE_ADMIN_PASSWORD` line holding its scrypt hash
  (`scrypt$n$r$p$salt$hash`, `n=2**15, r=8, p=1`). Like `secret` and `tokens`, the key is read
  from the environment only. The dashboard is off, and its routes are not mounted, while it is
  unset. A bearer token is never a login.
- **A stateless session.** A right password earns a cookie `{issued_at, nonce}` with an HMAC
  over it, under a key derived from `secret` and bound to the password hash. It lasts 12 hours.
  Changing the password or the secret ends every session; there is no session store to keep or
  to back up.
- **The cookie never leaves the dashboard.** `HttpOnly; SameSite=Strict; Path=/_admin`, and
  `Secure` with the `__Secure-` prefix when `public_base_url` is `https`. Plain `http` is
  accepted only on a loopback `public_base_url`, so local use works and nothing else does.
  `__Host-` would pin `Path=/`, which sends the cookie with every artifact request, so it is not
  used.
- **Every mutation is a form `POST`** that must carry vitrine's own `Origin`,
  `Sec-Fetch-Site: same-origin` when the browser sends one, and the session's CSRF token, an
  HMAC of its nonce.
- **Phase 1 only narrows.** Mint a private link, revoke, edit metadata, make private, delete.
  No dashboard route makes anything public; the gate stays the one path to public (ADR 0002).

The alternatives were server-side sessions, which need a store and a sweep for one operator,
and reusing a token, which the threat above rules out.

## Consequences

- A new route class, `SESSION`, in `ROUTE_ACCESS`; the login `POST` is `PASSWORD`. The
  route-table spec probes both.
- Wrong passwords spend `rate_admin_login` (`5/m` per source IP); a right one is refunded.
- Every change made from the dashboard is audited as `client = "@admin"`, which no token name
  can be.
- The dashboard is vitrine's own page and is not sandboxed, so every agent-written string it
  shows (titles, tags, projects, labels, file names) is escaped, and artifact content is never
  shown in it: "Open" mints a private link in a new tab (T25).
- Anything that can read the server's environment sees the hash, not the password. An agent
  that is told the password can log in, so it must not be.
- Phase 2 (an approval page for clients that cannot elicit) will add a second decision source to
  `PublicGate` and amend ADR 0002; it reuses this login.
