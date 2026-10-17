# ADR index

Decisions that are expensive to reverse ([`docs/design.md`](../design.md) § 10). Each one is
`Accepted` and short: the context, the decision, and what follows from it.

| # | Decision |
|---|---|
| [0001](0001-filesystem-store.md) | The default `Store` is the filesystem, not a database |
| [0002](0002-one-gate-to-public.md) | `PublicGate` is the only path to public, and it fails closed |
| [0003](0003-self-contained-auth.md) | Auth is self-contained: hashed tokens and HMAC links, no proxy dependency |
| [0004](0004-protocol-era-handling.md) | Both MCP protocol eras are implemented, behind one gate |
| [0005](0005-gated-public-updates.md) | New content on a public artifact goes through the gate, bound to what the human saw |
| [0006](0006-link-records-and-unlock-in-the-path.md) | Links become records with a `lid`; a passcode unlock rides in the path, not a cookie |
| [0007](0007-zip-archives-unpacked-server-side.md) | Zip archives are unpacked server-side, under the write path's rules |

New ADRs: copy the shape of an existing one, number it after the last, and link it here and from
the pull request that makes the decision.
