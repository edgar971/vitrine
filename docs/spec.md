# vitrine specification

Version: 0.1 (the contract `mcp-vitrine` 0.1 implements). The keywords **MUST**, **MUST NOT**, **SHOULD** and
**MAY** are used as in RFC 2119. Everything marked *configurable* has a key in § 8.

## 1. Scope

vitrine stores files that agents publish and serves them to browsers. This spec defines:

- the data model and on-disk format;
- the MCP interface used by agents;
- the HTTP interface used by browsers and upload clients;
- configuration and the command line;
- limits, errors and observability.

How the pieces are built is in [`design.md`](design.md); why they are safe is in
[`security.md`](security.md).

Out of scope: a web UI for owners, comments, fetching content from URLs, emailed one-time codes,
and storage backends other than the filesystem. Analytics are anonymous view counts only (§ 11.4).

## 2. Concepts

| Term | Definition |
|---|---|
| **Artifact** | A titled bundle of one or more files with one **entry** file (the page a link opens). Identified by an immutable **id**. |
| **Version** | An immutable snapshot of an artifact's files, numbered from 1. Updating creates the next version; links serve the current one unless a version is pinned. |
| **Visibility** | `private` (default) or `public`. |
| **Public URL** | `{base}/a/{id}/`. Anyone with it can view. The id is the secret; nothing lists public artifacts. |
| **Private link** | `{base}/s/{sig}/`. A signed capability with an expiry. Revocable. |
| **Recorded link** | A private link `artifact_link` minted: it carries a **link id** (`lid`) and lives as a record in `meta.json`, so it can carry a **passcode**, be revoked on its own and have its views counted. |
| **Link epoch** | A per-artifact counter inside every private link. Incrementing it revokes all private links issued before. |
| **Public window** | `public_until`: when a public artifact goes private again on its own (§ 4.3). |
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
body = "view"     "|" id "|" expires_unix "|" link_epoch [ "|" lid ]   (private viewing)
     | "unlocked" "|" id "|" expires_unix "|" link_epoch "|" lid       (a passcode was given)
     | "upload"   "|" id "|" expires_unix "|" nonce "|" client         (signed upload URL)
```

- `b64url` is URL-safe base64 without padding. A `lid` is 8 random bytes in `b64url`.
- Verification **MUST** use a constant-time comparison.
- The `kind` is covered by the MAC, so a signature of one kind **MUST NOT** verify as another.
- An upload URL signs the client that asked for it. The upload is attributed to that client and
  spends that client's `rate_writes` bucket, whoever runs the `PUT`.
- A link that is tampered, expired, of the wrong kind, for another id or carrying a stale epoch
  **MUST** produce the same response as an unknown artifact (404).
- **Recorded links.** Every link `artifact_link` mints carries a `lid` and has a record in
  `meta.json` (§ 4.2). It verifies only while that record exists, so deleting the record revokes
  that one link. The link a create or an update returns carries no `lid`: it is the author's
  working link, revoked by the epoch only. Links minted before 0.2 (no `lid`) verify as before.
- An artifact holds at most `max_links` live records (*configurable*, default `500`); minting
  past that fails with "revoke links first". Expired records are dropped first, and by the sweep.
- **Passcodes.** A recorded link may carry a passcode. Its record holds only the scrypt hash
  (`hashlib.scrypt`, `n=2**15, r=8, p=1`, a 16-byte salt). Case and hyphens are ignored, so
  `ABCDE-fghjk` and `abcdefghjk` are one passcode. The passcode **MUST NOT** appear in the audit
  log, the access log, metrics or any error. It cannot be changed: mint a new link instead.
- **Unlocked signatures.** A right passcode earns an `unlocked` signature (§ 7.1), valid until
  the earlier of `unlock_ttl` (*configurable*, default `12h`) and the link's own expiry, and only
  while the link's record exists and the epoch matches. `unlocked` never verifies as `view`, and
  `view` never as `unlocked`.
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
  views/<YYYY-MM>.jsonl            anonymous view counts (§ 11.4)
  tmp/                             staging; MUST be on the same filesystem as artifacts/
```

### 4.2 `meta.json`

```json
{
  "schema_version": 2,
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
  "project": "household",
  "link_epoch": 0,
  "used_upload_nonces": [],
  "links": [],
  "fallback": null,
  "public_until": null
}
```

| Field | Type | Default | Used by |
|---|---|---|---|
| `project` | `null` \| a name matching `[a-z0-9][a-z0-9_.-]{0,63}` | `null` | projects: one per artifact; tags stay many |
| `links` | `[{lid, label, created_at, expires_at, passcode}]`, one per recorded link; `passcode` is `null` or `{hash, salt, n, r, p}` (scrypt) | `[]` | per-link passcode, revoke and view counts (§ 3) |
| `fallback` | `null` \| a path in the version | `null` | zip sites, SPA fallback |
| `public_until` | `null` \| timestamp | `null` | scheduled unpublish (§ 4.3) |

A field whose feature is not enabled keeps its default and has no effect. `meta.json` holds no
secret in clear and nothing about viewers: no IPs, no passcodes. A link `label` is a name the user
chose, like a title, and never reaches the audit log.

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

**Scheduled unpublish**

- `public_until` ends a public window: `artifact_set_visibility(id, "public", public_for)` sets it
  to now + `public_for`. `public_ttl_max` (*configurable*, default `off`) caps every window, a
  missing `public_for` included, so with it set nothing stays public for ever.
- It is **enforced at request time**: once `public_until` passes, `/a/{id}/…` **MUST** answer
  404, as for any private id, even before the sweep runs.
- The sweep then makes the artifact private: `visibility = private`, `public_until = null`, link
  epoch + 1, every link record dropped. It audits `visibility_changed` with `client = "@sweep"`
  and `gate = "scheduled"`. The artifact and its history are kept.
- **Widening needs the gate.** Moving `public_until` later or clearing it is only possible through
  `artifact_set_visibility(public, …)`, which asks the human again (§ 6.1) and keeps the links.
  Going private early is `artifact_set_visibility(private)`.

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
   (epoch + 1, or │              │        set_visibility(private), epoch + 1  │              │
    one lid)      │              │ ◄───────────────────────────────────────── │              │ ◄── set_visibility(
                  │              │  sweep after public_until, epoch + 1       │              │     public, public_for)
                  └──────┬───────┘  (/a/ is 404 from public_until on)         └──────┬───────┘     + gate approved
                         │ expires_at passes                                         │
                         ▼                                                           ▼
                  ┌──────────────┐   sweep                                  (same: EXPIRED)
                  │   EXPIRED    │ ───────► DELETED ◄─────── delete (from any state)
                  │  views → 410 │
                  └──────────────┘
```

Every arrow is one audit event (§ 11.3). Every visibility change increments the link epoch and
drops every link record, so no private link survives a round trip through public.

New content on a public artifact is public the moment it lands, so a version **MUST NOT** be added
to a public artifact without the gate (§ 6). Over MCP, `artifact_update` asks the human when the
token holds `vitrine:publish`; without that scope, and on every HTTP route, the write is refused
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
| `vitrine:read` | `artifact_get`, `artifact_list`, resources |
| `vitrine:write` | create, update, upload and delete |
| `vitrine:share` | `artifact_link`, `artifact_revoke_links` |
| `vitrine:publish` | `artifact_set_visibility`, which still passes the public gate (§ 6) |

The HTTP API applies the same scopes: `/api` writes need `vitrine:write`, and listing needs
`vitrine:read`.

### 5.1 Tools

| Tool | Parameters | Result | Scope |
|---|---|---|---|
| `artifact_create` | `title`; `files: {path: text}` (UTF-8, total ≤ `max_mcp_body`); `entry?`; `expires_in?`; `tags?`; `project?`; `note?`; `client_ref?` (§ 4.5) | `{id, url, version, expires_at, created}`. Always **private**; `url` is a fresh private link. `created` is false when `client_ref` matched. | `vitrine:write` |
| `artifact_update` | `id`; `files?`; `merge=false`; `entry?`; `title?`; `expires_in?` (`"never"` clears); `tags?`; `note?` | `{id, url, version}`. The id is unchanged. With `merge`, files overlay the current version; without it they replace it. Without `files`, the current files carry over, so a metadata-only change is still a new version; `artifact_set_meta` changes metadata without one. On a **public** artifact the human is asked first (§ 6.1) and `url` is the public URL; a token without `vitrine:publish` is refused without asking (§ 4.4). | `vitrine:write` (+ `vitrine:publish` for a public artifact) |
| `artifact_get` | `id`; `version?` | `{artifact, version, files: [{path, bytes}], links, views}`. `artifact` is `meta.json` without its link records; `links` is `[{lid, label, created_at, expires_at, passcode: bool}]` for the live ones, never a URL or a hash. `views` is § 11.4's `{total, visitors, bots, first_viewed_at, last_viewed_at, links: [{lid, label, views, first_viewed_at, last_viewed_at}]}`, or `null` when `view_log` is `off`. `artifact.public_until` says when a public artifact stops being public. | `vitrine:read` |
| `artifact_set_meta` | `id`; `title?`; `tags?` (replaces them); `add_tags?`; `remove_tags?`; `project?` (`""` clears it) | `{id, title, tags, project}`. Rewrites `meta.json` only: no version, no gate, no new link epoch, so every link keeps working. `tags` cannot be combined with `add_tags` or `remove_tags`, and a call must name something to change. A change to the values already there writes and audits nothing. | `vitrine:write` |
| `artifact_list` | `query?` (search terms, below); `tag?`; `project?`; `visibility?`; `source?`; `updated_after?`; `expiring_within?` (a duration); `sort=updated` \| `created` \| `title`; `facets=false`; `limit=20` (max 100); `cursor?` | `{items: [{id, title, visibility, version, updated_at, source, tags, project, public_until}], next_cursor, facets}`. `updated` and `created` are newest first, `title` is A to Z. With `facets`, `facets` is `{projects: {name: count}, tags: {name: count}}` over the whole match, not the page; otherwise `null`. **No URLs**: links are minted on purpose with `artifact_link`. | `vitrine:read` |
| `artifact_link` | `id`; `ttl?`; `label?` (who it is for, at most 200 characters); `passcode?`: `true` generates one, a string of 8–128 characters sets one | `{url, expires_at, lid, passcode?}`: a fresh recorded link (§ 3). A generated passcode is returned **once**, shown as `xxxxx-xxxxx` (10 Crockford base32 characters, 50 bits); a supplied one is not returned. On a public artifact it returns the public URL with `expires_at` = `public_until`, and refuses a `label` or a `passcode`. Every call is audited (`link_issued`). | `vitrine:share` |
| `artifact_set_visibility` | `id`; `visibility`; `public_for?` (a duration, with `public` only) | `{id, visibility, url, public_until}`. Going public follows § 6; `public_for` and `public_ttl_max` set `public_until` (§ 4.3). On a public artifact, a different window asks the human again and keeps the links. Every change of visibility increments the link epoch. | `vitrine:publish` |
| `artifact_revoke_links` | `id`; `link?` (a `lid`) | `{id, link_epoch, link}`. With `link`, only that link and its unlocked URLs stop; without it, every private link does (epoch + 1). | `vitrine:share` |
| `artifact_upload_url` | `filename`; `id?`; `title?`; `ttl?` | `{id, put_url, expires_at, curl}`: a single-use signed upload URL (§ 7.3) for the artifact `id`, pre-allocated when none was given. `filename` and `title` ride unsigned in the URL's query: the signature fixes the id, the expiry and the nonce; the rest is the capability holder's to name. | `vitrine:write` |
| `artifact_delete` | `id` | `{deleted: id}` | `vitrine:write` |

Every tool also carries a `title` and all four MCP annotation hints (`readOnlyHint`,
`destructiveHint`, `idempotentHint`, `openWorldHint`); the values are listed in
[design § 4](design.md). Every parameter has a description and its constraints in the input
schema.

**Search.** `query` is split on whitespace into terms, and every term must match (AND),
case-insensitively, as a substring of the title, a tag, the project, a version note or the entry
file name. File contents are not searched. Filters combine with the terms: `updated_after` keeps
artifacts changed after that time, and `expiring_within` those whose `expires_at` falls within
that long from now. A cursor encodes its sort; one used with another sort is a bad request.

No tool other than `artifact_set_visibility` takes a visibility, and no HTTP route accepts one.
**There is exactly one path to public.**

### 5.2 Resources

| URI template | Content |
|---|---|
| `artifact://{id}` | `meta.json` plus the file list (JSON) |
| `artifact://{id}/{path}` | Text of one file in the current version. Text types only, up to 1 MB. Anything else returns an error naming the size and type. |
| `artifact://{id}/history` | This artifact's audit events, newest first (§ 11.3). An unknown id is `not found`. |
| `artifact://{id}/views` | This artifact's views hour by hour for the last 30 days: `[{hour, views, visitors, bots}]`, oldest first, empty hours left out (§ 11.4). An unknown id is `not found`. |

Clients use resources to read an artifact back before revising it. Every resource needs `vitrine:read`
and spends the `rate_reads` bucket. A file named `history` or `views` at an artifact's root is
shadowed by those resources; read it with `GET /s/…` instead.

### 5.3 Policy carried in descriptions

Tool descriptions are part of this spec, because they are what the model reads.

- **`artifact_create` and `artifact_update`:** they **MUST** state that artifacts are private, and
  that revising should reuse the `id` so the link stays the same.
- **`artifact_set_visibility`:** it **MUST** state:
  - use it only when the user asked for a public link;
  - the user will be asked to confirm;
  - the agent should tell the user when a link is public, and until when (`public_until`).
- **`artifact_link`:** it **MUST** tell the agent to give a passcode to the user separately from
  the link, and say when a public URL stops being public.

### 5.4 Server instructions

The server **MUST** send this text as its MCP `instructions`. Clients that surface it give the
model the sharing policy once, for every tool. It is part of the contract; changing it is a
minor-version change.

```text
vitrine publishes files as links. Use it for anything the user should open in a browser,
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
anyone with the link can view it. With a public window it **MUST** name the end ("…PUBLIC until
24 Oct 2026 10:00 UTC? Anyone with the link can view it until then"). When it moves the window of
an artifact that is already public, it says so ("Keep … PUBLIC until …"). When `artifact_update`
asks before adding a version to a public artifact, the message names the title, says it is public
and that the new version is visible immediately.

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
| `GET /s/{sig}/[{path}]`, `GET /s/{sig}@{n}/[{path}]` | view or unlocked signature | The same for any artifact the signature names. A recorded link with a passcode answers **401 with the unlock form** instead, for every path: vitrine's own page, with no title, no file names and nothing from the artifact. |
| `POST /s/{sig}[@{n}]/unlock` | view signature | Form fields `passcode` and `path`. A right passcode answers `303` to `/s/{usig}[@{n}]/{path}` (an `unlocked` signature, § 3), so relative assets resolve under it. A wrong one answers 401 with the form and one generic message. A link without a passcode answers `303` to itself. A bad signature or an expired artifact answers as the `GET` would (404 / 410). The body is at most 1 KiB, and a foreign `Origin` (`null` included) is refused with 403. |
| `PUT /u/{sig}?filename=[&title=]` | upload signature, from a client still configured with `vitrine:write` | Raw body. Creates the pre-allocated private artifact (`201`), or adds a version that overlays the file on the current ones (`200`). Single use. Returns `{id, url, version, expires_at}`. A public target returns 403 (§ 4.4). An URL whose client was removed from `VITRINE_TOKENS`, or lost `vitrine:write`, returns 404. |
| `POST /api/artifacts` | bearer, `vitrine:write` | `multipart/form-data`: one `file` part per file (the part's filename is its relative path), plus fields `title`, `entry?`, `expires_in?`, `tags?` (repeatable, or comma-separated), `project?` (empty means none), `note?`, `client_ref?`. Any other field is refused with 400, so there is no way to send a visibility. Returns `201 {id, url, version, expires_at}`, or `200` with the existing artifact when `client_ref` matched (§ 4.5). Always private. |
| `PUT /api/artifacts/{id}` | bearer, `vitrine:write` | Same body, without `client_ref`. Adds a version; `merge=true` overlays; `expires_in=never` clears the expiry; an empty `project` clears it. A public target returns 403 (§ 4.4). |
| `GET /api/artifacts` | bearer, `vitrine:read` | Same query parameters and page as `artifact_list` |
| `GET /api/artifacts/{id}[?version=]` | bearer, `vitrine:read` | As `artifact_get`: metadata, one version's files, live links (never their URL or hash) and views |
| `DELETE /api/artifacts/{id}` | bearer, `vitrine:write` | As `artifact_delete`. Returns `{deleted}` |
| `PATCH /api/artifacts/{id}` | bearer, `vitrine:write` | JSON `{title?, tags?, add_tags?, remove_tags?, project?}`, as `artifact_set_meta`. Any other key, `visibility` included, is refused with 400. Returns `{id, title, tags, project}` |
| `POST /api/artifacts/{id}/links` | bearer, `vitrine:share` | JSON `{ttl?, label?, passcode?}`, as `artifact_link`. Returns `201 {url, expires_at, lid, passcode}`; a generated passcode is in it once, a supplied one never. On a public artifact, the public URL |
| `POST /api/artifacts/{id}/revoke` | bearer, `vitrine:share` | JSON `{link?}`: the one link with that `lid`, or every private link. Returns `{id, link_epoch, link}` |
| `GET /healthz` | none | `200 {"ok": true}` when `data_dir` is writable (probed through `data_dir/tmp`, where every write starts), else 503 |
| `GET /metrics` on `metrics_port` | none | Prometheus text format, on a separate listener (§ 11) |

No `/api` route takes a visibility: the one path to public is `artifact_set_visibility` (§ 6).
Each route spends its tool's bucket (§ 7.5) and is audited as its tool is. A JSON body is
`application/json` (else 415) of at most 16 KiB, counted while it streams; an empty body is `{}`.

The `@{n}` suffix keeps version pinning inside one path segment, so relative asset URLs in
multi-file HTML keep working under every prefix.

The unlock form carries `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline';
form-action 'self'; frame-ancestors 'none'`, `no-store`, and `Referrer-Policy: same-origin` (under
`no-referrer` a browser posts the form with `Origin: null`). A passcode is never kept in a cookie:
agent HTML runs in an opaque origin, where a `SameSite` cookie never reaches the page's own
assets, so the unlocked state rides in the path segment, like `@{n}` (ADR 0006).

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
| `md` | CommonMark with raw HTML disabled, plus tables, strikethrough, task lists (disabled checkboxes), footnotes and an `id` anchor on each heading. Front matter (`---` lines at the top) is not shown; a simple `title: …` line in it becomes the page title. |
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
| `rate_writes` | client | `60/m` | create, update, metadata edits, upload URL issue, upload, delete |
| `rate_links` | client | `120/m` | `artifact_link`, `artifact_revoke_links`, `artifact_set_visibility`, and their `/api` routes |
| `rate_reads` | client | `600/m` | get, list, resources |
| `rate_views` | source IP | `1200/m` | `/a/` and `/s/` requests |
| `rate_unlock` | link + source IP | `10/m` | wrong passcodes only |
| `rate_unlock_link` | link | `100/h` | wrong passcodes only, from every address together |

Behaviour when a bucket is empty:

- MCP calls fail with `rate limited: retry after <n>s`.
- HTTP returns `429` with `Retry-After`.
- Rejected calls do no work and write nothing. An unlock refused by either unlock bucket does not
  check the passcode, so its holder can lock out other holders of that same link only.

The source IP honours `X-Forwarded-For` only from `forwarded_allow_ips`. Setting a limit to
`off` disables it. Counters reset on restart, which is acceptable for a single process. A bucket
that has refilled is dropped from memory, so the per-IP and per-link state does not grow with
every address that ever called.

A refusal from a client's bucket is audited (§ 11.3). A refused view is not: a viewer has no
client name, and writing a line per refused request would turn a flood into disk writes.

## 8. Configuration

### 8.1 Sources and precedence

1. Command-line flags (`--host`, `--port`, `--config`)
2. Environment variables `VITRINE_<KEY>` (upper-case key)
3. A TOML file given with `--config` (top-level keys or a `[vitrine]` table)
4. Defaults

**Secrets** (`secret`, `tokens`) are accepted **only** from the environment. A config file that
sets them **MUST** be rejected at startup, so config files can be committed safely. Unknown keys
**MUST** be rejected, which catches typos, in the file and among `VITRINE_*` variables alike. The
client-side `VITRINE_URL` and `VITRINE_TOKEN` (what MCP clients and the Hermes plugin read) are not
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
| `rate_unlock` / `rate_unlock_link` | `10/m` / `100/h` | Wrong passcodes per link and IP, and per link (§ 7.5) |
| `max_links` | `500` | Live recorded links per artifact (§ 3) |

**Links**

| Key | Default | Description |
|---|---|---|
| `link_ttl` / `link_ttl_max` | `7d` / `90d` | Private link lifetime: default and ceiling |
| `upload_ttl` / `upload_ttl_max` | `15m` / `1h` | Upload URL lifetime: default and ceiling |
| `unlock_ttl` | `12h` | How long an unlocked passcode link works, never past the link itself (§ 3) |
| `public_ttl_max` | `off` | The longest public window; `off` for none (§ 4.3) |

**Operations**

| Key | Default | Description |
|---|---|---|
| `sweep_interval` | `1h` | Expiry, unpublish, link-record, staging, idempotency-record, audit- and view-retention sweep period |
| `audit_retention` | `365d` | Audit files older than this are deleted by the sweep |
| `view_log` | `counts` | `counts` or `off` (§ 11.4) |
| `view_retention` | `90d` | View-count months older than this are deleted by the sweep |
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
# vitrine.toml: safe to commit; secrets come from VITRINE_SECRET and VITRINE_TOKENS
[vitrine]
data_dir        = "/var/lib/vitrine"
public_base_url = "https://vitrine.example.com"
host            = "0.0.0.0"
public_mode     = "confirm"
renderers       = ["md", "csv", "json"]
quota           = "20GB"
link_ttl        = "3d"
metrics_port    = 9090
log_format      = "json"
```

```bash
export VITRINE_SECRET="$(mcp-vitrine secret new)"
export VITRINE_TOKENS="hermes:<sha256>:read+write+share+publish,ci-bot:<sha256>:write"   # from `mcp-vitrine token new`
mcp-vitrine serve --config vitrine.toml
```

## 9. Command line

| Command | Purpose |
|---|---|
| `mcp-vitrine serve [--config FILE] [--host H] [--port P]` | Run the server. It refuses to start on data with another `schema_version` ([operations](operations.md) § 4), sweeps once before the first request, then every `sweep_interval`. |
| `mcp-vitrine config check [--config FILE]` | Validate the configuration and print the effective values, with secrets redacted. Exits non-zero on error. |
| `mcp-vitrine token new <name> [--scopes read,write,share,publish]` | Print a new random token, plus its `VITRINE_TOKENS` entry. Writes nothing to disk. |
| `mcp-vitrine token hash <token>` | Print the hash of an existing token |
| `mcp-vitrine secret new` | Print a new random 32-byte hex secret |
| `mcp-vitrine sweep [--config FILE]` | Run the expiry and staging sweep once, then exit (for cron-style setups). Safe beside a running server: staging younger than 10 minutes is left alone. |
| `mcp-vitrine audit [--id ID] [--client NAME] [--since 7d] [--json] [--config FILE]` | Print audit events, newest first; `--json` prints the stored lines |
| `mcp-vitrine views [--id ID] [--since 7d] [--config FILE]` | Print view counts per artifact and link, most viewed first: views, visitors, bots (§ 11.4). It reads what the server flushed, so the last minute may be missing. |
| `mcp-vitrine backup <dest> [--config FILE]` | Write a consistent, restorable copy of `data_dir`, `views/` included ([operations](operations.md) § 2) |
| `mcp-vitrine migrate [--dry-run] [--config FILE]` | Upgrade `meta.json` files to the current `schema_version` ([operations](operations.md) § 4) |

Every command exits `0` on success, `1` when it failed, and `2` when the arguments or the
configuration are wrong. Commands that read `data_dir` load the same configuration as `serve`, so
they need `VITRINE_SECRET` and `VITRINE_TOKENS` too.

## 10. Errors

| Condition | MCP | HTTP |
|---|---|---|
| Missing or invalid token | 401 (transport) | 401 |
| Token without the scope | tool not listed; calling it fails as unknown | 403 |
| Unknown or malformed id; private id, or a public one past `public_until`, on `/a/`; invalid link; revoked link record | tool error `not found` | 404 |
| A passcode link opened without its passcode, or with a wrong one | — | 401 with the unlock form |
| Expired artifact | tool error `expired` | 410 |
| Unsafe path; bad entry; empty artifact; a duration that is zero or over 36500 days | tool error naming the problem | 400 |
| An `/api` JSON body sent as another media type | — | 415 |
| Upload URL reused; `client_ref` reused with different content; the artifact changed while the human decided | tool error `conflict` | 409 |
| Rate limit exceeded | tool error `rate limited: retry after <n>s` | 429 + `Retry-After` |
| Over a size or file-count limit; `max_links` live links | tool error | 413 |
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
| `vitrine_artifacts` | gauge | — |
| `vitrine_storage_bytes` | gauge | — |
| `vitrine_versions_created_total` | counter | `client` |
| `vitrine_views_total` | counter | `route` = `a` \| `s`; `kind` = `view` \| `bot` (a link unfurler, § 11.4). Every file served, assets included. |
| `vitrine_unlock_total` | counter | `outcome` = `ok` \| `wrong` \| `rate_limited` |
| `vitrine_public_gate_total` | counter | `outcome` = `approved` \| `declined` \| `unsupported` \| `disabled` \| `not_required` (one per gate decision: making an artifact public, or adding a version to a public one) |
| `vitrine_rejected_total` | counter | `reason` = `rate_limited` \| `scope` |

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
| `visibility_changed` | `from`, `to`, `gate` = `approved` \| `not_required` \| `scheduled` (the sweep ended a public window; `client` = `@sweep`). Moving the window of a public artifact is `from` = `to` = `public`. |
| `publish_refused` | `gate` = `declined` \| `unsupported` \| `disabled` |
| `link_issued` | `lid`, `expires_at`, `passcode` (a boolean). Never the label, the URL or the passcode. |
| `links_revoked` | `link_epoch` (every link), or `lid` (one link) |
| `metadata_changed` | `fields`: the **names** of what changed (`title`, `tags`, `project`), never the values |
| `upload_url_issued` | `expires_at` |
| `deleted` | — |
| `expired` | — (written by the sweep) |
| `migrated` | `version` (the `schema_version` reached), `files` (how many `meta.json` files); no `artifact`; `client` = `@operator`. Written once per `mcp-vitrine migrate` run that changes anything, before the first file changes. |
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

### 11.4 Views

Views are telemetry, not audit events: they are counted in memory and appended to
`views/<YYYY-MM>.jsonl` every 60 seconds and at shutdown, without an `fsync`, so a crash loses up to
a minute of counts. A flood costs one counter, not a disk write per request.

`view_log` (*configurable*): `counts` (default) or `off`, which records nothing.

**What counts.** A view is a `GET` answered 2xx whose path is the entry or a renderer page;
assets do not count, so a page with 30 images is one view. Not counted: `HEAD`, `Range` requests,
`Sec-Purpose: prefetch`, requests refused by a rate limit, and unlock forms.

**Who.** A viewer is anonymous, so "who" means *which link*:

- a recorded link's views count under its `lid`, shown with its `label` when read; older links
  and the public URL count under `lid: null`;
- a **visitor** is `HMAC(hour_key, ip | user_agent)`, where `hour_key` is random, lives in memory
  only and changes every hour; only the **number** of distinct values is stored;
- a **bot** is a user agent from a short list of link unfurlers (Slackbot, Discordbot,
  TelegramBot, WhatsApp, Twitterbot, facebookexternalhit, LinkedInBot), counted apart from views.

No IP address or user agent reaches disk. Each line holds counts added since the previous line
for its key, and the lines of a key add up to its totals:

```json
{"hour":"2026-10-17T14:00:00Z","artifact":"k7m2q9x4tz3wbn6r","route":"s","lid":"AbCdEfGh_-0","views":12,"visitors":3,"bots":1}
```

Disk grows with artifacts × hours viewed × links, not with requests. `view_retention` (default
`90d`) deletes whole months; counts of deleted artifacts stay until then, and hold only ids.
They are read through `artifact_get.views`, `artifact://{id}/views` and `mcp-vitrine views`.

## 12. Compatibility

| Area | Policy |
|---|---|
| Versioning | Semantic versioning. The MCP tool names and parameters, the HTTP routes, the config keys and `meta.json` are the public API. |
| MCP protocol | Both `2025-11-25` and `2026-07-28` clients are supported. The confirmation mechanism follows the negotiated version (§ 6.1). |
| Python | 3.15 and newer |
| On-disk format | `meta.json` carries `schema_version`. Upgrades migrate forward on `mcp-vitrine migrate`; the server refuses to start on data it would misread ([operations](operations.md) § 4). |
