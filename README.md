# drop

**Shareable, versioned artifacts for AI agents.** drop is a small MCP server. An agent publishes
an HTML report, a chart, a PDF or a folder of files, and gets back a link that opens in any
browser. Links are **private** (signed, expiring, revocable) by default. They become **public**
only after a human confirms.

> **Status: in progress, not released.** The core, the MCP layer and the web layer are built
> (M1–M3); the `mcp-drop` CLI that serves them arrives with M4, so there is nothing to run yet.
> The build follows [`docs/roadmap.md`](docs/roadmap.md).

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

## Documents

| Document | Contents |
|---|---|
| [`docs/spec.md`](docs/spec.md) | The contract: concepts, data model, MCP tools and resources, HTTP routes, configuration reference, CLI, limits, errors |
| [`docs/design.md`](docs/design.md) | Architecture, modules, storage protocol, request flows, extension points, project tooling and release, deployment options, client integrations |
| [`docs/security.md`](docs/security.md) | Threat model, trust boundaries, controls, residual risks, required security tests |
| [`docs/operations.md`](docs/operations.md) | Backup, restore, upgrades and migrations, rotation, monitoring, incident playbooks |
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
