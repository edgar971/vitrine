# vitrine security model

The threat model, controls and residual risks for the [spec](spec.md) and [design](design.md).
Report a vulnerability through [`SECURITY.md`](../SECURITY.md), not an issue.

## 1. Assets

| Asset | Why it matters |
|---|---|
| Private artifact contents | Agent output often contains personal, business or infrastructure data |
| Client tokens | Write access and link minting |
| The signing secret | Anyone holding it can forge a private link to any artifact |
| Viewers' browsers | Agent-written HTML and SVG run in them |
| The network around vitrine | vitrine must not become a pivot point |

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

**Validated** rows are the controls the design spike proved first; each is a spec today, and the
cell names the closest one. **Spec** rows came after the spike, where the code is the evidence;
§ 6 lists their tests.

| # | Threat | Control | Status |
|---|---|---|---|
| T1 | A prompt-injected or careless agent publishes private data | Clients that never need public links get no `vitrine:publish` scope. Everything is created private. `artifact_set_visibility` is the only path to public, behind the human gate (`public_mode = confirm`), and adding content to an artifact that is already public goes through the same gate (T19). The gate fails closed on decline, timeout, unattended runs and clients without elicitation. No upload route takes a visibility. | Validated (both protocol eras, approve-only clients, decline, no support) |
| T2 | Unauthenticated MCP calls | Hashed tokens, constant-time comparison, 401 | Validated |
| T3 | Custom HTTP routes missing auth (the MCP auth layer covers only `/mcp`) | Each route authenticates itself, and a route-table test fails on any unclassified route | Validated (bypass demonstrated, then closed); route-table test in `tests/http/routes_spec.py` |
| T4 | Guessing ids, or learning that a private artifact exists | 80-bit ids. One indistinguishable 404 for missing, malformed and private ids and for bad links. No listing without a token. | Validated |
| T5 | Forging, transplanting or replaying links | HMAC with the kind, id, expiry, epoch and (for a recorded link) its `lid` inside the MAC. Epoch revocation, and per-link revocation by deleting the link's record. An `unlocked` signature is its own kind: it never verifies as a view link, cannot be unlocked again, and dies with its link's record or epoch. Single-use upload nonces, spent in the same write as the version. An upload never re-creates a deleted or expired id, whose old links would otherwise open the new content. | Validated; upload replay in `tests/http/uploads_spec.py`, re-creation in `tests/unit/services/artifacts_spec.py`, kinds in `tests/unit/services/links_spec.py` |
| T6 | Path traversal or symlink escape | One path rule for writing and reading; `resolve()` must stay inside the version root; symlinks refused | Validated (encoded and plain `..` variants) |
| T7 | Malicious HTML/SVG reads cookies, storage or acts as the viewer | CSP `sandbox` without `allow-same-origin` gives an opaque origin. `frame-ancestors 'none'`, `nosniff`, an extension allowlist, and unknown types forced to download. | Validated in Chromium |
| T8 | DNS rebinding from a browser on the same network | Host allowlist (421) and Origin check on `/mcp` (403) | Validated against uvicorn bound to `0.0.0.0` |
| T9 | Disk or memory exhaustion | Body limits enforced while streaming, per-version and total quotas, file-count limit | Validated (header and chunked) |
| T10 | Links leaking through `Referer`, caches or logs | `no-referrer`, `no-store` on private views, signed segments redacted in access logs (vitrine writes the access log; uvicorn's own is off) | Validated (`tests/http/logs_spec.py`) |
| T11 | A compromised vitrine process attacks its surroundings | vitrine never makes outbound requests (there is no fetch-from-URL feature). Runs non-root with a read-only root filesystem. The Kubernetes example denies all egress and mounts no service-account token. | Spec and example |
| T12 | A token leaks, or a client is over-privileged | One token per client, revoked by removing one hash; 256-bit tokens. **Scopes** (`vitrine:read`, `write`, `share`, `publish`): a tool outside the token's scopes is not even listed, and calling it fails as an unknown tool. What a revoked client already handed out is covered by T21. | Validated (scoped token cannot see or call a write tool) |
| T13 | The signing secret leaks | Rotating it revokes every private link. Accepted from the environment only, never from config files. | Spec |
| T14 | Supply chain | `uv.lock` with `--frozen` installs, `pip-audit` in CI, PyPI trusted publishing, a cosign-signed image with an SBOM and provenance. Every GitHub Action and both base images are pinned by digest; the release build uses no shared cache. Deployments should pin the image by digest. | `release.yml`; verification in [operations](operations.md) § 9. The examples pin a version tag until the first release has a digest. |
| T15 | Phishing pages hosted on the vitrine domain | Treat vitrine's hostname as untrusted content: a dedicated hostname with no other app's cookies, and `noindex` | Operator guidance |
| T16 | A looping or hijacked agent floods writes, links or views | Per-client token buckets for writes, links and reads; a per-IP bucket for views; rejected calls do no work. Quotas cap the damage that does get through. | Spec |
| T17 | Nobody can tell who published something, or when | An append-only audit log (fsync before acknowledging), with one event per state change, including refused publishes and denied calls. If the audit write fails, the change does not happen. | Spec |
| T18 | Retries create duplicate artifacts, or a reused key swaps content | `client_ref` is scoped per client and claimed with an exclusive file create. The same key with a different request is a conflict. A create that fails releases its key, so the retry is not locked out. | Spec |
| T19 | New content on an already public artifact goes live without the gate: a `write`-only token, `PUT /api/artifacts/{id}` or an upload URL adds a version, and the caller is told it is private | Adding a version to a public artifact needs an approving gate decision. Over MCP, `artifact_update` asks the human when the token holds `vitrine:publish`; otherwise, and on every HTTP route, it is refused (403) and audited as `publish_refused`. The store checks visibility under the artifact's lock, so a concurrent publish cannot slip a version past it. | Validated (`tests/mcp/public_updates_spec.py`, `tests/http/public_updates_spec.py`) |
| T20 | A gate answer is forged, replayed onto another question, or approves content the human never saw | On `2026-07-28` the question's `requestState` is signed over the artifact, the version shown, the client and the call's arguments; an answer without it, or with another request's, is declined (the SDK also seals the state to its request). On both eras the write must find the version the human was shown, else `conflict`. | Validated on both eras (`tests/mcp/public_updates_spec.py`) |
| T21 | A client's token is removed, but what it minted still works | Upload URLs carry their client and are refused once that client is gone or lost `vitrine:write`. Private view links are bearer capabilities: they last until they expire (`link_ttl_max`), their record is revoked (`artifact_revoke_links(link=lid)`), `artifact_revoke_links` or a visibility change bumps the epoch, or the secret is rotated. | Upload refusal in `tests/http/public_updates_spec.py`; view links are a documented limit |
| T22 | A forwarded or unfurled private link exposes the artifact: it is pasted into a ticket, or a chat unfurler (Slackbot, Discordbot, …) fetches it and shows a preview to the whole channel | A recorded link can carry a **passcode**: without it, every path answers 401 with vitrine's own form, which shows nothing from the artifact, so a preview shows the form. The passcode is kept as an scrypt hash only, never logged or audited; it is given to the user apart from the link. Wrong passcodes are rate-limited per link and address (`rate_unlock`) and per link (`rate_unlock_link`), and an empty bucket answers 429 without checking. The unlock `POST` refuses a foreign `Origin` and bodies over 1 KiB. One link can be revoked on its own, which ends its unlocked URLs too. | Validated (`tests/http/passcodes_spec.py`, `tests/browser/sandbox_spec.py`) |
| T23 | View counts track viewers | Counts and ids only: a visitor is an HMAC under a key that lives in memory and changes every hour, and only the number of distinct values is written. No IP address or user agent reaches disk. Views are not audit events, the files hold no content, months older than `view_retention` are deleted, and `view_log = off` records nothing. | Validated (`tests/http/view_counts_spec.py`, `tests/unit/services/views_spec.py`) |

## 4. Secrets

| Secret | Where it lives | Notes |
|---|---|---|
| `VITRINE_SECRET` | Server environment (a Kubernetes Secret, a Docker secret, …) | At least 32 bytes. `mcp-vitrine secret new` generates one. |
| `VITRINE_TOKENS` | Server environment | **Hashes only**, so a leak of the server's configuration reveals no usable token |
| Plaintext tokens | Each client's own configuration | `mcp-vitrine token new <name>` prints the token and its hash line, and writes nothing to disk |

`mcp-vitrine config check` prints the effective configuration with both secrets redacted.

## 5. Residual risks

| # | Risk | Mitigation and guidance |
|---|---|---|
| R1 | **Plain HTTP deployments** expose tokens and links to the network path | Use TLS everywhere except `127.0.0.1`. `config check` and `serve` warn when `public_base_url` is `http://` and the bind address is not loopback. |
| R2 | An agent with shell access can read its own token and call vitrine directly | No HTTP route can make anything public or change public content, so a `write` token is no path to public. But elicitation is answered by the MCP client, and the server cannot tell a human's answer from the client's: an agent that holds a `vitrine:publish` token *and* can speak MCP itself can approve its own publish. Give `vitrine:publish` only to clients whose host shows the question to a person and keeps the token out of the agent's reach. An out-of-band approval link (roadmap) removes this. |
| R3 | The human approves without reading | The prompt names the title, the file count and the consequence. A future option can add a short content preview. |
| R4 | Public URLs last until revoked: the id is the secret | That is the meaning of "anyone with the link". `artifact_set_visibility(private)` or `artifact_delete` removes access at the origin immediately; public views are `max-age=60`, so a shared cache or CDN may serve them for up to a minute longer. |
| R5 | A single replica means downtime on restart | Acceptable for the target scale; the `Store` seam is the path to more replicas |
| R6 | Protocol-era detection uses a private FastMCP method | Minor-version pin plus per-era tests (design § 12) |
| R7 | A passcode is only as private as the way it travels | A generated passcode passes through the MCP transcript, since the agent tells the user. An unlocked URL is itself a bearer capability that can be forwarded for up to `unlock_ttl`. A supplied passcode is only as strong as its owner makes it, and case and hyphens do not count. Keep `unlock_ttl` short where that matters, and revoke the link (`artifact_revoke_links(link=lid)`) to end every unlocked URL at once. |

## 6. The security specs

1. Every control in § 3, as a pytest spec.
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
13. **Public content:** no route or token without the gate adds a version to a public artifact;
    a forged, replayed or stale gate answer is declined (T19, T20).
14. **Client plugins:** a plugin's upload tool has no visibility parameter, and what it publishes
    is private (`tests/integrations/hermes_spec.py`).
15. **Passcodes:** a passcode link never serves content without its passcode, a wrong passcode
    never does either, an empty unlock bucket does not check the passcode, revoking a link ends
    its unlocked URLs, an unlocked signature is refused where a view one is expected, and the
    passcode never reaches a log (T22).
16. **Scheduled unpublish:** `/a/` answers 404 from `public_until` on, before the sweep, which
    then makes the artifact private and revokes its private links; the gate names the end date.
17. **Views:** no IP address or user agent is ever written, and `view_log = off` writes nothing
    (T23).
18. **Compensation:** undoing a change whose audit failed never undoes a change that landed beside
    it (`tests/unit/services/link_records_spec.py`).

### Invariants and the specs that hold them

Each invariant in [`AGENTS.md`](../AGENTS.md), named by its bold lead, has at least one spec
here. `tests/unit/invariants_spec.py`
fails when a listed spec is renamed or removed, so this table cannot point at nothing.

| Invariant | Spec |
|---|---|
| Private by default | `tests/mcp/tools_spec.py::DescribeArtifactCreate::WhenTheFilesAreValid::it_creates_a_private_artifact` |
| Private by default | `tests/http/api_spec.py::DescribeCreatingOverTheApi::WhenTheBodyIsValid::it_creates_a_private_artifact` |
| Private by default | `tests/http/uploads_spec.py::DescribeTheUploadRoute::WhenTheUrlIsValid::it_is_private` |
| Private by default | `tests/integrations/hermes_spec.py::DescribePublishingFiles::WhenAFolderIsPublished::it_leaves_the_artifact_private` |
| One path to public | `tests/mcp/gate_spec.py::DescribeThePublicGate::WhenTheHumanDeclines::it_leaves_the_artifact_private` |
| One path to public | `tests/mcp/gate_spec.py::DescribeThePublicGate::WhenTheClientCannotElicit::it_leaves_the_artifact_private` |
| One path to public | `tests/mcp/gate_spec.py::DescribeTheHandshakeEraGate::WhenTheHumanNeverAnswers::it_leaves_the_artifact_private` |
| One path to public | `tests/mcp/gate_spec.py::DescribeTheHandshakeEraGate::WhenTheClientFailsWhileAnswering::it_leaves_the_artifact_private` |
| One path to public | `tests/http/api_spec.py::DescribeCreatingOverTheApi::WhenAVisibilityIsSent::it_refuses_the_request` |
| One path to public | `tests/mcp/public_updates_spec.py::DescribeUpdatingAPublicArtifact::WhenTheTokenCannotPublish::it_writes_nothing` |
| One path to public | `tests/mcp/public_updates_spec.py::DescribeUpdatingAPublicArtifact::WhenTheHumanDeclines::it_writes_nothing` |
| One path to public | `tests/mcp/public_updates_spec.py::DescribeMakingPublicWhileTheContentChanges::WhenAVersionLandsWhileTheHumanDecides::it_leaves_the_artifact_private` |
| One path to public | `tests/mcp/public_updates_spec.py::DescribeAnAnswerTheServerNeverAskedFor::WhenItCarriesNoState::it_leaves_the_artifact_private` |
| One path to public | `tests/http/public_updates_spec.py::DescribeUpdatingAPublicArtifactOverTheApi::it_writes_nothing` |
| One path to public | `tests/http/public_updates_spec.py::DescribeUploadingToAPublicArtifact::it_writes_nothing` |
| One path to public | `tests/integrations/hermes_spec.py::DescribeRegistering::it_offers_no_way_to_ask_for_visibility` |
| Every HTTP route authenticates itself | `tests/http/routes_spec.py::DescribeTheRouteTable::WhenTheAppIsAssembled::it_classifies_every_route` |
| Every HTTP route authenticates itself | `tests/http/routes_spec.py::DescribeTheRouteTable::it_refuses_a_caller_without_credentials` |
| Signed link segments and tokens are never logged | `tests/http/logs_spec.py::DescribeTheAccessLog::WhenASignedLinkIsViewed::it_never_logs_the_signature` |
| Signed link segments and tokens are never logged | `tests/http/logs_spec.py::DescribeTheAccessLog::WhenAnUploadUrlIsUsed::it_never_logs_the_signature` |
| Signed link segments and tokens are never logged | `tests/http/logs_spec.py::DescribeTheAccessLog::WhenABearerTokenIsSent::it_never_logs_the_token` |
| Signed link segments and tokens are never logged | `tests/http/logs_spec.py::DescribeTheAccessLog::WhenAPasscodeIsPosted::it_never_logs_the_unlocked_signature` |
| Every state change is audited | `tests/mcp/audit_spec.py::DescribeTheAuditOfEachChange::WhenAnArtifactIsCreated::it_records_created_once` |
| Every state change is audited | `tests/unit/services/artifacts_spec.py::DescribeDelete::WhenTheAuditFails::it_keeps_the_artifact` |
| Every state change is audited | `tests/mcp/audit_spec.py::DescribeTheAuditOfEachChange::WhenTheTitleIsSecret::it_keeps_the_title_out_of_the_log` |
| Every state change is audited | `tests/unit/services/audit_spec.py::DescribeTheEventModel::WhenItIsInspected::it_holds_only_the_fields_the_spec_documents` |
| No `allow-same-origin` | `tests/http/policy_spec.py::DescribeTheSandbox::it_never_allows_the_same_origin` |
| No `allow-same-origin` | `tests/browser/sandbox_spec.py::DescribeTheSandboxInChromium::WhenAgentHtmlIsViewed::it_runs_in_an_opaque_origin` |
