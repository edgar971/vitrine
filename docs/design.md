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
│ ASGI middleware: Host/Origin guard → body limit → redacting access log       │
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
  __main__.py            python -m mcp_drop → cli
  cli.py                 serve · config check · token new|hash · secret new · sweep
  config.py              Settings (pydantic-settings): env + TOML, validation
  app.py                 build_app(settings) → ASGI app; lifespan wires services
  domain/
    models.py            Artifact, Version, Visibility, LinkClaims (pydantic, frozen)
    errors.py            DropError → NotFound, Expired, TooLarge, OverQuota, Conflict, Refused
  storage/
    base.py              Store protocol
    filesystem.py        FileStore: write protocol, retention, quota, path resolution, sweeps
  services/
    artifacts.py         ArtifactService: create, update, get, list, delete, set_visibility
    links.py             LinkService: ids, sign/verify view and upload links
    gate.py              PublicGate: public_mode + era-aware confirmation
    audit.py             AuditLog: append (fsync) and query; retention sweep
    limits.py            RateLimiter: token buckets per client and per IP
  mcp/
    server.py            build_mcp(settings) → FastMCP(providers=[…], auth=…)
    auth.py              HashedTokenVerifier, scopes
    deps.py              Depends() providers: artifact_service(), link_service(), gate()
    tools.py             LocalProvider `tools`: the nine artifact_* tools
    resources.py         LocalProvider `resources`: artifact://… and artifact://{id}/history
    prompts.py           LocalProvider `prompts`: share
    instructions.py      the server instructions text (spec § 5.4), one constant
  web/
    routes.py            view, upload, api and health handlers
    responses.py         MIME allowlist, security headers, renderer dispatch
    renderers.py         md, csv, tsv, json
    middleware.py        body limit, redacting access log
```

**Dependency direction:** `mcp`/`web` → `services` → `storage`/`domain`, and every layer may use
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
    return service.create(title=title, files=files, entry=entry, source=caller.client_id)

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
| Domain errors become `ToolError` once, in one place | Tools contain no try/except boilerplate, and messages never leak whether a private artifact exists |
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
1  stream each file into tmp/<uuid>/ (open "xb"), enforcing path, size and count limits
   merge=true: hard-link the current version's files into the stage first (immutable, safe)
2  choose entry; check quota against cached usage + staged bytes
3  rename tmp/<uuid> → artifacts/<id>/v<n>                         atomic, same filesystem
4  write tmp/<uuid>.json, os.replace → artifacts/<id>/meta.json      atomic
5  prune versions beyond retain_versions
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

It has no visibility parameter and no `ctx.call_mcp` grant. The draft passes
`hermes plugins validate` ([`spike/hermes-plugin-drop/`](spike/hermes-plugin-drop/)).

## 9. Code and test conventions

### Code

**Language and structure**

- Python 3.15+ (`requires-python = ">=3.15"`), the latest stable release.
- Full type hints; `mypy --strict` clean.
- `ruff` (lint and format) is the style; no style debates in review.
- Small modules with one job each. Functions do one thing; no function needs a comment to be
  understood.

**Data and dependencies**

- Pydantic models at the edges, frozen dataclasses or models inside.
- No `dict[str, Any]` passed between layers.
- Composition over inheritance. Protocols for seams; no abstract base classes without a second
  implementation.
- No globals: dependencies are passed in or injected.

**Comments and docstrings**

- Docstrings on public functions and tools: one line, plus `Args`/`Raises` only when not obvious.
  A tool's docstring is its description for the model.
- Comments explain *why*, never *what*. Delete any comment that restates the code.

**Errors**

- Domain errors are typed (`domain/errors.py`) and mapped once per edge (MCP and HTTP).
- No bare `except`.
- No error message reveals whether a private artifact exists.

### Tests: BDD with classes

Tests read as specifications. pytest collects nested classes and `it_*` functions:

```toml
[tool.pytest.ini_options]
python_files     = ["*_spec.py"]
python_classes   = ["Describe*", "When*"]
python_functions = ["it_*"]
```

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
- Validated with plain pytest; no BDD plugin is needed.

**Test layout**

```
tests/
  conftest.py                  shared givens: settings, tmp data_dir, tokens, clients
  unit/        *_spec.py       domain, links, storage, services, renderers, config (no server)
  contract/    store_spec.py   one suite parametrised over every Store implementation
  mcp/         *_spec.py       tool behaviour via FastMCP's in-memory Client (schemas, annotations, gate)
  http/        *_spec.py       real uvicorn on a free port: auth and scopes, headers, guard, limits
  browser/     *_spec.py       Playwright Chromium, marker "browser" (sandbox isolation)
```

## 10. Repository and tooling

```
drop/                          github.com/edgar971/drop · PyPI and CLI: mcp-drop
  AGENTS.md  CLAUDE.md         agent guidance (§ 11)
  README.md  CHANGELOG.md  CONTRIBUTING.md  SECURITY.md  LICENSE (MIT)
  pyproject.toml  uv.lock      one source for metadata, dependencies and tool config
  src/mcp_drop/  tests/        § 3, § 9
  docs/                        spec, design, security, configuration (generated), adr/
  integrations/hermes/drop/    Hermes plugin
  examples/                    drop.toml, compose.yaml, kubernetes/
  Dockerfile  .dockerignore
  .github/                     workflows/{ci,release}.yml, dependabot.yml, ISSUE_TEMPLATE/
  .pre-commit-config.yaml
```

### Tools

| Concern | Tool |
|---|---|
| Environments, locking, running, building | **uv** (`uv sync`, `uv run`, `uv lock --check`, `uv_build`) |
| Lint and format | **ruff**: `E F I UP B SIM RUF ANN ASYNC C4 PTH RET TID S D PL PT N`, Google docstrings, no relative imports |
| Types | **mypy** `--strict` with the pydantic plugin; `py.typed` shipped |
| Layering | **import-linter** (§ 3) |
| Tests | **pytest**, pytest-asyncio, pytest-cov (90% gate), Playwright for the `browser` marker |
| Supply chain | `pip-audit` via `uvx`, Dependabot for uv and Actions, ruff `S` rules |
| Hooks | pre-commit: the same gates as CI |
| Task runner | `just` (`just check` runs every gate; `just dev` serves with reload) |

### CI and releases

**CI** runs on Python 3.15: lock check, ruff, mypy, import-linter, pytest with
coverage, browser tests, `pip-audit`, and a multi-arch image build without pushing.

**Release** runs on a `v*` tag:

1. `uv build` publishes to PyPI by trusted publishing.
2. `ghcr.io/edgar971/drop:<version>` is pushed for amd64 and arm64, cosign-signed, with an SBOM.
3. Release notes come from `CHANGELOG.md` (Keep a Changelog).

Commits follow Conventional Commits.

**Container image**

- Built with `uv sync --frozen --no-dev`, from a `python:3.15-slim` base.
- Runs as user 10001 with a read-only root filesystem; writes only to `/data` and `/tmp`.
- `ENTRYPOINT ["mcp-drop", "serve"]`.

**ADRs:** `docs/adr/` records the decisions that are expensive to reverse. The first four are:
the filesystem store, one gate to public, self-contained auth, and the protocol-era handling.

## 11. Agent guidance files

`AGENTS.md` is the single source for any coding agent. `CLAUDE.md` imports it and adds only
Claude Code specifics, so the two cannot drift.

**AGENTS.md**

- Kept under 100 lines.
- Every rule has a reason; it never restates what ruff or mypy already enforce.
- Sections:
  1. What drop is (two lines) and where the spec lives.
  2. Commands: `uv sync`, `just check`, `uv run pytest -k …`, `uv run mcp-drop serve`.
  3. Layout and the layering rule.
  4. Conventions with reasons: the MCP decorator rules (§ 4), BDD test naming (§ 9), errors,
     no comments that restate code.
  5. Invariants that must never break:
     - private by default;
     - one path to public;
     - every HTTP route authenticates itself;
     - signed segments never logged;
     - every state change audited, and no content in the audit log;
     - no `allow-same-origin`.
  6. Definition of done: `just check` green; new behaviour has an `it_`; the spec and docs updated
     if the contract changed.

**CLAUDE.md**

```markdown
@AGENTS.md

## Claude Code
- Hooks (.claude/settings.json): ruff format + ruff check on edited .py files.
- Rules (.claude/rules/): mcp.md loads for src/mcp_drop/mcp/**; tests.md for tests/**.
```

`.claude/rules/` holds only path-scoped gotchas, such as tool annotations and the protocol-era
gate for `mcp/`, and the BDD naming for `tests/`. Nothing in it restates `AGENTS.md`.

## 12. Deployment options

| Option | How | Fits |
|---|---|---|
| Local | `uvx mcp-drop serve` | One agent on one machine, bound to `127.0.0.1` |
| Container | `docker run -v drop:/data -e DROP_SECRET -e DROP_TOKENS -p 8080:8080 ghcr.io/edgar971/drop` | Any Docker host |
| Compose | `examples/compose.yaml` (with Caddy for TLS) | Small servers |
| Kubernetes | `examples/kubernetes/` | Clusters |

**The Kubernetes example**

- Pod Security `restricted`; a single-replica `Recreate` deployment; non-root, read-only root
  filesystem; no service-account token.
- Probes send an allowed `Host` header.
- Storage is an RWO PVC.
- A default-deny NetworkPolicy with **no egress**; ingress only from the ingress controller and
  the metrics scraper.

**Before exposing drop to the internet:**

- terminate TLS;
- use a dedicated hostname that carries no other application's cookies;
- optionally block `/mcp`, `/api` and `/u` at the proxy.

## 13. Compatibility risks

| Risk | Handling |
|---|---|
| FastMCP exposes the protocol era only privately | Pin `fastmcp>=4.1,<4.2`; per-era gate tests fail CI on change |
| Clients answer elicitation differently (approve-only or with form data) | Decide on `action`; `confirm` defaults to true; an explicit `false` declines |
| Clients move to `2026-07-28` | Both branches are implemented and tested |
