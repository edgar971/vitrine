# vitrine documentation

Installing and using vitrine is covered in the [README](../README.md). This folder is for running
it in production, understanding how it works, and changing it.

## How it works

```
  MCP clients (Claude Code, Hermes, …)                 browsers
     │ MCP + bearer token  │ HTTP upload                  │ GET /a/… /s/…
     ▼                     ▼                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ vitrine: one process, one port                                      │
│   /mcp      tools, resources, the public gate                       │
│   /api /u   uploads              /a /s   views         /healthz     │
│   /_admin   the operator dashboard   /_vitrine  JSX assets          │
├─────────────────────────────────────────────────────────────────────┤
│ data_dir/   artifacts/<id>/meta.json + v<n>/ · audit/ refs/ views/  │
└─────────────────────────────────────────────────────────────────────┘
```

- **One process, one port, one directory.** No database, queue or sidecar. Each artifact is a
  folder with a JSON metadata file and one subfolder per version.
- **Auth is built in.** Agent tokens are stored as SHA-256 hashes. A viewer gets either an
  unguessable public id (`/a/<id>/`) or an HMAC-signed, expiring private link (`/s/<sig>/`). A
  reverse proxy is optional, never relied on.
- **Safe by default.** Everything is private until a human approves public, and the approval
  fails closed. Agent-written HTML runs in a CSP sandbox with an opaque origin.
- **Audited.** Every change is appended to an audit log, which never holds content.
- **Configured from the environment or a TOML file.** Secrets only ever come from the environment.

The full picture is in [design](design.md).

## Guides

| Document | Read it to |
|---|---|
| [operations](operations.md) | Deploy, back up, restore, upgrade, rotate secrets, monitor, handle incidents |
| [configuration](configuration.md) | Look up any setting (generated from the code) |
| [examples](../examples/) | Start from a TOML file, Compose with Caddy, or Kubernetes manifests |
| [Hermes integration](../integrations/hermes/) | Set up Hermes Agent and its plugin |

## Reference

| Document | What it holds |
|---|---|
| [spec](spec.md) | The contract: data model, MCP tools, HTTP routes, configuration, limits, errors |
| [security](security.md) | Threat model, controls and residual risks |
| [design](design.md) | Architecture, storage, flows, extension points, test conventions |
| [adr](adr/) | The decisions that are expensive to reverse |
| [research](research.md) | The platform facts and alternatives behind the design |
| [roadmap](roadmap.md) | What shipped, and what comes next |

## Contributing

Start with [CONTRIBUTING.md](../CONTRIBUTING.md): setup takes `uv` and `just`, and `just check`
runs every gate CI runs. If you work with a coding agent, [AGENTS.md](../AGENTS.md) is its guide.
Report vulnerabilities through [SECURITY.md](../SECURITY.md), not a public issue.
