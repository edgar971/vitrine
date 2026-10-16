# drop design

How the [spec](spec.md) is built. The aims, in order: **safe by default, simple, easy to run,
easy to extend.**

## 1. Principles

1. **One process, one port, one directory.** No database, no queue, no sidecar.
2. **The server owns its security.** Auth, link signing, the public gate, the rebinding guard and
   the sandbox headers all live in drop. A reverse proxy is optional.
3. **MCP first.** Every capability is an MCP tool. HTTP exists only for browsers and bulk upload.
4. **One gate to public.** It is a single code path, tested for each MCP protocol era, and it
   fails closed.
5. **Thin edges, plain core.** MCP tools and HTTP routes only translate. The behaviour lives in
   plain, framework-free services that are tested without a server.

## 2. Architecture

```
  MCP clients (Hermes, Claude Code, …)                     browsers
     │ MCP + Bearer        │ HTTP + Bearer / signed URL       │ GET /a/… /s/…
     ▼                     ▼                                  ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ ASGI middleware: proxy headers → redacting access log → Host/Origin → body   │
├────────────────────────────┬──────────────────────────────┬──────────────────┤
│ mcp/  (FastMCP)            │ web/  (Starlette routes)     │ metrics (own     │
│  LocalProviders: tools,    │  /a /s views, /api, /u,      │ port, optional)  │
│  resources, prompts;       │  /healthz                    │                  │
│  instructions; scopes      │                              │                  │
├────────────────────────────┴──────────────────────────────┴──────────────────┤
│ services/  ArtifactService · LinkService · PublicGate · AuditLog · Limits    │
├──────────────────────────────────────────────────────────────────────────────┤
│ domain/  models, errors      storage/  Store protocol → FileStore → data_dir │
└──────────────────────────────────────────────────────────────────────────────┘
```

## 3. Package layout

```
src/mcp_drop/
  app.py          build_app(settings) → one Starlette app: web routes + /mcp, one middleware
                  stack, and the lifespan that sweeps at startup and every sweep_interval
  cli.py          serve · config check · token · secret · sweep · audit · backup · migrate
  config.py       Settings (pydantic-settings): env + TOML, secrets env-only
  reference.py    docs/configuration.md, rendered from Settings (`just docs`)
  metrics.py      the optional metrics_port exporter
  domain/         models, errors, ids, media, paths, layout, clock — no framework, no I/O
  storage/        base (the Store protocol), filesystem (FileStore), schema, backup
  services/       artifacts, links, gate, audit, limits, tokens, metrics, maintenance, container
  mcp/            server, tools, resources, prompts, auth, deps, gate, middleware, policy, errors,
                  results, instructions
  web/            routes (with ROUTE_ACCESS), responses, renderers, middleware
```

`app.py` is the composition root of both edges; `services/container.py` composes the services they
are built from.

**Dependency direction:** `mcp`/`web` → `services` → `storage` → `domain`, and every layer may use
`config`.

- `services`, `storage` and `domain` never import FastMCP or Starlette.
- import-linter enforces this in CI.

## 4. MCP layer conventions

Each feature registers on a `LocalProvider` with the decorator. The server is assembled from
providers, so each feature's tools are tested in isolation.

```python
tools = LocalProvider()

@tools.tool(
    title="Create artifact",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                idempotent_hint=False, open_world_hint=False),
    tags={"write"},
    auth=require_scopes("drop:write"),
    timeout=30,
)
def artifact_create(
    title: Annotated[str, Field(description="Shown in listings and in the confirmation prompt.",
                                max_length=200)],
    files: Annotated[dict[str, str], Field(description="Relative path → UTF-8 text.")],
    entry: Annotated[str | None, Field(description="File the link opens.")] = None,
    service: ArtifactService = Depends(artifact_service),
    caller: AccessToken = CurrentAccessToken(),
) -> PublishedArtifact:
    """Publish text files as a PRIVATE artifact. Reuse the id with artifact_update to revise."""
    draft = ArtifactDraft(title=title, files={path: text.encode() for path, text in files.items()},
                          entry=entry)
    return service.create(draft, client=caller.client_id)

server = FastMCP("drop", instructions=INSTRUCTIONS, providers=[tools, resources, prompts],
                 auth=verifier)
```

These rules were validated against FastMCP 4.1 (see [`research.md`](research.md) § 3):

| Rule | Why |
|---|---|
| Use `@provider.tool(...)` on a `LocalProvider` per feature, not `@mcp.tool` on a global server | No import-time global; providers compose into `FastMCP(providers=[...])` and test alone |
| Set all four `ToolAnnotations` hints and a `title` on every tool | Clients use them for UI and approval policy; the defaults (destructive, open-world) are wrong for most of drop |
| Use `Annotated[T, Field(description=…, constraints)]` for every parameter | Descriptions and limits land in the JSON schema the model sees. No separate docs to drift. |
| Inject services with `Depends(...)` and the caller with `CurrentAccessToken()` | Injected parameters are hidden from the schema; no globals, and easy test overrides |
| Use per-tool `auth=require_scopes(...)` | A token without the scope does not even *see* the tool. Least privilege comes for free. |
| Return typed pydantic models | FastMCP derives the output schema, so clients get structured results |
| Domain errors become `ToolError` once, in one place (`@domain_errors`, a wrapper: FastMCP masks an unknown exception before middleware sees it) | Tools contain no try/except boilerplate, and messages never leak whether a private artifact exists |
| A tool's scope and rate bucket live in its `tags` | The edge middleware audits a scope refusal and spends the bucket from the same record `require_scopes` enforces |
| Keep sync tools sync | FastMCP runs them in a worker thread (`run_in_thread=True`), so file I/O never blocks the loop |
| Set a `timeout` on tools that touch disk | One slow disk cannot pin a worker forever |
| The public gate is the only `async` tool that awaits the client | It returns `InputRequiredResult` on `2026-07-28` connections (spec § 6.1) |

### Tool catalogue

| Tool | Title | Scope | read-only | destructive | idempotent | open-world |
|---|---|---|---|---|---|---|
| `artifact_create` | Create artifact | `drop:write` | no | no | no | no |
| `artifact_update` | Update artifact | `drop:write` | no | no | no | no |
| `artifact_get` | Get artifact | `drop:read` | yes | no | yes | no |
| `artifact_list` | List artifacts | `drop:read` | yes | no | yes | no |
| `artifact_link` | Create private link | `drop:share` | no | no | no | no |
| `artifact_set_visibility` | Change visibility | `drop:publish` | no | no | yes | **yes** |
| `artifact_revoke_links` | Revoke private links | `drop:share` | no | yes | no | no |
| `artifact_upload_url` | Create upload URL | `drop:write` | no | no | no | no |
| `artifact_delete` | Delete artifact | `drop:write` | no | **yes** | yes | no |

`set_visibility` is marked open-world because making something public exposes it beyond the
system.

## 5. Storage

### Write protocol

Each step runs under the artifact's lock.

```
1  stream each file into tmp/<uuid>/, enforcing the path, size and count limits
   merge=true: hard-link the current version's files into the stage first (immutable, safe)
2  choose the entry (spec § 4.3); refuse a version over max_artifact, and one that would push
   the bytes on disk (hard links counted once) past quota
3  rename tmp/<uuid> → artifacts/<id>/v<n>                         atomic, same filesystem
4  write tmp/<uuid>.json, os.replace → artifacts/<id>/meta.json      atomic
5  ArtifactService prunes versions beyond retain_versions, once the audit landed
```

**Crash outcomes**

- A crash before step 3 leaves garbage only in `tmp/`, which the startup sweep removes.
- A crash between steps 3 and 4 leaves an orphan `v<n>` that nothing references; the next write
  replaces it.

Readers take no lock. Versions are immutable and `meta.json` is replaced in one rename.

**Path resolution** runs the same way on every view:

```
valid id → meta → (public, or signature with the current epoch) → not expired → version exists
→ safe relative path or entry → resolves inside v<n>, regular file, not a symlink → serve
anything else → 404 (410 for expiry)
```

**Scale:** writers are serialised per artifact in one process. `list` scans `meta.json` files,
which is fine to roughly 10⁴ artifacts; an index would be internal to `FileStore`. More than one
replica needs another `Store`, not file locks.

## 6. Flows

Every mutating flow follows the same pipeline inside `ArtifactService`:

```
scope check (FastMCP) ─► rate limit ─► validate ─► idempotency (create only) ─► store write
                                                                   ─► audit append (fsync) ─► result
a failure at any step: nothing written, nothing audited except access_denied (scope, rate limit)
a change that landed but cannot be audited is undone, so a mutation is only acknowledged once it
is recorded; a deletion audits first, because a deleted artifact cannot be brought back
```

**Idempotent create**

```
create(client_ref) ─► open refs/<client>/<sha256(ref)> exclusively
     ├─ created   ─► write the artifact ─► fill in the ref record {id, request_hash} ─► created=true
     └─ exists    ─► same request_hash and ref still live ─► return that artifact, created=false
                     different request_hash             ─► Conflict (409)
```

```
publish text   client ─ artifact_create ─► service.create (private) ─► links.sign(view) ─► /s/<sig>/
publish files  plugin ─ POST /api/artifacts (multipart, Bearer) ─► service.create ─► 201
               agent  ─ artifact_upload_url ─► curl -T file put_url ─► service.add_version
go public      client ─ artifact_set_visibility ─► gate.confirm(mode, ctx)
                 2025-11-25: elicitation/create ─► human ─► accept | decline | timeout
                 2026-07-28: return InputRequiredResult ─► human ─► retried call with answer
               accept ─► visibility=public, epoch+1 ─► /a/<id>/     else ─► error, unchanged
view           browser ─ GET /s/<sig>/x ─► guard ─► verify ─► resolve ─► headers + body
```

## 7. Extension points

| Seam | Ships with | Natural next |
|---|---|---|
| `Store` protocol | `FileStore` | S3-compatible store for more than one replica |
| Renderer `(bytes, filename) -> html` | md, csv, tsv, json | ipynb, mermaid, diff |
| `TokenVerifier` | hashed static tokens with scopes | OAuth/OIDC (`RemoteAuthProvider`) for multi-user deployments |
| `PublicGate` | elicitation, two eras | Out-of-band approval link for clients without elicitation |
| Providers | `tools`, `resources`, `prompts` | Further prompts as their own provider |
| Audit sink | `audit/*.jsonl` files | Also forward events to syslog or a webhook |

New implementations run the same shared contract tests as the defaults (§ 9).

## 8. Integrations

**Any MCP client** (Claude Code shown):

```bash
claude mcp add --transport http drop https://drop.example.com/mcp \
  --header "Authorization: Bearer $DROP_TOKEN"
```

Each client gets its own token and scopes. Binaries go through `artifact_upload_url` and
`curl -T`.

**Hermes Agent** connects through `mcp_servers`, with `${VAR}` interpolated from
`~/.hermes/.env`. It routes the public-gate elicitation to its approve/deny UI (CLI, Telegram,
Slack, …), and fails closed for cron jobs.

```yaml
mcp_servers:
  drop:
    url: "https://drop.example.com/mcp"
    headers: { Authorization: "Bearer ${DROP_TOKEN}" }
    elicitation: { timeout: 300 }
plugins:
  enabled: [drop]
```

The optional **Hermes plugin** (`integrations/hermes/drop/`) adds:

- `artifact_publish_files(paths, …)`, which streams local files to `/api/artifacts`, always
  private;
- a `drop:drop` skill;
- the `/share` and `/artifacts` commands.

It has no visibility parameter and no `ctx.call_mcp` grant. It uses only the standard library
(`http.client`), so it adds nothing to Hermes's environment and runs on every Python Hermes
supports (3.11+). When drop refuses an upload before reading it (a wrong token, a missing scope,
a rate limit, an oversized body), the client still reads the refusal, or names those causes when
the connection was reset first. CI runs `hermes plugins validate` on it at a pinned Hermes commit,
and `tests/integrations/hermes_spec.py` drives a real server through it.

## 9. Test conventions

Tests read as specifications: `Describe*`/`When*` classes holding `it_*` functions are the only
names pytest collects (`pyproject.toml`), so a name states one observable outcome and the suite
reads as the behaviour it holds.

```python
class DescribeArtifactCreate:
    class WhenTheCallerHasNoWriteScope:
        def it_does_not_list_the_tool(self, read_only_client): ...

    class WhenFilesAreValid:
        def it_creates_a_private_artifact(self, service): ...
        def it_returns_a_signed_link(self, service): ...

    class WhenAPathEscapesTheArtifact:
        def it_refuses_the_whole_version(self, service): ...
```

- **Describe** the unit (a service, a tool, a route). **When** names the situation (the given).
  **it_** states one observable outcome (the then).
- Fixtures are the givens and live in the nearest `conftest.py`. Outer-class fixtures are visible
  to nested classes.
- One behaviour per `it_`, arranged as given / when / then, with no logic in tests.

**Where the specs live**

```
tests/
  conftest.py   shared givens: settings, a temporary data_dir, tokens, clients
  unit/         domain, services, storage, mcp policy/auth/errors, web, config — no server
  contract/     one suite parametrised over every Store implementation
  mcp/          tool behaviour via FastMCP's in-memory Client (schemas, annotations, gate)
  http/         real uvicorn on a free port: auth and scopes, headers, guard, limits, logs
  cli/          the mcp-drop commands, in-process against a temporary data_dir
  integrations/ client plugins (Hermes) driving a real server
  browser/      Playwright Chromium, marker "browser"; its own CI job
```

## 10. CI and releases

The sources of truth for the tooling are `pyproject.toml` (uv, ruff, mypy, import-linter, pytest
and the coverage gate), the `justfile` (one recipe per gate; `just check` runs them all),
pre-commit and `.github/workflows/`. This section does not restate them.

**CI** runs on Python 3.15: `just check`, then a smoke test of the built wheel
(`just wheel-test`), the browser specs, `pip-audit`, a multi-arch image build that pushes
nothing, and `hermes plugins validate` on the Hermes plugin. On a pull request a `commits` job
checks that every commit is a Conventional Commit.

**Release** runs on a `v*` tag (`release.yml`): every CI job on the tagged commit (reusing
`ci.yml`), a PEP 440 check against `pyproject.toml`, the image for amd64 and arm64 with an SBOM,
a provenance attestation and a keyless cosign signature (no `latest` tag — deployments pin a
version or a digest), then `uv build` and trusted publishing to TestPyPI for a pre-release and to
PyPI for a final version, then a GitHub release with notes from the changelog. The image goes
first, because a PyPI version can never be uploaded twice. Rehearse with a pre-release; the
procedure is CONTRIBUTING.md § Releasing.

**Container image:** the `Dockerfile` — CPython 3.15 on `debian:trixie-slim`, user 10001, a
read-only root filesystem, everything it writes under `/data`. Running it, and what to check
before exposing drop, is [operations](operations.md) § 9.

**ADRs:** `docs/adr/` records the decisions that are expensive to reverse.

## 11. Deployment

Local `uvx mcp-drop serve`, a container, Compose with Caddy for TLS, or the Kubernetes manifests:
[operations](operations.md) § 9 has each one, the image's run flags, and what to check before
exposing drop to the internet, and [`examples/`](../examples/) holds the deployment shapes.

## 12. Compatibility risks

| Risk | Handling |
|---|---|
| FastMCP exposes the protocol era only privately | Pin `fastmcp>=4.1,<4.2`; per-era gate tests fail CI on change |
| Clients answer elicitation differently (approve-only or with form data) | Decide on `action`; `confirm` defaults to true; an explicit `false` declines |
| Clients move to `2026-07-28` | Both branches are implemented and tested |
