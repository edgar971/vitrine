# drop specification

Version: draft 1 (targets `mcp-drop` 0.1). The keywords **MUST**, **MUST NOT**, **SHOULD** and
**MAY** are used as in RFC 2119. Everything marked *configurable* has a key in § 8.

## 1. Scope

drop stores files that agents publish and serves them to browsers. This spec defines:

- the data model and on-disk format;
- the MCP interface used by agents;
- the HTTP interface used by browsers and upload clients;
- configuration and the command line;
- limits, errors and observability.

How the pieces are built is in [`design.md`](design.md); why they are safe is in
[`security.md`](security.md).

Out of scope for 0.1: a web UI for owners, comments, analytics, password-protected links,
fetching content from URLs, and storage backends other than the filesystem.

## 2. Concepts

| Term | Definition |
|---|---|
| **Artifact** | A titled bundle of one or more files with one **entry** file (the page a link opens). Identified by an immutable **id**. |
| **Version** | An immutable snapshot of an artifact's files, numbered from 1. Updating creates the next version; links serve the current one unless a version is pinned. |
| **Visibility** | `private` (default) or `public`. |
| **Public URL** | `{base}/a/{id}/`. Anyone with it can view. The id is the secret; nothing lists public artifacts. |
| **Private link** | `{base}/s/{sig}/`. A signed capability with an expiry. Revocable. |
| **Link epoch** | A per-artifact counter inside every private link. Incrementing it revokes all private links issued before. |
| **Client** | An agent or tool holding an API token. Its **name** is recorded as the artifact's `source`. |
| **Base** | The configured public origin (`public_base_url`) used to build every URL. |
| **Client reference** | An optional key a client sends with `artifact_create` so a retried call returns the same artifact instead of a duplicate (§ 4.5). |
| **Audit log** | An append-only record of every state change: who did what to which artifact, and when (§ 11.3). |

## 3. Identifiers and signed links

**Artifact id**

- 16 characters from Crockford base32, lowercase (`0123456789abcdefghjkmnpqrstvwxyz`).
- Drawn from a CSPRNG, which gives 80 bits.
- Any string of another shape **MUST** be treated as not found, before any filesystem access.

**Signed link**

```
sig  = b64url(body) "." b64url(HMAC-SHA256(secret, body)[0:16])
body = "view"   "|" id "|" expires_unix "|" link_epoch                (private viewing)
     | "upload" "|" id "|" expires_unix "|" nonce "|" client       (signed upload URL)
```

- `b64url` is URL-safe base64 without padding.
- Verification **MUST** use a constant-time comparison.
- The `kind` is covered by the MAC, so a signature of one kind **MUST NOT** verify as another.
- An upload URL signs the client that asked for it. The upload is attributed to that client and
  spends that client's `rate_writes` bucket, whoever runs the `PUT`.
- A link that is tampered, expired, of the wrong kind, for another id or carrying a stale epoch
  **MUST** produce the same response as an unknown artifact (404).
- TTLs are *configurable*: default and maximum for view links and for upload URLs (§ 8).
- Duration strings use the form `<int><s|m|h|d>`, for example `15m` or `7d`.

## 4. Data model

### 4.1 On-disk layout

```
<data_dir>/
  artifacts/<id>/meta.json
  artifacts/<id>/v<n>/<files…>     immutable once in place
  refs/<client>/<sha256(ref)>      idempotency records (§ 4.5)
  audit/<YYYY-MM>.jsonl            append-only audit log (§ 11.3)
  tmp/                             staging; MUST be on the same filesystem as artifacts/
```

### 4.2 `meta.json`

```json
{
  "schema_version": 1,
  "id": "k7m2q9x4tz3wbn6r",
  "title": "Energy, September",
  "visibility": "private",
  "entry": "index.html",
  "version": 2,
  "versions": [
    {"n": 1, "at": "2026-10-15T10:00:00Z", "note": "", "bytes": 18234, "files": 1},
    {"n": 2, "at": "2026-10-15T10:20:00Z", "note": "added chart", "bytes": 90211, "files": 2}
  ],
  "created_at": "2026-10-15T10:00:00Z",
  "updated_at": "2026-10-15T10:20:00Z",
  "expires_at": null,
  "source": "hermes",
  "client_ref": "weekly-energy-2026-09",
  "tags": ["energy"],
  "link_epoch": 0,
  "used_upload_nonces": []
}
```

Implementations **MUST** refuse to start on a `schema_version` they do not know. Future versions
add migrations; they never reinterpret a field.

### 4.3 Rules

**File paths**

- Relative POSIX paths, at most 255 bytes.
- **MUST** be rejected if they contain an empty, `.` or `..` segment, a segment starting with
  `.`, a leading `/`, a backslash, or NUL.
- Files are regular files, never symlinks.

The same rule **MUST** apply when writing and when resolving a request path.

**Entry**

- If given, it **MUST** name a file in the version.
- If not given: keep the previous entry while it still exists, else `index.html` if present,
  else the only file. Otherwise it is an error.

**Retention**

- The newest `retain_versions` versions are kept (*configurable*, default 10).
- Older version folders are removed after a successful write.

**Expiry**

- After `expires_at`, every view returns `410 Gone`.
- A periodic sweep deletes expired artifacts (*configurable* interval).

**Write atomicity**

- A version becomes visible only once its folder is renamed into place and `meta.json` is
  atomically replaced to reference it.
- Readers **MUST NOT** observe a partial version.

### 4.4 Lifecycle

```
                    create / upload
                          │
                          ▼
   update ─────►  ┌──────────────┐   set_visibility(public) + gate approved   ┌──────────────┐
  (new version)   │   PRIVATE    │ ─────────────────────────────────────────► │    PUBLIC    │ ◄── update
                                                                                                + gate approved
   revoke_links   │ /s/{sig} only│ ◄───────────────────────────────────────── │ /a/{id} + /s │
   (epoch + 1)    └──────┬───────┘        set_visibility(private), epoch + 1  └──────┬───────┘
                         │ expires_at passes                                         │
                         ▼                                                           ▼
                  ┌──────────────┐   sweep                                  (same: EXPIRED)
                  │   EXPIRED    │ ───────► DELETED ◄─────── delete (from any state)
                  │  views → 410 │
                  └──────────────┘
```

Every arrow is one audit event (§ 11.3). Every visibility change increments the link epoch, so no
private link survives a round trip through public.

New content on a public artifact is public the moment it lands, so a version **MUST NOT** be added
to a public artifact without the gate (§ 6). Over MCP, `artifact_update` asks the human when the
token holds `drop:publish`; without that scope, and on every HTTP route, the write is refused
(`publish_refused`, `gate` = `unsupported`). The check runs under the same lock as the write, so a
publish that lands concurrently cannot slip a version past it.

### 4.5 Idempotent create

`artifact_create` and `POST /api/artifacts` accept an optional `client_ref`: at most 128
characters, from `[A-Za-z0-9._:-]`.

- The key is scoped to the calling client. Two clients may use the same `client_ref`
  independently.
- **First call:** the server atomically creates `refs/<client>/<sha256(client_ref)>` (exclusive
  create), recording the new id and a hash of the request. Then it creates the artifact.
- **Repeat call, same request:** within `ref_ttl` (*configurable*, default `24h`), it returns the
  existing artifact with `created: false` and a fresh link. No new version is written.
- **Repeat call, different request:** it **MUST** fail with a conflict. A key never silently
  maps to different content.
- **After `ref_ttl`,** or once the artifact is deleted, the key is free again. The sweep removes
  stale records.

The request hash covers `title`, `entry` and the files' paths and SHA-256 digests.

## 5. MCP interface

**Transport:** Streamable HTTP at `{base}/mcp`. Every request **MUST** carry
`Authorization: Bearer <token>`. The server sends the `instructions` text in § 5.4 and offers the
prompt in § 5.5.

### 5.0 Scopes

Each token carries scopes, and each tool requires one. A token that lacks a tool's scope does not
see the tool at all, and calling it fails as an unknown tool.

| Scope | Grants |
|---|---|
| `drop:read` | `artifact_get`, `artifact_list`, resources |
| `drop:write` | create, update, upload and delete |
| `drop:share` | `artifact_link`, `artifact_revoke_links` |
| `drop:publish` | `artifact_set_visibility`, which still passes the public gate (§ 6) |

The HTTP API applies the same scopes: `/api` writes need `drop:write`, and listing needs
`drop:read`.

### 5.1 Tools

| Tool | Parameters | Result | Scope |
|---|---|---|---|
| `artifact_create` | `title`; `files: {path: text}` (UTF-8, total ≤ `max_mcp_body`); `entry?`; `expires_in?`; `tags?`; `note?`; `client_ref?` (§ 4.5) | `{id, url, version, expires_at, created}`. Always **private**; `url` is a fresh private link. `created` is false when `client_ref` matched. | `drop:write` |
| `artifact_update` | `id`; `files?`; `merge=false`; `entry?`; `title?`; `expires_in?` (`"never"` clears); `tags?`; `note?` | `{id, url, version}`. The id is unchanged. With `merge`, files overlay the current version; without it they replace it. Without `files`, the current files carry over, so a metadata-only change is still a new version. On a **public** artifact the human is asked first (§ 6.1) and `url` is the public URL; a token without `drop:publish` is refused without asking (§ 4.4). | `drop:write` (+ `drop:publish` for a public artifact) |
| `artifact_get` | `id`; `version?` | Metadata plus `files: [{path, bytes}]` | `drop:read` |
| `artifact_list` | `query?` (title or tag substring); `tag?`; `visibility?`; `source?`; `limit=20` (max 100); `cursor?` | `{items: [{id, title, visibility, version, updated_at, source, tags}], next_cursor}`, newest first. **No URLs**: links are minted on purpose with `artifact_link`. | `drop:read` |
| `artifact_link` | `id`; `ttl?` | `{url, expires_at}`. A fresh private link, or the public URL if public. | `drop:share` |
| `artifact_set_visibility` | `id`; `visibility` | `{id, visibility, url}`. Going public follows § 6. Every change increments the link epoch. | `drop:publish` |
| `artifact_revoke_links` | `id` | `{id, link_epoch}` | `drop:share` |
| `artifact_upload_url` | `filename`; `id?`; `title?`; `ttl?` | `{id, put_url, expires_at, curl}`: a single-use signed upload URL (§ 7.3) for the artifact `id`, pre-allocated when none was given. `filename` and `title` ride unsigned in the URL's query: the signature fixes the id, the expiry and the nonce; the rest is the capability holder's to name. | `drop:write` |
| `artifact_delete` | `id` | `{deleted: id}` | `drop:write` |

Every tool also carries a `title` and all four MCP annotation hints (`readOnlyHint`,
`destructiveHint`, `idempotentHint`, `openWorldHint`); the values are listed in
[design § 4](design.md). Every parameter has a description and its constraints in the input
schema.

No tool other than `artifact_set_visibility` takes a visibility, and no HTTP route accepts one.
**There is exactly one path to public.**

### 5.2 Resources

| URI template | Content |
|---|---|
| `artifact://{id}` | `meta.json` plus the file list (JSON) |
| `artifact://{id}/{path}` | Text of one file in the current version. Text types only, up to 1 MB. Anything else returns an error naming the size and type. |
| `artifact://{id}/history` | This artifact's audit events, newest first (§ 11.3). An unknown id is `not found`. |

Clients use resources to read an artifact back before revising it. Every resource needs `drop:read`
and spends the `rate_reads` bucket. A file named `history` at an artifact's root is shadowed by the
history resource; read it with `GET /s/…` instead.

### 5.3 Policy carried in descriptions

Tool descriptions are part of this spec, because they are what the model reads.

- **`artifact_create` and `artifact_update`:** they **MUST** state that artifacts are private, and
  that revising should reuse the `id` so the link stays the same.
- **`artifact_set_visibility`:** it **MUST** state:
  - use it only when the user asked for a public link;
  - the user will be asked to confirm;
  - the agent should tell the user when a link is public.

### 5.4 Server instructions

The server **MUST** send this text as its MCP `instructions`. Clients that surface it give the
model the sharing policy once, for every tool. It is part of the contract; changing it is a
minor-version change.

```text
drop publishes files as links. Use it for anything the user should open in a browser,
revise later or pass on: HTML pages, reports, charts, PDFs, images.
- Everything you publish is private: links are signed and expire.
- To revise, call artifact_update with the same id; the link stays the same.
- Only call artifact_set_visibility(public) when the user asked for a public link.
  The user is asked to confirm. Say clearly when a link is public.
- Pass client_ref when a call may be retried, so a retry never creates a duplicate.
- Small images or files that only matter in this conversation: attach them instead.
```

### 5.5 Prompts

| Prompt | Arguments | Produces |
|---|---|---|
| `share` | `what` (a description of the output to share); `audience` = `private` (default) or `public` | A user message telling the agent to publish `what` with `artifact_create` (or `artifact_update` when it was published before), reply with the link, and, only when `audience` is `public`, call `artifact_set_visibility`. The confirmation still applies. |

The prompt gives clients that show prompts a one-click "share this". It grants nothing that the
tools and scopes don't already allow.

## 6. Public visibility

`public_mode` (*configurable*) decides how `artifact_set_visibility(id, "public")` behaves.

| `public_mode` | Behaviour |
|---|---|
| `confirm` (default) | A human **MUST** approve through MCP elicitation before anything changes (§ 6.1). |
| `disabled` | The call fails. Every link is a private signed link. |
| `unrestricted` | The change applies without asking. For single-user, fully trusted setups only; the server **MUST** log a warning at startup. |

### 6.1 Confirmation

The confirmation message **MUST** name the artifact title and its file count, and say that
anyone with the link can view it. When `artifact_update` asks before adding a version to a public
artifact, the message names the title, says it is public and that the new version is visible
immediately.

| Negotiated MCP protocol | Mechanism | Approved when |
|---|---|---|
| `2026-07-28` or later | The tool returns an `InputRequiredResult` holding one form elicitation `{confirm: boolean, default true}`. The client retries with the answer. | `action = accept` and `confirm` is not `false` |
| `2025-11-25` or earlier | The server sends `elicitation/create` with the same form | Same |

The default on `confirm` lets approve/deny-only clients approve: they answer `accept` with empty
content.

**Binding the answer to the question.** An approval counts only for the question that was asked:

- On `2026-07-28`, the `InputRequiredResult` carries a `requestState` that the server signs over
  the artifact id, the version shown, the calling client, a digest of the call's arguments and a
  15-minute expiry. A retry whose answer comes without that state, or with state minted for
  another artifact, client, version or set of arguments, is `declined`.
- On both eras, the write **MUST** find the version the human was shown. A version that landed
  while they decided makes the call fail with `conflict` ("changed while waiting"), and nothing
  becomes public.

The gate **MUST** fail closed. Decline, cancel, timeout, transport error and a client without
elicitation support all leave the artifact private and return a tool error saying so. A client that
did not declare the elicitation capability is refused without being asked (`unsupported`), and on
a handshake-era connection the server stops waiting after 10 minutes (`declined`), so a client that
never answers cannot hold the call open. Unattended
agents (scheduled jobs) therefore cannot publish publicly under `confirm`.

## 7. HTTP interface

Every route other than `/mcp` authenticates itself as listed here. The MCP auth layer does not
cover them.

### 7.1 Routes

| Method and path | Auth | Behaviour |
|---|---|---|
| `GET /a/{id}/[{path}]` | none | Public artifact, current version. A missing, private or malformed id returns 404. |
| `GET /a/{id}@{n}/[{path}]` | none | Pinned version `n` of a public artifact. A version that is not retained returns 404. |
| `GET /s/{sig}/[{path}]`, `GET /s/{sig}@{n}/[{path}]` | view signature | The same for any artifact the signature names |
| `PUT /u/{sig}?filename=[&title=]` | upload signature, from a client still configured with `drop:write` | Raw body. Creates the pre-allocated private artifact (`201`), or adds a version that overlays the file on the current ones (`200`). Single use. Returns `{id, url, version, expires_at}`. A public target returns 403 (§ 4.4). An URL whose client was removed from `DROP_TOKENS`, or lost `drop:write`, returns 404. |
| `POST /api/artifacts` | bearer, `drop:write` | `multipart/form-data`: one `file` part per file (the part's filename is its relative path), plus fields `title`, `entry?`, `expires_in?`, `tags?` (repeatable, or comma-separated), `note?`, `client_ref?`. Any other field is refused with 400, so there is no way to send a visibility. Returns `201 {id, url, version, expires_at}`, or `200` with the existing artifact when `client_ref` matched (§ 4.5). Always private. |
| `PUT /api/artifacts/{id}` | bearer, `drop:write` | Same body, without `client_ref`. Adds a version; `merge=true` overlays; `expires_in=never` clears the expiry. A public target returns 403 (§ 4.4). |
| `GET /api/artifacts` | bearer, `drop:read` | Same query parameters and page as `artifact_list` |
| `GET /healthz` | none | `200 {"ok": true}` when `data_dir` is writable (probed through `data_dir/tmp`, where every write starts), else 503 |
| `GET /metrics` on `metrics_port` | none | Prometheus text format, on a separate listener (§ 11) |

The `@{n}` suffix keeps version pinning inside one path segment, so relative asset URLs in
multi-file HTML keep working under every prefix.

Every `GET` route also answers `HEAD`. A view URL without its trailing slash redirects to it.
An empty `{path}` serves the entry; an entry in a subdirectory is redirected to (`307`, a
`./`-relative, percent-encoded `Location`), so its relative links resolve beside it. A pinned
older version opens its own entry: the current one while that version has it, otherwise the one
§ 4.3 would choose for it. A token without the route's scope gets 403, and the refusal is
audited (§ 11.3).

### 7.2 Viewing

**Query parameters**

- `?download=1` forces `Content-Disposition: attachment`.
- `?raw=1` bypasses renderers.

**Content type**

Set from an extension allowlist:

```
html htm css js mjs json md txt csv tsv xml png jpg jpeg gif webp avif svg ico pdf
mp4 webm mp3 wav ogg woff woff2
```

Anything else **MUST** be served as `application/octet-stream` with
`Content-Disposition: attachment`.

**Renderers** (*configurable* set, default all)

| Extension | Rendered as |
|---|---|
| `md` | CommonMark with raw HTML disabled |
| `csv`, `tsv` | Table |
| `json` | Indented, escaped |

All renderer output is HTML. A file over 2 MiB is served as itself rather than rendered.

**Headers on every artifact response**

| Header | Value |
|---|---|
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `no-referrer` |
| `X-Robots-Tag` | `noindex, nofollow` |
| `Cache-Control` | `private, no-store` under `/s/`; `public, max-age=60` under `/a/` |
| `Content-Security-Policy` | `sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads; frame-ancestors 'none'` |

The `Content-Security-Policy` header applies to HTML, SVG, XML (an XHTML namespace makes XML a
page) and renderer output. It **MUST NOT** include `allow-same-origin`.

Errors on a view are plain text and carry the same headers, with `no-store`. A malformed path
on a view is a 404 like any other missing file.

`Range` requests **SHOULD** be supported for media.

### 7.3 Uploads

**Request bodies**

- Bodies are limited **while streaming**, not only by `Content-Length`:
  - `max_upload` on `/api` and `/u`;
  - `max_mcp_body` on `/mcp`.
- Exceeding a limit returns 413.

**Upload URLs** (`artifact_upload_url`)

- The `id` is always fixed: it is pre-allocated when the caller doesn't give one.
- The nonce is recorded in `used_upload_nonces` (the last 50 are kept), in the same write as
  the version it lands, so two concurrent `PUT`s of one URL land at most once.
- A replay returns 409.
- An id that once held an artifact is never created again by an upload: if the artifact was
  deleted or expired after the URL was issued, the `PUT` returns 404. Re-creating it would make
  its old private links (epoch 0) open the new content.

### 7.4 Host and Origin

The server **MUST** reject requests whose `Host` is not in the allowed set with 421. The allowed
set is the host of `public_base_url` plus `allowed_hosts`; hosts compare without their port. It
**MUST** also reject browser requests to `/mcp` whose `Origin` is foreign, with 403. An `Origin`
is trusted when it is `public_base_url`'s origin or names a host in `allowed_hosts`; anything
else, `null` included, is foreign.

Health checks **MUST** send an allowed `Host`.

### 7.5 Rate limits

Token buckets in memory: per client for authenticated calls, per source IP for views.

| Bucket | Key | Default | Counts |
|---|---|---|---|
| `rate_writes` | client | `60/m` | create, update, upload URL issue, upload, delete |
| `rate_links` | client | `120/m` | `artifact_link`, `artifact_revoke_links`, `artifact_set_visibility` |
| `rate_reads` | client | `600/m` | get, list, resources |
| `rate_views` | source IP | `1200/m` | `/a/` and `/s/` requests |

Behaviour when a bucket is empty:

- MCP calls fail with `rate limited: retry after <n>s`.
- HTTP returns `429` with `Retry-After`.
- Rejected calls do no work and write nothing.

The source IP honours `X-Forwarded-For` only from `forwarded_allow_ips`. Setting a limit to
`off` disables it. Counters reset on restart, which is acceptable for a single process.

A refusal from a client's bucket is audited (§ 11.3). A refused view is not: a viewer has no
client name, and writing a line per refused request would turn a flood into disk writes.

## 8. Configuration

### 8.1 Sources and precedence

1. Command-line flags (`--host`, `--port`, `--config`)
2. Environment variables `DROP_<KEY>` (upper-case key)
3. A TOML file given with `--config` (top-level keys or a `[drop]` table)
4. Defaults

**Secrets** (`secret`, `tokens`) are accepted **only** from the environment. A config file that
sets them **MUST** be rejected at startup, so config files can be committed safely. Unknown keys
**MUST** be rejected, which catches typos, in the file and among `DROP_*` variables alike. The
client-side `DROP_URL` and `DROP_TOKEN` (what MCP clients and the Hermes plugin read) are not
settings and are ignored, so one shell can run the server and a client.

**Formats**

| Kind | Accepted forms |
|---|---|
| Sizes | `512KB`, `100MB`, `10GB` or bytes (binary units) |
| Durations | `30s`, `15m`, `1h`, `7d` or seconds; at most 12 digits, and positive |
| Lists in env vars | Comma-separated; native arrays in TOML |
| Rates | `<int>/<s\|m\|h>`, for example `60/m`, or `off` |

### 8.2 Reference

**Serving**

| Key | Default | Description |
|---|---|---|
| `data_dir` | `./data` | Storage root (§ 4.1) |
| `public_base_url` | `http://localhost:8080` | Origin used to build every URL. **MUST** be an origin, with no path. |
| `host` / `port` | `127.0.0.1` / `8080` | Listen address |
| `allowed_hosts` | `[]` | Extra `Host` values accepted besides the base URL's |
| `forwarded_allow_ips` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are trusted |

**Secrets** (environment only)

| Key | Default | Description |
|---|---|---|
| `secret` | — | HMAC key for links: at least 32 bytes, required. Rotating it revokes all private links. |
| `tokens` | — | Client tokens as `name:sha256hex[:scope+scope…]`, at least one required. Names are `[a-z0-9][a-z0-9_.-]{0,63}` and unique. Omitted scopes mean all four; `config check` warns about such entries. |

**Policy**

| Key | Default | Description |
|---|---|---|
| `public_mode` | `confirm` | `confirm`, `disabled` or `unrestricted` (§ 6) |
| `renderers` | `[md, csv, tsv, json]` | Renderers to enable; `[]` serves everything raw |

**Limits**

| Key | Default | Description |
|---|---|---|
| `max_upload` | `100MB` | Request body limit on `/api` and `/u` |
| `max_mcp_body` | `4MB` | Request body limit on `/mcp` (text artifacts ride in tool arguments) |
| `max_artifact` | `100MB` | Largest single version |
| `max_files` | `200` | Files per version |
| `quota` | `10GB` | Total storage. Writes beyond it fail with 507. |
| `retain_versions` | `10` | Versions kept per artifact |
| `ref_ttl` | `24h` | How long a `client_ref` stays bound to its artifact (§ 4.5) |
| `rate_writes` / `rate_links` / `rate_reads` | `60/m` / `120/m` / `600/m` | Per-client buckets (§ 7.5) |
| `rate_views` | `1200/m` | Per-IP bucket for views (§ 7.5) |

**Links**

| Key | Default | Description |
|---|---|---|
| `link_ttl` / `link_ttl_max` | `7d` / `90d` | Private link lifetime: default and ceiling |
| `upload_ttl` / `upload_ttl_max` | `15m` / `1h` | Upload URL lifetime: default and ceiling |

**Operations**

| Key | Default | Description |
|---|---|---|
| `sweep_interval` | `1h` | Expiry, staging, idempotency-record and audit-retention sweep period |
| `audit_retention` | `365d` | Audit files older than this are deleted by the sweep |
| `metrics_port` | `0` (off) | Separate Prometheus listener; needs the `metrics` extra |
| `log_level` | `INFO` | |
| `log_format` | `text` | `text` or `json` |

Validation at startup:

- `link_ttl ≤ link_ttl_max`;
- `upload_ttl ≤ upload_ttl_max`;
- `max_artifact ≤ quota`;
- `public_base_url` is an absolute `http(s)` origin;
- `data_dir` and `data_dir/tmp` are on one filesystem.

### 8.3 Example

```toml
# drop.toml: safe to commit; secrets come from DROP_SECRET and DROP_TOKENS
[drop]
data_dir        = "/var/lib/drop"
public_base_url = "https://drop.example.com"
host            = "0.0.0.0"
public_mode     = "confirm"
renderers       = ["md", "csv", "json"]
quota           = "20GB"
link_ttl        = "3d"
metrics_port    = 9090
log_format      = "json"
```

```bash
export DROP_SECRET="$(mcp-drop secret new)"
export DROP_TOKENS="hermes:<sha256>:read+write+share+publish,ci-bot:<sha256>:write"   # from `mcp-drop token new`
mcp-drop serve --config drop.toml
```

## 9. Command line

| Command | Purpose |
|---|---|
| `mcp-drop serve [--config FILE] [--host H] [--port P]` | Run the server. It refuses to start on data with another `schema_version` ([operations](operations.md) § 4), sweeps once before the first request, then every `sweep_interval`. |
| `mcp-drop config check [--config FILE]` | Validate the configuration and print the effective values, with secrets redacted. Exits non-zero on error. |
| `mcp-drop token new <name> [--scopes read,write,share,publish]` | Print a new random token, plus its `DROP_TOKENS` entry. Writes nothing to disk. |
| `mcp-drop token hash <token>` | Print the hash of an existing token |
| `mcp-drop secret new` | Print a new random 32-byte hex secret |
| `mcp-drop sweep [--config FILE]` | Run the expiry and staging sweep once, then exit (for cron-style setups). Safe beside a running server: staging younger than 10 minutes is left alone. |
| `mcp-drop audit [--id ID] [--client NAME] [--since 7d] [--json] [--config FILE]` | Print audit events, newest first; `--json` prints the stored lines |
| `mcp-drop backup <dest> [--config FILE]` | Write a consistent, restorable copy of `data_dir` ([operations](operations.md) § 2) |
| `mcp-drop migrate [--dry-run] [--config FILE]` | Upgrade `meta.json` files to the current `schema_version` ([operations](operations.md) § 4) |

Every command exits `0` on success, `1` when it failed, and `2` when the arguments or the
configuration are wrong. Commands that read `data_dir` load the same configuration as `serve`, so
they need `DROP_SECRET` and `DROP_TOKENS` too.

## 10. Errors

| Condition | MCP | HTTP |
|---|---|---|
| Missing or invalid token | 401 (transport) | 401 |
| Token without the scope | tool not listed; calling it fails as unknown | 403 |
| Unknown or malformed id; private id on `/a/`; invalid link | tool error `not found` | 404 |
| Expired artifact | tool error `expired` | 410 |
| Unsafe path; bad entry; empty artifact; a duration that is zero or over 36500 days | tool error naming the problem | 400 |
| Upload URL reused; `client_ref` reused with different content; the artifact changed while the human decided | tool error `conflict` | 409 |
| Rate limit exceeded | tool error `rate limited: retry after <n>s` | 429 + `Retry-After` |
| Over a size or file-count limit | tool error | 413 |
| Over quota | tool error | 507 |
| Public refused (§ 6) | tool error stating it stays private | — |
| A new version for a public artifact without the gate (§ 4.4) | tool error saying the artifact is public | 403 |
| Host not allowed / foreign Origin | — | 421 / 403 |

Error messages **MUST NOT** reveal whether a private artifact exists. A 5xx answers
`internal error` and never the underlying message, which can carry filesystem details. MCP does
the same: only the errors in this table reach a client in their own words; any other failure is a
tool error `internal error`.

## 11. Observability and audit

### 11.1 Logs

- One access line per request.
- Signed path segments (`/s/<sig>`, `/u/<sig>`) **MUST** be redacted (for example `/s/[redacted]/`)
  before logging, because they are credentials.
- Tokens are never logged.
- Public-gate decisions are logged with the artifact id, client name and outcome.

### 11.2 Metrics (`metrics_port`)

| Metric | Type | Labels |
|---|---|---|
| `drop_artifacts` | gauge | — |
| `drop_storage_bytes` | gauge | — |
| `drop_versions_created_total` | counter | `client` |
| `drop_views_total` | counter | `route` = `a` \| `s` |
| `drop_public_gate_total` | counter | `outcome` = `approved` \| `declined` \| `unsupported` \| `disabled` \| `not_required` (one per gate decision: making an artifact public, or adding a version to a public one) |
| `drop_rejected_total` | counter | `reason` = `rate_limited` \| `scope` |

Metrics **MUST NOT** carry artifact ids or titles.

### 11.3 Audit log

One JSON object per line in `audit/<YYYY-MM>.jsonl`, appended and flushed with `fsync` before the
state change is acknowledged.

```json
{"ts":"2026-10-15T10:21:04Z","event":"visibility_changed","artifact":"k7m2q9x4tz3wbn6r",
 "client":"hermes","from":"private","to":"public","gate":"approved"}
```

| Event | Extra fields |
|---|---|
| `created` | `version`, `files`, `bytes`, `client_ref` (when given) |
| `version_added` | `version`, `files`, `bytes`, `merge` (on a public artifact, only after the gate approved) |
| `visibility_changed` | `from`, `to`, `gate` = `approved` \| `not_required` |
| `publish_refused` | `gate` = `declined` \| `unsupported` \| `disabled` |
| `links_revoked` | `link_epoch` |
| `upload_url_issued` | `expires_at` |
| `deleted` | — |
| `expired` | — (written by the sweep) |
| `migrated` | `version` (the `schema_version` reached), `files` (how many `meta.json` files); no `artifact`; `client` = `@operator`. Written once per `mcp-drop migrate` run that changes anything, before the first file changes. |
| `access_denied` | `reason` = `scope` \| `rate_limited`; `tool`, or `route` as a route **name** (`upload`, `api_create`, …), never a path. A refused resource read is `tool` = `resources/read`. `artifact` is present only when the call named a well-formed id. |

Rules:

- The audit log records **who and what**, never content. It holds no titles, file names,
  signatures or tokens. Ids and client names only, so the log can be kept longer than the
  artifacts and shared with whoever reviews it.
- Files are never rewritten. Retention deletes whole months older than `audit_retention`.
- Audit write failures fail the operation (fail closed): a change that cannot be audited does not
  happen.
- A line that is not an event (a write torn by a crash) is skipped and logged when the log is
  read, and the next append starts on a fresh line.

## 12. Compatibility

| Area | Policy |
|---|---|
| Versioning | Semantic versioning. The MCP tool names and parameters, the HTTP routes, the config keys and `meta.json` are the public API. |
| MCP protocol | Both `2025-11-25` and `2026-07-28` clients are supported. The confirmation mechanism follows the negotiated version (§ 6.1). |
| Python | 3.15 and newer |
| On-disk format | `meta.json` carries `schema_version`. Upgrades migrate forward on `mcp-drop migrate`; the server refuses to start on data it would misread ([operations](operations.md) § 4). |
