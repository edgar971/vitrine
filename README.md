# vitrine

[![ci](https://github.com/edgar971/vitrine/actions/workflows/ci.yml/badge.svg)](https://github.com/edgar971/vitrine/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.15%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

**Shareable links for what your AI agent makes.** vitrine is a small, self-hosted MCP server. Your
agent publishes an HTML report, a chart, a PDF or a folder of files, and gets back a link that
opens in any browser.

- **Private by default.** Links are signed, expire and can be revoked. An artifact becomes public
  only after you approve it.
- **One link per artifact.** When the agent revises it, the link stays the same.
- **Any MCP client.** Claude Code, Hermes Agent, or anything that speaks MCP over HTTP.
- **Easy to run.** One process, one port, one folder of files. No database.

## Install

With [`uv`](https://docs.astral.sh/uv/) (it fetches Python 3.15 for you):

```bash
uvx mcp-vitrine --version          # run without installing
uv tool install mcp-vitrine        # or install the `mcp-vitrine` command
```

Or with Docker. `stable` is the newest release; for a deployment, pin a version from the
[releases](https://github.com/edgar971/vitrine/releases) instead:

```bash
docker pull ghcr.io/edgar971/vitrine:stable
```

## Run it

vitrine needs a secret, to sign links, and one token per client:

```bash
export VITRINE_SECRET="$(uvx mcp-vitrine secret new)"
uvx mcp-vitrine token new claude
# token: 3AVjpX77…            ← give this to the client
# entry: claude:65b2dd…:read+write+share+publish
export VITRINE_TOKENS="claude:65b2dd…:read+write+share+publish"   # the entry it printed
uvx mcp-vitrine serve              # http://localhost:8080, data in ./data
```

With Docker, the same commands run inside the image:

```bash
docker run --rm ghcr.io/edgar971/vitrine:stable secret new
docker run --rm ghcr.io/edgar971/vitrine:stable token new claude
docker run -d --name vitrine -p 127.0.0.1:8080:8080 -v vitrine-data:/data \
  -e VITRINE_SECRET=<secret> -e VITRINE_TOKENS=<entry> \
  ghcr.io/edgar971/vitrine:stable
```

Check that it works, using the token that `token new` printed:

```bash
export VITRINE_TOKEN=<token>
echo '<h1>hello</h1>' > index.html
curl -s -H "Authorization: Bearer $VITRINE_TOKEN" -F title=Hello -F file=@index.html \
  localhost:8080/api/artifacts
# {"id": "…", "url": "http://localhost:8080/s/…/", …}
```

Open the `url`: a private, expiring link to your page.

## Connect your agent

vitrine serves MCP at `/mcp`, authenticated with a bearer token.

**Claude Code**

```bash
claude mcp add --transport http vitrine http://localhost:8080/mcp \
  --header "Authorization: Bearer $VITRINE_TOKEN"
```

**Any other MCP client** that supports remote servers with headers:

```json
{
  "mcpServers": {
    "vitrine": {
      "url": "http://localhost:8080/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

**Hermes Agent** connects the same way, and an optional plugin publishes files from disk: see
[integrations/hermes](https://github.com/edgar971/vitrine/tree/main/integrations/hermes/).

Give each client its own token, so you can revoke one without the others. Grant only the scopes it
needs: `token new ci --scopes read,write` cannot share links or make anything public.

## Use it

Ask your agent for something you would open in a browser:

> Build an HTML report of last month's energy use and share it with me.

> Update the report with this week's numbers. *(same link, new version)*

> Make that report public. *(you are asked to approve first)*

What the agent can do:

| Tool | Scope | Does |
|---|---|---|
| `artifact_create` | `write` | Publish text files (HTML, Markdown, CSV, SVG, …) and get a private link |
| `artifact_update` | `write` | Add a version behind the same link |
| `artifact_set_meta` | `write` | Rename, retag or move to a project, without a new version |
| `artifact_upload_url` | `write` | A single-use upload URL for binaries and large files |
| `artifact_delete` | `write` | Remove an artifact and every link to it |
| `artifact_get` / `artifact_list` | `read` | Read an artifact back, or search earlier ones by words, project, tag or expiry |
| `artifact_link` | `share` | A fresh private link, with its own expiry |
| `artifact_revoke_links` | `share` | Revoke every private link issued so far |
| `artifact_set_visibility` | `publish` | Make an artifact public, after you approve |

Approval goes through your MCP client's confirmation prompt. If the client cannot ask, or you
decline or don't answer, the artifact stays private.

## Beyond localhost

Set `VITRINE_PUBLIC_BASE_URL` to the address people will open, for example
`https://vitrine.example.com`, and put vitrine behind TLS. Ready-made setups are in
[examples](https://github.com/edgar971/vitrine/tree/main/examples/): Compose with Caddy, and
Kubernetes. Backups, upgrades and monitoring are in
[operations](https://github.com/edgar971/vitrine/blob/main/docs/operations.md), and every setting is
in the [configuration reference](https://github.com/edgar971/vitrine/blob/main/docs/configuration.md).

## Learn more

- [Documentation](https://github.com/edgar971/vitrine/blob/main/docs/README.md): how it works, the
  spec, the security model, and how to contribute.
- [Changelog](https://github.com/edgar971/vitrine/blob/main/CHANGELOG.md)
- Found a vulnerability? Follow [SECURITY.md](https://github.com/edgar971/vitrine/blob/main/SECURITY.md)
  rather than opening an issue.

MIT licensed.
