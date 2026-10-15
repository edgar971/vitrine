# drop for Hermes Agent

A Hermes plugin that publishes files already on disk to a drop server. The drop MCP server
(`mcp_servers.drop`) does everything else; set it up first ([README](../../README.md#hermes-agent)).

| Adds | What it does |
|---|---|
| `artifact_publish_files` tool | Streams files or a folder to `POST /api/artifacts` (or `PUT` with `id`, a new version behind the same link) and returns a **private** link. It has no visibility parameter: going public is `mcp_drop_artifact_set_visibility`, which asks you. |
| `drop:drop` skill | When to share output as a link, and to keep it private unless you ask |
| `/share <path>…` | A private link to the files, without a turn of the agent |
| `/artifacts [query]` | The newest artifacts, with ids and visibility (links are minted on purpose, through the agent) |

## Install

```bash
hermes plugins install edgar971/drop/integrations/hermes/drop --enable
```

In `~/.hermes/.env`:

```bash
DROP_URL=https://drop.example.com
DROP_TOKEN=<a token from `mcp-drop token new hermes`>
```

The plugin needs the token's `write` scope to publish and `read` for `/artifacts`. It uses the
standard library only, so it adds no dependencies to Hermes's environment, and runs on every
Python Hermes supports (3.11 and later).

## What it uploads

A file keeps its name. A folder contributes its contents at their paths relative to it, so a
site's `index.html` lands at the root. Hidden files and symlinks inside a folder are skipped:
the server refuses hidden paths, and a link can point anywhere on the machine. Relative paths
resolve against the agent's working directory.

## Development

`tests/integrations/hermes_spec.py` drives a real drop server through the plugin. CI also runs
Hermes's own admission check (`hermes plugins validate`) at a pinned Hermes commit.
