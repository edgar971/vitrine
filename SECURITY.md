# Security policy

vitrine holds private data and hands out links to it, so security reports are welcome and taken
seriously. The threat model, the controls and the residual risks are in
[`docs/security.md`](docs/security.md).

## Supported versions

No release exists yet. Until `0.1.0` ships, only the `main` branch is supported; after that, the
latest `0.1.x` is, and older minor versions are not.

| Version | Supported |
|---|---|
| `main` | ✅ |
| `< 0.1.0` releases | none exist |

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: **Security → Report a vulnerability** on
<https://github.com/edgar971/vitrine/security/advisories/new>. Please do not open a public issue, and
do not test against someone else's deployment.

A useful report includes:

- what an attacker gains, and from where (a link, a token, the network, a malicious artifact);
- the smallest reproduction you have, against which version or commit;
- whether it is already described in [`docs/security.md`](docs/security.md) § 3 or § 5.

Expect an acknowledgement within a few days. Please give us a reasonable window to ship a fix
before writing it up publicly.

## Out of scope

- Denial of service through sheer volume (rate limits and quotas bound it; see security § 3 T16).
- Anything that depends on an agent with shell access on the same host reading its own token
  (security § 5 R2).
- A leaked link being usable by whoever holds it: that is what "anyone with the link" means
  (security § 5 R4).
- Reports from a scanner without a demonstrated impact on vitrine.
