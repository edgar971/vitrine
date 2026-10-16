# vitrine for Hermes Agent

Hermes uses vitrine in two parts: the vitrine **MCP server**, which does nearly everything, and an
optional **plugin** that publishes files already on disk.

## Connect the MCP server

In `~/.hermes/config.yaml`, with `VITRINE_TOKEN` in `~/.hermes/.env`:

```yaml
mcp_servers:
  vitrine_mcp:                         # any key but `vitrine`, which the plugin owns
    url: "http://localhost:8080/mcp"
    headers: { Authorization: "Bearer ${VITRINE_TOKEN}" }
    elicitation: { timeout: 300 }      # how long the approve/deny prompt for public waits
```

The key becomes the tools' prefix (`mcp__vitrine_mcp__artifact_create`, …). It has to differ from
`vitrine`: with a plugin of that name installed, Hermes registers the server's tools but never
exposes them, so nothing can call them and `tool_search` does not list them.

Making an artifact public shows Hermes's approve/deny prompt, in the CLI or the chat gateway.
Scheduled jobs cannot answer it, so they cannot make anything public.

## The plugin

| Adds | What it does |
|---|---|
| `artifact_publish_files` tool | Streams files or a folder to `POST /api/artifacts` (or `PUT` with `id`, a new version behind the same link) and returns a **private** link. It has no visibility parameter: going public is the server's `artifact_set_visibility`, which asks you. |
| `vitrine:vitrine` skill | When to share output as a link, and to keep it private unless you ask |
| `/share <path>…` | A private link to the files, without a turn of the agent |
| `/artifacts [query]` | The newest artifacts, with ids and visibility (links are minted on purpose, through the agent) |

### Install

```bash
hermes plugins install edgar971/vitrine/integrations/hermes/vitrine --enable
```

In `~/.hermes/.env`:

```bash
VITRINE_URL=https://vitrine.example.com
VITRINE_TOKEN=<a token from `mcp-vitrine token new hermes`>
```

The plugin needs the token's `write` scope to publish and `read` for `/artifacts`. It uses the
standard library only, so it adds no dependencies to Hermes's environment, and runs on every
Python Hermes supports (3.11 and later).

### What it uploads

A file keeps its name. A folder contributes its contents at their paths relative to it, so a
site's `index.html` lands at the root. Hidden files and symlinks inside a folder are skipped:
the server refuses hidden paths, and a link can point anywhere on the machine. Relative paths
resolve against the agent's working directory.

## Development

`tests/integrations/hermes_spec.py` drives a real vitrine server through the plugin. CI also runs
Hermes's own admission check (`hermes plugins validate`) at a pinned Hermes commit.
