# drop

[![ci](https://github.com/edgar971/drop/actions/workflows/ci.yml/badge.svg)](https://github.com/edgar971/drop/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.15%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

**Shareable, versioned artifacts for AI agents.** drop is a small, self-hosted MCP server. An
agent publishes an HTML report, a chart, a PDF or a folder of files, and gets back a link that
opens in any browser.

Links are **private** by default — signed, expiring, revocable — and become **public** only after
a human confirms.

## Why

Agents produce artifacts constantly: reports, dashboards, diagrams, exports. A chat integration
can attach the file, but it cannot:

- render HTML as a page;
- keep one URL while the content is revised;
- let you decide who can see it, or revoke it later;
- tell you what was shared.

claude.ai Artifacts and friends solve this inside one vendor's UI. drop is the self-hosted,
client-agnostic equivalent: any MCP client can publish, and anyone with a browser can view.

## Quick start

All you need is [`uv`](https://docs.astral.sh/uv/). `uvx` fetches Python 3.15 and drop on first
use; nothing is installed permanently.

```bash
export DROP_SECRET="$(uvx mcp-drop secret new)"   # signs every private link
uvx mcp-drop token new claude                     # prints a token, and the entry holding its hash
export DROP_TOKENS="claude:<sha256>:read+write+share+publish"   # the entry it printed
uvx mcp-drop serve                                # http://localhost:8080, artifacts in ./data
```

Check it from another shell:

```bash
curl -s localhost:8080/healthz                    # {"ok":true}

echo '<h1>hello</h1>' > index.html
curl -s -H "Authorization: Bearer $TOKEN" -F title=Hello -F file=@index.html \
  localhost:8080/api/artifacts                    # {"id": …, "url": "http://localhost:8080/s/…/"}
```

Open the `url`: that is the whole product, a private expiring link to an HTML page.

Serving beyond localhost? Set `DROP_PUBLIC_BASE_URL` to the address people will open, and read
[operations](docs/operations.md) for TLS, backups and the container image.

## Connect a client

drop speaks MCP over Streamable HTTP at `/mcp` with a bearer token. Give every client its own
token (`mcp-drop token new <name>`) so revocation and the audit log stay per client, and only the
scopes it needs — `--scopes read,write` leaves out `share` and `publish`.

Making an artifact public asks a human through MCP elicitation. A client that cannot ask, or a
person who declines or does not answer, leaves it private.

**Claude Code**

```bash
claude mcp add --transport http drop http://localhost:8080/mcp \
  --header "Authorization: Bearer $DROP_TOKEN"
```

Or in a project's `.mcp.json` (Claude Code expands `${DROP_TOKEN}` from the environment, so the
token never lands in the file):

```json
{
  "mcpServers": {
    "drop": {
      "type": "http",
      "url": "http://localhost:8080/mcp",
      "headers": { "Authorization": "Bearer ${DROP_TOKEN}" }
    }
  }
}
```

**Hermes Agent** — in `~/.hermes/config.yaml`, with `DROP_TOKEN` in `~/.hermes/.env`:

```yaml
mcp_servers:
  drop:
    url: "http://localhost:8080/mcp"
    headers: { Authorization: "Bearer ${DROP_TOKEN}" }
    elicitation: { timeout: 300 }   # how long the approve/deny prompt for public waits
```

The optional plugin publishes files already on disk (PDFs, images, a folder with assets) through
`artifact_publish_files`, and adds a `drop:drop` skill plus the `/share <path>…` and
`/artifacts [query]` commands. It needs `DROP_URL` (the server's base URL) beside `DROP_TOKEN`:

```bash
hermes plugins install edgar971/drop/integrations/hermes/drop --enable
```

See [integrations/hermes](integrations/hermes/) for details.

**Any other MCP client** that supports remote servers with headers:

```json
{
  "mcpServers": {
    "drop": {
      "url": "https://drop.example.com/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Files too large to pass inline go through `artifact_upload_url`, which returns a single-use URL
and a ready-made `curl -T` command.

## How it works

- **One process, one port, one directory.** No database, no queue, no sidecar: artifacts are
  folders on disk, each with a JSON metadata file.
- **Auth is built in.** Agent tokens are stored as SHA-256 hashes; viewers get either an
  unguessable public id or an HMAC-signed private link. A reverse proxy is optional, never
  required.
- **Safe by default.** Private unless a human approves public, and the gate fails closed.
  Agent-written HTML runs in a CSP sandbox, in an opaque origin.
- **Plain HTTP for browsers.** `/a/<id>/` views a public artifact, `/s/<sig>/` serves a private
  link, `/api/artifacts` takes uploads, `/healthz` answers probes.
- **Configure it your way.** `DROP_*` environment variables or a TOML file; secrets only ever
  come from the environment. Every key is in [configuration](docs/configuration.md).

## Documents

- [spec](docs/spec.md) — the contract: data model, MCP tools, HTTP routes, configuration, limits.
- [design](docs/design.md) — how it is built: architecture, storage, flows, extension points.
- [security](docs/security.md) — threat model, controls and residual risks.
- [operations](docs/operations.md) — backup, restore, upgrades, monitoring, deployments.
- [roadmap](docs/roadmap.md) — what is left before `0.1.0`, and what comes after.
- [examples](examples/) — a TOML file, Compose with Caddy, Kubernetes manifests.
- [adr](docs/adr/) — the decisions that are expensive to reverse.

## Contributing

Bug reports and pull requests are welcome — start with [CONTRIBUTING.md](CONTRIBUTING.md), and
[AGENTS.md](AGENTS.md) if you work with a coding agent. Report vulnerabilities through
[SECURITY.md](SECURITY.md) rather than a public issue.

MIT licensed.
