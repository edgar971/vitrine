# drop security model

The threat model, controls and residual risks for the [spec](spec.md) and [design](design.md).
In the repository this document becomes `docs/security.md`, next to a `SECURITY.md` that explains
how to report vulnerabilities (GitHub private advisories) and which versions are supported.

## 1. Assets

| Asset | Why it matters |
|---|---|
| Private artifact contents | Agent output often contains personal, business or infrastructure data |
| Client tokens | Write access and link minting |
| The signing secret | Anyone holding it can forge a private link to any artifact |
| Viewers' browsers | Agent-written HTML and SVG run in them |
| The network around drop | drop must not become a pivot point |

## 2. Actors and trust boundaries

```
 untrusted                         semi-trusted                         trusted
┌─────────────────────┐   ┌──────────────────────────────────────┐   ┌────────────────────────┐
│ anyone with a URL   │   │ the model: prompt-injectable through │   │ the human answering    │
│ network neighbours  │   │ web pages, mail and files it reads   │   │ the confirmation       │
│ leaked links        │   │ MCP clients holding a token          │   │ the operator (config,  │
│                     │   │ artifact CONTENT, always             │   │ secrets)               │
└─────────────────────┘   └──────────────────────────────────────┘   └────────────────────────┘
      B1 view routes            B2 /mcp, /api (token)                   B3 public gate
```

- **B1:** a request gets exactly what its URL proves: a public id, or a valid signature.
- **B2:** a token proves *which client* is calling, not that a human wanted the action.
- **B3:** the only point where human intent is checked. It guards the one change whose effect
  can't be taken back once someone has seen the content: making it public.

## 3. Threats and controls

"Validated" refers to the spike checks in [`research.md`](research.md) § 5. "Spec" means the
control is specified and must ship with the tests listed in § 6.

| # | Threat | Control | Status |
|---|---|---|---|
| T1 | A prompt-injected or careless agent publishes private data | Clients that never need public links get no `drop:publish` scope. Everything is created private. `artifact_set_visibility` is the only path to public, behind the human gate (`public_mode = confirm`). The gate fails closed on decline, timeout, unattended runs and clients without elicitation. No upload route takes a visibility. | Validated (both protocol eras, approve-only clients, decline, no support) |
| T2 | Unauthenticated MCP calls | Hashed tokens, constant-time comparison, 401 | Validated |
| T3 | Custom HTTP routes missing auth (the MCP auth layer covers only `/mcp`) | Each route authenticates itself, and a route-table test fails on any unclassified route | Validated (bypass demonstrated, then closed); route-table test in `tests/http/routes_spec.py` |
| T4 | Guessing ids, or learning that a private artifact exists | 80-bit ids. One indistinguishable 404 for missing, malformed and private ids and for bad links. No listing without a token. | Validated |
| T5 | Forging, transplanting or replaying links | HMAC with the kind, id, expiry and epoch inside the MAC. Epoch revocation. Single-use upload nonces, spent in the same write as the version. An upload never re-creates a deleted or expired id, whose old links would otherwise open the new content. | Validated; upload replay and re-creation tested (`tests/http/uploads_spec.py`) |
| T6 | Path traversal or symlink escape | One path rule for writing and reading; `resolve()` must stay inside the version root; symlinks refused | Validated (encoded and plain `..` variants) |
| T7 | Malicious HTML/SVG reads cookies, storage or acts as the viewer | CSP `sandbox` without `allow-same-origin` gives an opaque origin. `frame-ancestors 'none'`, `nosniff`, an extension allowlist, and unknown types forced to download. | Validated in Chromium |
| T8 | DNS rebinding from a browser on the same network | Host allowlist (421) and Origin check on `/mcp` (403) | Validated against uvicorn bound to `0.0.0.0` |
| T9 | Disk or memory exhaustion | Body limits enforced while streaming, per-version and total quotas, file-count limit | Validated (header and chunked) |
| T10 | Links leaking through `Referer`, caches or logs | `no-referrer`, `no-store` on private views, signed segments redacted in access logs (drop writes the access log; uvicorn's own is off) | Validated (`tests/http/logs_spec.py`) |
| T11 | A compromised drop process attacks its surroundings | drop never makes outbound requests (there is no fetch-from-URL feature). Runs non-root with a read-only root filesystem. The Kubernetes example denies all egress and mounts no service-account token. | Spec and example |
| T12 | A token leaks, or a client is over-privileged | One token per client, revoked by removing one hash; 256-bit tokens. **Scopes** (`drop:read`, `write`, `share`, `publish`): a tool outside the token's scopes is not even listed, and calling it fails as an unknown tool. | Validated (scoped token cannot see or call a write tool) |
| T13 | The signing secret leaks | Rotating it revokes every private link. Accepted from the environment only, never from config files. | Spec |
| T14 | Supply chain | `uv.lock` with `--frozen` installs, `pip-audit` in CI, PyPI trusted publishing, a cosign-signed image with an SBOM, the image pinned by digest in deployments | Spec |
| T15 | Phishing pages hosted on the drop domain | Treat drop's hostname as untrusted content: a dedicated hostname with no other app's cookies, and `noindex` | Operator guidance |
| T16 | A looping or hijacked agent floods writes, links or views | Per-client token buckets for writes, links and reads; a per-IP bucket for views; rejected calls do no work. Quotas cap the damage that does get through. | Spec |
| T17 | Nobody can tell who published something, or when | An append-only audit log (fsync before acknowledging), with one event per state change, including refused publishes and denied calls. If the audit write fails, the change does not happen. | Spec |
| T18 | Retries create duplicate artifacts, or a reused key swaps content | `client_ref` is scoped per client and claimed with an exclusive file create. The same key with a different request is a conflict. | Spec |

## 4. Secrets

| Secret | Where it lives | Notes |
|---|---|---|
| `DROP_SECRET` | Server environment (a Kubernetes Secret, a Docker secret, …) | At least 32 bytes. `mcp-drop secret new` generates one. |
| `DROP_TOKENS` | Server environment | **Hashes only**, so a leak of the server's configuration reveals no usable token |
| Plaintext tokens | Each client's own configuration | `mcp-drop token new <name>` prints the token and its hash line, and writes nothing to disk |

`mcp-drop config check` prints the effective configuration with both secrets redacted.

## 5. Residual risks

| # | Risk | Mitigation and guidance |
|---|---|---|
| R1 | **Plain HTTP deployments** expose tokens and links to the network path | Use TLS everywhere except `127.0.0.1`. `config check` warns when `public_base_url` is `http://` and the bind address is not loopback. |
| R2 | An agent with shell access can read its own token and call the HTTP API directly | No HTTP route can make anything public, so the human gate still holds. An agent with shell and network access can exfiltrate data by other means anyway; drop is not the control for that. |
| R3 | The human approves without reading | The prompt names the title, the file count and the consequence. A future option can add a short content preview. |
| R4 | Public URLs last until revoked: the id is the secret | That is the meaning of "anyone with the link". `artifact_set_visibility(private)` or `artifact_delete` removes access immediately. |
| R5 | A single replica means downtime on restart | Acceptable for the target scale; the `Store` seam is the path to more replicas |
| R6 | Protocol-era detection uses a private FastMCP method | Minor-version pin plus per-era tests (design § 10) |

## 6. Security tests the implementation must ship

1. Every validated check in [`research.md`](research.md) § 5, as pytest.
2. **Route table:** enumerate the ASGI routes and assert each is `/mcp` (provider auth),
   bearer-checked, signature-checked, or on the explicit public list (`/a/…`, `/healthz`).
3. **Upload single use:** a second `PUT` with the same signature returns 409.
4. **Log redaction:** neither `/s/<sig>` nor `/u/<sig>` appears in captured access logs, and no
   token appears in any log.
5. **Expiry:** an expired artifact returns 410 and the sweep removes it.
6. **Quota and limits:** 507 over quota; 413 over `max_artifact`, `max_files` and the body limits.
7. **Renderers:** output carries the sandbox CSP, and Markdown raw HTML is escaped.
8. **Config:** secrets in the TOML file are rejected, unknown keys are rejected, and
   `unrestricted` logs a warning.
9. **Store contract suite:** runs against every `Store` implementation.
10. **Rate limits:** an exhausted bucket returns 429 or a tool error with retry-after, and writes
    nothing.
11. **Audit:**
    - every state change writes exactly one event;
    - no event contains a title, file name, signature or token;
    - a failing audit write aborts the change.
12. **Idempotency:** concurrent creates with one `client_ref` produce one artifact; a different
    request with the same key conflicts.
