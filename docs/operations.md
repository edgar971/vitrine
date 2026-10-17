# vitrine operations

Running vitrine day to day. The interfaces are in the [spec](spec.md); the reasons are in
[security](security.md).

## 1. What state exists

| State | Where | Back up? |
|---|---|---|
| Artifacts, versions, idempotency records, audit log | `data_dir/` | Yes: this is all the data |
| Configuration | The TOML file and non-secret env vars | Yes, alongside the deployment (it is safe to commit) |
| `VITRINE_SECRET` | Secret store | Yes, separately. Without the same secret, existing private links stop working. |
| `VITRINE_TOKENS` (hashes) | Secret store | Yes. Clients keep their plaintext tokens. |
| Rate-limit counters, caches | Memory | No; rebuilt on start |

## 2. Backup

vitrine's write protocol is crash-safe ([design § 5](design.md)). Any **crash-consistent snapshot**
of `data_dir` is therefore a valid backup: a filesystem, volume or VM snapshot taken at any
moment. On restore, the startup sweep removes half-staged files, and orphan versions are ignored.

Without snapshots, use the built-in command:

```
mcp-vitrine backup <dest>
  for each artifact:   read meta.json → copy the versions it lists → read meta.json again
                       → unchanged: write that meta.json into <dest>; changed: copy it again
  then:                copy refs/, then the audit months oldest first, each up to its last whole line
  result:              <dest>/ is a valid data_dir; prints artifacts, versions, bytes copied
```

It runs online and takes no lock, so writers never wait for it. Versions are immutable once
placed and `meta.json` changes only by rename, so a `meta.json` read before and after its versions
were copied describes exactly what was copied. An artifact that keeps changing for 50 attempts in a
row fails the backup; run it again. `<dest>` must be a new or empty directory.

**Plain `cp -r` or `rsync` of a live `data_dir` is not supported.** It can copy a `meta.json`
that references a version folder it has not copied yet.

## 3. Restore

```
stop vitrine → replace data_dir with the backup → keep the same VITRINE_SECRET → start vitrine
```

- Public URLs keep working.
- Private links keep working only if `VITRINE_SECRET` is unchanged and they haven't expired.
- `mcp-vitrine config check` followed by `/healthz` confirms the restored instance.

## 4. Upgrades and migrations

```
read CHANGELOG ─► backup ─► stop ─► install new version ─► mcp-vitrine migrate --dry-run
                                                                │ shows what would change
                                                                ▼
                                    mcp-vitrine migrate ─► start ─► /healthz ─► smoke test
```

**Versions** follow semantic versioning. Patch and minor releases never need a migration. A
release that changes the on-disk format bumps `schema_version` and says so in the changelog.

**Migrations**

- Forward-only and idempotent; safe to re-run after an interruption.
- Each file is migrated with the same staging plus `os.replace` as a normal write.
- The audit log records one `migrated` event per run that changes anything, written before the
  first file changes; a dry run, or a run with nothing to do, records nothing.

| `schema_version` | Release | What `migrate` does |
|---|---|---|
| 1 | `0.1.x` | — |
| 2 | `0.2.0` | Adds `links`, `fallback`, `public_until` and `project` at their defaults (spec § 4.2); nothing else changes, and every link keeps working |

**Startup refuses** to run against:

- data with an **older** `schema_version`, until `mcp-vitrine migrate` has run;
- data with a **newer** `schema_version` (a downgrade). Restore the pre-upgrade backup instead.

## 5. Rotation

| What | Steps | Effect |
|---|---|---|
| A client token | `mcp-vitrine token new <name>-2` → add its entry to `VITRINE_TOKENS` → update the client → remove the old entry | No downtime; the two entries overlap |
| A client's scopes | Edit that client's entry in `VITRINE_TOKENS`, then restart | Tokens are checked on every request, so it applies as soon as the server is back; the client's open upload URLs follow the new scopes too |
| `VITRINE_SECRET` | `mcp-vitrine secret new` → replace → restart | **Every private link and upload URL stops working.** Public URLs are unaffected. Agents mint fresh links with `artifact_link`. |

## 6. Monitoring

`/healthz` is for liveness and readiness. With `metrics_port` set, these alerts are suggested:

| Alert | Expression (sketch) | Why |
|---|---|---|
| Storage near quota | `vitrine_storage_bytes / quota > 0.8` for 30m | Writes fail with 507 at 100% |
| Publishing refused repeatedly | `increase(vitrine_public_gate_total{outcome!="approved"}[1h]) > 5` | An agent keeps trying to go public, which may be a prompt injection |
| Clients rate-limited | `increase(vitrine_rejected_total{reason="rate_limited"}[15m]) > 0` | A looping agent, or limits set too tight |
| Server errors | 5xx rate on the ingress | |

The audit log (`mcp-vitrine audit --since 24h`) is where an operator answers "who published what".

## 7. Incident playbooks

| Situation | Action |
|---|---|
| A private link was shared too widely | `artifact_revoke_links(id, link=lid)` ends that one link and its unlocked URLs; without `link`, every private link. `artifact_get` lists the live links with their labels, and `mcp-vitrine views --id` shows which link was opened. `mcp-vitrine audit --id` shows who minted each link (`link_issued`) and who changed the artifact |
| Something is public that shouldn't be | `artifact_set_visibility(id, private)` removes `/a/` access at once and revokes private links. Then `artifact_delete` if needed. |
| A client token leaked | Remove its entry from `VITRINE_TOKENS` and restart. Review `mcp-vitrine audit --client <name>`. |
| `VITRINE_SECRET` leaked | Rotate it (§ 5); every private link dies. Review the audit log. |
| The disk is full | Raise `quota` or the volume size. Lower `retain_versions`. Check for artifacts without `expires_at`. |
| An agent is looping | Rate limits contain it. Remove the client's token if it persists. |

## 8. Removing vitrine

Delete `data_dir` and the secrets. Every link stops resolving. Back up the audit directory first
if it must be kept.

## 9. Deployments

Every key is in the generated [configuration reference](configuration.md). The ready-made setups:

| Setup | Where | Notes |
|---|---|---|
| Local | `uvx mcp-vitrine serve` | Binds `127.0.0.1:8080`; `data_dir` is `./data` |
| Container | [`Dockerfile`](../Dockerfile) | See below |
| Compose with Caddy | [`examples/compose.yaml`](../examples/compose.yaml), [`examples/Caddyfile`](../examples/Caddyfile) | Caddy terminates TLS; only Caddy publishes ports; vitrine trusts `X-Forwarded-For` from the compose subnet only |
| Kubernetes | [`examples/kubernetes/`](../examples/kubernetes/) | `kubectl apply -k`; Pod Security `restricted`, one `Recreate` replica, RWO volume, no egress |
| A config file | [`examples/vitrine.toml`](../examples/vitrine.toml) | Safe to commit; secrets stay in the environment |

**The image** runs `mcp-vitrine serve` as uid/gid `10001`, listening on `0.0.0.0:8080`, with
`data_dir=/data`. Everything it writes is under `/data`, so it runs with `--read-only` and
`--cap-drop ALL`; mount a volume at `/data`. Its `HEALTHCHECK` sends the `Host` that
`VITRINE_PUBLIC_BASE_URL` names, because every route checks `Host`; Kubernetes probes set the same
header. The image includes the `metrics` extra. The CLI runs inside it with the server's
environment, for example `docker exec vitrine mcp-vitrine audit --since 24h`.

**Image tags.** Every release is tagged with its version (`0.1.0`), and `stable` follows the
newest final release, never a pre-release. `stable` is for trying vitrine; a deployment pins a
version or a digest, so an upgrade happens when you choose it, after the backup and migration
steps in § 4.

**Verifying a release image.** Each image is signed keylessly by the release workflow, and
carries an SBOM. Pin the digest, then check the signature before deploying:

```bash
cosign verify ghcr.io/edgar971/vitrine@sha256:<digest> \
  --certificate-identity-regexp '^https://github.com/edgar971/vitrine/\.github/workflows/release\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
docker buildx imagetools inspect ghcr.io/edgar971/vitrine@sha256:<digest> --format '{{ json .SBOM }}'
```

Behind a TLS-intercepting proxy, build with that proxy's CA as a secret:
`CA=ca.pem just image` (or `docker build --secret id=ca_bundle,src=ca.pem .`). The bundle is
used while the build steps run and never lands in a layer.

**Kubernetes backups:** a CSI volume snapshot of the PVC is crash-consistent, which is all § 2
needs. `mcp-vitrine backup` also works through `kubectl exec`, but the root filesystem is read-only,
so its destination has to be another mounted volume.
