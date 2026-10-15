# drop

**Shareable, versioned artifacts for AI agents.** drop is a small MCP server. An agent publishes
an HTML report, a chart, a PDF or a folder of files, and gets back a link that opens in any
browser. Links are **private** (signed, expiring, revocable) by default. They become **public**
only after a human confirms.

> **Status: `0.1.0` release candidate.** The server, the CLI, the container image, the deployment
> examples and the client integrations are built (M1–M5,
> [`docs/roadmap.md`](docs/roadmap.md)); the release itself is next.

## Why

Agents produce artifacts all the time: reports, dashboards, diagrams, exports. Chat
integrations can attach a file, but they can't:

- render HTML as a page;
- keep a stable URL while the content is revised;
- let you choose who can see it, or revoke it later;
- keep a list of what was shared.

Hosted products such as claude.ai Artifacts solve this inside one vendor's UI. drop is the
self-hosted, client-agnostic equivalent: any MCP client can publish, and anyone with a browser
can view.

## At a glance

| | |
|---|---|
| **Interface** | MCP tools and resources (Streamable HTTP), plus a few plain HTTP routes for viewing and uploads |
| **Storage** | A directory on disk: one folder per artifact, immutable version folders and a JSON metadata file. No database. |
| **Auth** | Agent tokens are checked against SHA-256 hashes. Viewers use unguessable public ids or HMAC-signed private links. Works without a reverse proxy. |
| **Safety** | Private by default. Public needs human confirmation via MCP elicitation and fails closed. Agent-written HTML runs in a CSP sandbox. |
| **Config** | `DROP_*` environment variables or a TOML file. Secrets only from the environment. |
| **Runs as** | `uvx mcp-drop serve`, a container image (amd64 and arm64), or the Kubernetes example |
| **Built with** | Python 3.15+, FastMCP, Starlette/uvicorn, pydantic-settings; uv, ruff, mypy, pytest |
| **License** | MIT |

## Quick start

You need [`uv`](https://docs.astral.sh/uv/). `uvx` fetches Python 3.15 and drop on first use;
nothing else is installed.

```bash
# 1. A secret for signing links, and a token for your agent (printed once, stored nowhere).
export DROP_SECRET="$(uvx mcp-drop secret new)"
uvx mcp-drop token new claude
#   token: <give this to the client>
#   entry: claude:<sha256>:read+write+share+publish

# 2. Serve on http://localhost:8080, keeping artifacts in ./data.
export DROP_TOKENS="claude:<sha256>:read+write+share+publish"   # the entry line
uvx mcp-drop serve
```

Until `0.1.0` is on PyPI, use `uvx --from git+https://github.com/edgar971/drop mcp-drop` in place
of `uvx mcp-drop`.

Check it from another shell:

```bash
TOKEN=<the token from step 1>
curl -s localhost:8080/healthz                               # {"ok":true}
echo '<h1>hello</h1>' > index.html
curl -s -H "Authorization: Bearer $TOKEN" -F title=Hello -F file=@index.html \
  localhost:8080/api/artifacts                               # {"id": …, "url": ".../s/…/", …}
```

Open the `url` in a browser. To serve beyond localhost, set `DROP_PUBLIC_BASE_URL` to the
address people will open, and see [`docs/operations.md`](docs/operations.md) for TLS, backups
and the container image.

## Connect a client

drop speaks MCP over Streamable HTTP at `/mcp` with a bearer token. Give each client its own token
(`mcp-drop token new <name>`), so the audit log and revocation are per client, and only the scopes
it needs (`--scopes read,write` leaves out `share` and `publish`).

Making an artifact public asks a human through MCP elicitation. A client that cannot ask, or a
person who declines or does not answer, leaves it private.

### Claude Code

```bash
claude mcp add --transport http drop http://localhost:8080/mcp \
  --header "Authorization: Bearer $DROP_TOKEN"
```

Or, shared with a project, in `.mcp.json` (Claude Code expands `${DROP_TOKEN}` from the
environment, so the token stays out of the file):

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

### Hermes Agent

In `~/.hermes/config.yaml`, with `DROP_TOKEN` in `~/.hermes/.env`:

```yaml
mcp_servers:
  drop:
    url: "http://localhost:8080/mcp"
    headers: { Authorization: "Bearer ${DROP_TOKEN}" }
    elicitation: { timeout: 300 }   # how long the approve/deny prompt for public waits
```

The optional plugin adds `artifact_publish_files`, which streams files already on disk (PDFs,
images, a folder with assets) to drop as a private link, a `drop:drop` skill, and the `/share
<path>…` and `/artifacts [query]` commands. It needs `DROP_URL` (the server's base URL) beside
`DROP_TOKEN` in `~/.hermes/.env`:

```bash
hermes plugins install edgar971/drop/integrations/hermes/drop --enable
```

See [`integrations/hermes/`](integrations/hermes/).

### Other MCP clients

Any client that supports remote (Streamable HTTP) servers with custom headers works. Most take
this shape:

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

Binary files that are too large to pass inline go through `artifact_upload_url`, which returns a
single-use URL and a ready-made `curl -T` command.

## Documents

| Document | Contents |
|---|---|
| [`docs/spec.md`](docs/spec.md) | The contract: concepts, data model, MCP tools and resources, HTTP routes, configuration reference, CLI, limits, errors |
| [`docs/design.md`](docs/design.md) | Architecture, modules, storage protocol, request flows, extension points, project tooling and release, deployment options, client integrations |
| [`docs/security.md`](docs/security.md) | Threat model, trust boundaries, controls, residual risks, required security tests |
| [`docs/operations.md`](docs/operations.md) | Backup, restore, upgrades and migrations, rotation, monitoring, incident playbooks, deployments |
| [`docs/configuration.md`](docs/configuration.md) | Every configuration key with its environment variable and default (generated) |
| [`examples/`](examples/) | A config file, Compose with Caddy, and Kubernetes manifests |
| [`integrations/`](integrations/) | Client plugins: the Hermes Agent plugin |
| [`docs/roadmap.md`](docs/roadmap.md) | Milestones M0–M5 to `v0.1.0`, each with deliverables and testable exit criteria |
| [`docs/research.md`](docs/research.md) | Prior art, options considered, platform facts checked against source, validation findings and the record of what the spike proved |
| [`docs/spike/`](docs/spike/) | The throwaway prototype and checks the design was validated against |
| [`docs/adr/`](docs/adr/) | The accepted decisions that are expensive to reverse |

## Example

```text
you    > make an HTML summary of last month's energy usage and send me the link
agent  → artifact_create(title="Energy, September", files={"index.html": "…"})
drop   ← {id: "k7m2q9x4tz3wbn6r", url: "https://drop.example.com/s/eyJ…/", version: 1}
agent  > Here it is (private link, valid 7 days): https://drop.example.com/s/eyJ…/
you    > add a chart, then make it public
agent  → artifact_update(id=…, files={…})                       same id, version 2
agent  → artifact_set_visibility(id=…, visibility="public")
client ⇢ "Make 'Energy, September' (2 files) PUBLIC? Anyone with the link …"  [Approve] [Deny]
drop   ← {url: "https://drop.example.com/a/k7m2q9x4tz3wbn6r/"}
```
