# vitrine research

The evidence behind the [spec](spec.md) and [design](design.md), gathered on 2026-10-15. Platform
facts were checked against installed source (`fastmcp==4.1.0`, `mcp==2.3.0`) and the Hermes Agent
source at `NousResearch/hermes-agent@348f1451` (2026-10-14), not taken from memory or summaries.

## 1. Problem

Agent frameworks deliver files into chat as attachments. For example, Hermes Agent's
"deliverable mode" uploads a file path named in a reply to Slack, Telegram, Discord or WhatsApp.
Images and PDFs attach, but **HTML uploads as a file and is not rendered**. Nothing provides:

- a browser URL;
- a stable link across revisions;
- a choice of audience;
- revocation;
- a list of what was shared.

Hosted artifact features solve this inside a single vendor's UI. There is no self-hosted,
client-agnostic equivalent.

## 2. Options considered

| Option | Verdict |
|---|---|
| Static file server with a WebDAV/SFTP drop folder | No metadata, versions, expiry or listing. Unsandboxed HTML. Agents would need filesystem credentials. |
| Existing self-hosted sharing apps (Zipline, PicoShare, Pingvin Share and similar) | Designed for humans uploading files. None updates in place at a stable URL, none speaks MCP, and they serve HTML as text or a download (for good reason). Several need a database. |
| SQL metadata (SQLite or Postgres) | Not needed at the target scale, and a second source of truth to keep consistent with the files. Kept as a possible `Store` implementation. |
| Client plugin only | Locks the feature to one agent framework |
| MCP only | Binary files would travel as base64 inside tool arguments: the model has to emit every byte. Solved instead with `/api` uploads and signed upload URLs. |
| Zip bundles | Brings in zip-bomb and zip-slip classes. Multipart multi-file upload covers the same need. Since reversed for agents that only have a shell: [ADR 0007](adr/0007-zip-archives-unpacked-server-side.md). |
| **MCP server with a filesystem store, own auth, optional client plugins** | **Chosen** |

## 3. Platform facts

### FastMCP 4.1

| Fact | Consequence for vitrine |
|---|---|
| `mcp.http_app(path=…)` gives a Starlette ASGI app; `@mcp.custom_route` adds HTTP routes | One process and one port serve MCP and the views |
| Custom routes are **not** covered by the auth provider | Every route authenticates itself, with a route-table test (security T3) |
| `TokenVerifier` can be subclassed; `StaticTokenVerifier` is documented as "never use in production" (plaintext tokens) | vitrine's verifier checks SHA-256 hashes |
| `get_access_token().client_id` is available inside tools | Records the `source` client |
| `ctx.elicit()` raises on `2026-07-28` connections. Those use the SEP-2322 guard pattern: return `InputRequiredResult`, read `ctx.input_responses` on the retry. | The public gate implements both (spec § 6.1) |
| `ctx.elicit(response_type=None)` is refused; `bool` fails validation when a client accepts with `{}` | The consent form uses one boolean field with a default |
| `http_app(host_origin_protection=True, allowed_hosts=…)` guards every route | Health checks must send an allowed `Host` |
| The era check is `Context._is_modern_protocol()`, which is private | Minor-version pin plus per-era tests |
| `LocalProvider` takes `@provider.tool(...)`; servers compose providers with `FastMCP(providers=[...])` | One provider per feature, no global server (design § 4) |
| The decorator accepts `title`, `annotations` (`ToolAnnotations`), `tags`, `timeout`, `auth`, `meta`, and defaults to `run_in_thread=True` | Annotations and a timeout on every tool; sync file I/O stays off the event loop |
| `Annotated[T, Field(description=…, max_length=…)]` lands in the input schema | Parameter docs and limits live with the signature |
| `Depends(...)` and `CurrentAccessToken()` parameters are injected and hidden from the schema | Services and caller identity without globals |
| `auth=require_scopes(...)` hides the tool from tokens without the scope; calling it fails as an unknown tool | Least-privilege token scopes (spec § 5.0) |
| **No server-side `resources/subscribe` on a `LocalProvider`, and the capabilities report `"subscribe": false`** | Resources are readable, but a client cannot be *notified*: `artifact://{id}/views` and `…/history` updates wait for FastMCP ([#53](https://github.com/edgar971/vitrine/issues/53)) rather than going through the lower-level `mcp` server hooks |

### Hermes Agent

Hermes was the first integration target, and is representative of agent frameworks with chat
gateways.

| Fact | Source | Consequence |
|---|---|---|
| Remote MCP servers are configured with `url` and `headers`; `${VAR}` is interpolated from `~/.hermes/.env` | `tools/mcp_tool_config.py` | Tokens stay out of config files |
| It negotiates the handshake-era protocol (`2025-11-25` against FastMCP 4.1) | `tools/mcp_tool.py`, measured | The `ctx.elicit()` branch is the one Hermes uses today |
| Elicitation is shown as **approve/deny**, accepting with empty content. It fails closed on timeout (default 300 s, `elicitation.timeout`), on errors and in unattended contexts (cron, single-query runs). | `tools/mcp_tool_sampling.py`, `tools/approval_prompt.py` | The human gate works across Hermes's CLI and chat gateways without extra code |
| Plugin API: `register_tool(name, toolset, schema, handler, …)`, `register_skill(name, path, …)`, `register_command(name, handler, …, args_hint)`, hooks; `call_mcp` is default-deny per server | `hermes_cli/plugins.py`, `plugins_content.py` | The plugin's shape (design § 8) |
| `hermes plugins validate` runs manifest checks, an isolated `register()` probe, a collision check and a security scan | `hermes_cli/plugin_validate.py` | The plugin's admission test, run in CI |

## 4. Findings that shaped the design

| # | Finding | Where it is handled |
|---|---|---|
| F1 | MCP `2026-07-28` removed server-initiated elicitation, while popular clients still negotiate `2025-11-25` | Spec § 6.1 (both mechanisms), design § 6 |
| F2 | An approve-only client accepting with `{}` fails a `bool` elicitation, so the gate would never open | A consent model with a defaulted field; decide on `action` |
| F3 | The Host guard returns 421 to health probes that send the pod IP as `Host` | Spec § 7.4; the Kubernetes example sets the probe `Host` header |
| F4 | Custom routes bypass MCP auth by default | Security T3 |
| F5 | Signed links are credentials and end up in access logs | Spec § 11 redaction |
| F6 | Self-hosters run on mixed arm64 and amd64 hardware | Multi-arch image (design § 10) |
| F7 | Small deployments often serve plain HTTP on a "trusted" network | Security R1 and the `config check` warning |

## 5. The design spike

Before any of vitrine was written, a throwaway spike implemented the load-bearing mechanisms —
`/mcp` auth, the views with their headers and 404s, signed links and their failure modes, path
traversal, upload limits, DNS rebinding, the Chromium sandbox, and both elicitation eras — and
checked them from the outside: 59 checks, all passing on Python 3.13 and on 3.15.0.

The spike itself is gone. Every check it made is a spec under `tests/` that runs in CI, and the
[threat model](security.md) § 3 names the spec holding each control. What the spike discovered
about a *platform*, rather than about vitrine, is in the tables above.

Two of its findings are still load-bearing for CI:

- `hermes plugins validate` runs headless (`python -I -m hermes_cli.main plugins validate`) with
  no configuration, no gateway and an empty `HOME`. PyPI's `hermes-agent` predates the subcommand,
  and `uv tool install git+…/hermes-agent` refuses to build, so CI uses Hermes's own composite
  action, `NousResearch/hermes-agent/.github/actions/plugin-validate@<ref>`.
- That action's bootstrap `python3` must be 3.11 or newer, and its `setup_toolchain.py` writes to
  `$GITHUB_OUTPUT`, `$GITHUB_ENV` and `$GITHUB_PATH` — so it runs inside a GitHub Actions job,
  never on a bare shell. `ubuntu-latest` satisfies both.
