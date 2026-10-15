# drop research and validation

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
| Zip bundles | Brings in zip-bomb and zip-slip classes. Multipart multi-file upload covers the same need. |
| **MCP server with a filesystem store, own auth, optional client plugins** | **Chosen** |

## 3. Platform facts

### FastMCP 4.1

| Fact | Consequence for drop |
|---|---|
| `mcp.http_app(path=…)` gives a Starlette ASGI app; `@mcp.custom_route` adds HTTP routes | One process and one port serve MCP and the views |
| Custom routes are **not** covered by the auth provider | Every route authenticates itself, with a route-table test (security T3) |
| `TokenVerifier` can be subclassed; `StaticTokenVerifier` is documented as "never use in production" (plaintext tokens) | drop's verifier checks SHA-256 hashes |
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

### Hermes Agent

Hermes is the first integration target, and representative of agent frameworks with chat
gateways.

| Fact | Source | Consequence |
|---|---|---|
| Remote MCP servers are configured with `url` and `headers`; `${VAR}` is interpolated from `~/.hermes/.env` | `tools/mcp_tool_config.py` | Tokens stay out of config files |
| It negotiates the handshake-era protocol (`2025-11-25` against FastMCP 4.1) | `tools/mcp_tool.py`, measured | The `ctx.elicit()` branch is the one Hermes uses today |
| Elicitation is shown as **approve/deny**, accepting with empty content. It fails closed on timeout (default 300 s, `elicitation.timeout`), on errors and in unattended contexts (cron, single-query runs). | `tools/mcp_tool_sampling.py`, `tools/approval_prompt.py` | The human gate works across Hermes's CLI and chat gateways without extra code |
| Plugin API: `register_tool(name, toolset, schema, handler, …)`, `register_skill(name, path, …)`, `register_command(name, handler, …, args_hint)`, hooks; `call_mcp` is default-deny per server | `hermes_cli/plugins.py`, `plugins_content.py` | The plugin's shape (design § 7.2) |
| `hermes plugins validate` runs manifest checks, an isolated `register()` probe, a collision check and a security scan | `hermes_cli/plugin_validate.py` | Used as the plugin's admission test |

## 4. Findings that shaped the design

| # | Finding | Where it is handled |
|---|---|---|
| F1 | MCP `2026-07-28` removed server-initiated elicitation, while popular clients still negotiate `2025-11-25` | Spec § 6.1 (both mechanisms), design § 10 |
| F2 | An approve-only client accepting with `{}` fails a `bool` elicitation, so the gate would never open | A consent model with a defaulted field; decide on `action` |
| F3 | The Host guard returns 421 to health probes that send the pod IP as `Host` | Spec § 7.4; the Kubernetes example sets the probe `Host` header |
| F4 | Custom routes bypass MCP auth by default | Security T3 |
| F5 | Signed links are credentials and end up in access logs | Spec § 11 redaction |
| F6 | Self-hosters run on mixed arm64 and amd64 hardware | Multi-arch image (design § 8) |
| F7 | Small deployments often serve plain HTTP on a "trusted" network | Security R1 and the `config check` warning |

## 5. Validation record

The throwaway spike in [`spike/`](spike/) implements the load-bearing mechanisms and checks them
from the outside. All 59 checks passed on Python 3.13, and again on 3.15.0 (2026-10-15) when the
project moved to 3.15.

```bash
cd spike
uv run --no-project --python 3.15 --with fastmcp==4.1.0 --with uvicorn --with httpx \
  --with playwright python run.py          # exit 0 when every check passes
```

### `test_spike.py` (42 checks, FastMCP client, protocol `2026-07-28`)

- **Auth:** no token gets 401; a wrong token gets 401; a raw POST gets 401; a valid token lists
  the tools; the client name is recorded as the source.
- **Single gate:**
  - `artifact_create` rejects a `visibility` argument;
  - a new artifact is private with a signed link;
  - public is reachable only through `artifact_set_visibility` after confirmation.
- **Views:**
  - a public artifact gets 200;
  - HTML and SVG carry the sandbox CSP without `allow-same-origin`;
  - `nosniff`, `no-referrer` and `noindex` are set;
  - a private id on `/a/` gets 404.
- **Links:**
  - a signed link gets 200, and so do relative assets under it;
  - these all get 404: a tampered tag, a signature moved to another id, an expired link, an
    upload signature used to view, and a link after revocation.
- **Versions:** an update keeps the id and URL and serves the new version.
- **Traversal:** `../` is rejected on create, and four encoded and plain `..` URL variants get 404
  without leaking metadata.
- **Resources:** `artifact://{id}` reads back.
- **Chromium:** the script runs; `document.cookie` and `localStorage` throw even with a host cookie
  set; `window.origin` is `"null"`; a relative image loads under a signed path.
- **Gate:** decline keeps it private; a client without elicitation cannot go public; accept makes
  it public.
- **Custom routes:** `/healthz` answers without a token (the bypass); the upload route returns 401
  without one.
- **Uploads:** oversize by `Content-Length` gets 413; oversize chunked with no length gets 413; a
  traversal filename gets 400; a valid upload gets 201 with a private link; an unknown type is
  served as `octet-stream` with `attachment`.

### `test_handshake.py` (4 checks, raw `mcp` SDK `ClientSession`, as Hermes connects)

- `2025-11-25` is negotiated.
- The elicitation callback is invoked.
- An accept with `{}` makes the artifact public.
- A decline leaves it private.

### `test_origin.py` (6 checks, uvicorn bound to `0.0.0.0`)

- An allowed Host gets 200.
- A foreign Host gets 421, and so does a foreign Host with a matching Origin (DNS rebinding).
- A foreign Origin on an allowed Host gets 403.
- A probe with the pod IP as Host gets 421; with an allowed Host it gets 200.

### `test_patterns.py` (7 checks, the design § 4 conventions)

- Injected parameters are hidden from the schema.
- The `Annotated` description and constraint appear in the schema.
- `ToolAnnotations` are exposed.
- The caller identity is injected.
- A token without `drop:write` neither sees nor can call the write tool.

The BDD test layout (nested `Describe*`/`When*` classes with `it_*` functions, fixtures shared
from outer classes) was checked with plain pytest and needs no plugin.

### Last run (2026-10-15)

```
42/42 passed · PASS × 4 · PASS × 6 · PASS × 7 · exit 0   (59 checks)
```

### Other checks

| Check | Result |
|---|---|
| Hermes plugin draft ([`spike/hermes-plugin-drop/`](spike/hermes-plugin-drop/)) through Hermes's `validate_plugin_dir` | All 14 admission checks pass, including the isolated `register()` probe, no built-in collisions and the security scan |
| Kubernetes example ([`spike/kubernetes-example.yaml`](spike/kubernetes-example.yaml)): `kubeconform -strict`, Kubernetes 1.35 | 9 valid, 1 skipped (ServiceMonitor CRD) |
| `hermes plugins validate` headless, with a pristine `HOME` and no gateway attached | 14 checks pass, "Validation passed.", exit 0 |

### `hermes plugins validate` on a CI runner

The M0 spike check (M5 depends on it). `python -I -m hermes_cli.main plugins validate <plugin-dir>`
runs with no configuration, no gateway and an empty `HOME`: the command is self-contained.

Two install paths do **not** work and should not be attempted in CI:

- PyPI `hermes-agent` (0.19.0) predates the subcommand — its `hermes plugins` offers only
  install, update, remove, list, enable and disable;
- `uv tool install git+https://github.com/NousResearch/hermes-agent` fails by design: the build
  backend refuses to build a wheel or an sdist ("Hermes is distributed via the shell installer,
  Docker image, or Nix").

What works is Hermes's own reusable composite action,
`NousResearch/hermes-agent/.github/actions/plugin-validate@<ref>`: shallow-clone the repo at that
ref, run `python3 -S scripts/ci/setup_toolchain.py prepare|install`, build the isolated validator
runtime with `pm.build_environment`, then run the command above against the plugin directory. Its
`hermes-ref` input pins the Hermes version the plugin is validated against, and Hermes's own catalog
CI uses the same machinery on `ubuntu-latest` (`hermes plugins validate --install-deps`).

Two constraints the action's machinery carries: the bootstrap `python3` must be 3.11 or newer (the
toolchain scripts use `datetime.UTC` and `X | Y` unions — `ubuntu-latest` satisfies this), and
`setup_toolchain.py` writes to `$GITHUB_OUTPUT`, `$GITHUB_ENV` and `$GITHUB_PATH`, so it runs only
inside a GitHub Actions job, not on a bare shell. Both are satisfied by the runner the action
targets; neither costs anything to depend on.

## 6. Not yet proven

These must be covered by the implementation's tests or by a first deployment.

- Log redaction, single-use upload URLs, quota, renderers, the expiry sweep and the config loader
  are specified but not in the spike.
- The plugin's multipart client has not been run against a real `/api/artifacts`, because the
  spike implements a simpler raw-body upload to prove auth and limits.
- Pod Security `restricted` admission and NetworkPolicy enforcement, which depend on the target
  cluster.
- The container image with a read-only root filesystem, and the multi-arch build.
