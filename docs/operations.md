# drop operations

Running drop day to day. The interfaces are in the [spec](spec.md); the reasons are in
[security](security.md).

## 1. What state exists

| State | Where | Back up? |
|---|---|---|
| Artifacts, versions, idempotency records, audit log | `data_dir/` | Yes: this is all the data |
| Configuration | The TOML file and non-secret env vars | Yes, alongside the deployment (it is safe to commit) |
| `DROP_SECRET` | Secret store | Yes, separately. Without the same secret, existing private links stop working. |
| `DROP_TOKENS` (hashes) | Secret store | Yes. Clients keep their plaintext tokens. |
| Rate-limit counters, caches | Memory | No; rebuilt on start |

## 2. Backup

drop's write protocol is crash-safe ([design § 5](design.md)). Any **crash-consistent snapshot**
of `data_dir` is therefore a valid backup: a filesystem, volume or VM snapshot taken at any
moment. On restore, the startup sweep removes half-staged files, and orphan versions are ignored.

Without snapshots, use the built-in command:

```
mcp-drop backup <dest>
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
stop drop → replace data_dir with the backup → keep the same DROP_SECRET → start drop
```

- Public URLs keep working.
- Private links keep working only if `DROP_SECRET` is unchanged and they haven't expired.
- `mcp-drop config check` followed by `/healthz` confirms the restored instance.

## 4. Upgrades and migrations

```
read CHANGELOG ─► backup ─► stop ─► install new version ─► mcp-drop migrate --dry-run
                                                                │ shows what would change
                                                                ▼
                                    mcp-drop migrate ─► start ─► /healthz ─► smoke test
```

**Versions** follow semantic versioning. Patch and minor releases never need a migration. A
release that changes the on-disk format bumps `schema_version` and says so in the changelog.

**Migrations**

- Forward-only and idempotent; safe to re-run after an interruption.
- Each file is migrated with the same staging plus `os.replace` as a normal write.
- The audit log records one `migrated` event per run that changes anything, written before the
  first file changes; a dry run, or a run with nothing to do, records nothing.

**Startup refuses** to run against:

- data with an **older** `schema_version`, until `mcp-drop migrate` has run;
- data with a **newer** `schema_version` (a downgrade). Restore the pre-upgrade backup instead.

## 5. Rotation

| What | Steps | Effect |
|---|---|---|
| A client token | `mcp-drop token new <name>-2` → add its entry to `DROP_TOKENS` → update the client → remove the old entry | No downtime; the two entries overlap |
| A client's scopes | Edit that client's entry in `DROP_TOKENS`, then restart | Tokens are checked on every request, so it applies as soon as the server is back; the client's open upload URLs follow the new scopes too |
| `DROP_SECRET` | `mcp-drop secret new` → replace → restart | **Every private link and upload URL stops working.** Public URLs are unaffected. Agents mint fresh links with `artifact_link`. |

## 6. Monitoring

`/healthz` is for liveness and readiness. With `metrics_port` set, these alerts are suggested:

| Alert | Expression (sketch) | Why |
|---|---|---|
| Storage near quota | `drop_storage_bytes / quota > 0.8` for 30m | Writes fail with 507 at 100% |
| Publishing refused repeatedly | `increase(drop_public_gate_total{outcome!="approved"}[1h]) > 5` | An agent keeps trying to go public, which may be a prompt injection |
| Clients rate-limited | `increase(drop_rejected_total{reason="rate_limited"}[15m]) > 0` | A looping agent, or limits set too tight |
| Server errors | 5xx rate on the ingress | |

The audit log (`mcp-drop audit --since 24h`) is where an operator answers "who published what".

## 7. Incident playbooks

| Situation | Action |
|---|---|
| A private link was shared too widely | `artifact_revoke_links(id)`. Minting a link is not audited; `mcp-drop audit --id` shows who changed the artifact and who asked for upload URLs |
| Something is public that shouldn't be | `artifact_set_visibility(id, private)` removes `/a/` access at once and revokes private links. Then `artifact_delete` if needed. |
| A client token leaked | Remove its entry from `DROP_TOKENS` and restart. Review `mcp-drop audit --client <name>`. |
| `DROP_SECRET` leaked | Rotate it (§ 5); every private link dies. Review the audit log. |
| The disk is full | Raise `quota` or the volume size. Lower `retain_versions`. Check for artifacts without `expires_at`. |
| An agent is looping | Rate limits contain it. Remove the client's token if it persists. |

## 8. Removing drop

Delete `data_dir` and the secrets. Every link stops resolving. Back up the audit directory first
if it must be kept.

## 9. Deployments

Every key is in the generated [configuration reference](configuration.md). The ready-made setups:

| Setup | Where | Notes |
|---|---|---|
| Local | `uvx mcp-drop serve` | Binds `127.0.0.1:8080`; `data_dir` is `./data` |
| Container | [`Dockerfile`](../Dockerfile) | See below |
| Compose with Caddy | [`examples/compose.yaml`](../examples/compose.yaml), [`examples/Caddyfile`](../examples/Caddyfile) | Caddy terminates TLS; only Caddy publishes ports; drop trusts `X-Forwarded-For` from the compose subnet only |
| Kubernetes | [`examples/kubernetes/`](../examples/kubernetes/) | `kubectl apply -k`; Pod Security `restricted`, one `Recreate` replica, RWO volume, no egress |
| A config file | [`examples/drop.toml`](../examples/drop.toml) | Safe to commit; secrets stay in the environment |

**The image** runs `mcp-drop serve` as uid/gid `10001`, listening on `0.0.0.0:8080`, with
`data_dir=/data`. Everything it writes is under `/data`, so it runs with `--read-only` and
`--cap-drop ALL`; mount a volume at `/data`. Its `HEALTHCHECK` sends the `Host` that
`DROP_PUBLIC_BASE_URL` names, because every route checks `Host`; Kubernetes probes set the same
header. The image includes the `metrics` extra. The CLI runs inside it with the server's
environment, for example `docker exec drop mcp-drop audit --since 24h`.

**Verifying a release image.** Each image is signed keylessly by the release workflow, and
carries an SBOM. Pin the digest, then check the signature before deploying:

```bash
cosign verify ghcr.io/edgar971/drop@sha256:<digest> \
  --certificate-identity-regexp '^https://github.com/edgar971/drop/\.github/workflows/release\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
docker buildx imagetools inspect ghcr.io/edgar971/drop@sha256:<digest> --format '{{ json .SBOM }}'
```

Behind a TLS-intercepting proxy, build with that proxy's CA as a secret:
`CA=ca.pem just image` (or `docker build --secret id=ca_bundle,src=ca.pem .`). The bundle is
used while the build steps run and never lands in a layer.

**Kubernetes backups:** a CSI volume snapshot of the PVC is crash-consistent, which is all § 2
needs. `mcp-drop backup` also works through `kubectl exec`, but the root filesystem is read-only,
so its destination has to be another mounted volume.
