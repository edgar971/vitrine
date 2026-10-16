# vitrine configuration

<!-- Generated from mcp_vitrine.config.Settings by `just docs`. Do not edit by hand. -->

Every key can be set as `VITRINE_<KEY>` in the environment or in a TOML file passed with `--config`
(top-level keys or a `[vitrine]` table); `--host` and `--port` override both. The secrets are read
from the environment only, and a file that sets one is refused. Unknown keys are refused too.
The contract, including the accepted formats, is [spec § 8](spec.md#8-configuration).

| Kind | Accepted forms |
|---|---|
| Sizes | `512KB`, `100MB`, `10GB` or bytes (binary units) |
| Durations | `30s`, `15m`, `1h`, `7d` or seconds |
| Lists | Comma-separated in the environment; arrays in TOML |
| Rates | `<int>/<s\|m\|h>`, for example `60/m`, or `off` |

## Serving

| Key | Environment | Default | Description |
|---|---|---|---|
| `data_dir` | `VITRINE_DATA_DIR` | `./data` | Storage root (spec § 4.1) |
| `public_base_url` | `VITRINE_PUBLIC_BASE_URL` | `http://localhost:8080` | Origin used to build every URL; an origin, with no path |
| `host` | `VITRINE_HOST` | `127.0.0.1` | Listen address |
| `port` | `VITRINE_PORT` | `8080` | Listen port |
| `allowed_hosts` | `VITRINE_ALLOWED_HOSTS` | `[]` | Extra `Host` values accepted besides the base URL's |
| `forwarded_allow_ips` | `VITRINE_FORWARDED_ALLOW_IPS` | `["127.0.0.1"]` | Proxies whose `X-Forwarded-*` headers are trusted |

## Secrets (environment only)

| Key | Environment | Default | Description |
|---|---|---|---|
| `secret` | `VITRINE_SECRET` | required | HMAC key for links, at least 32 bytes. Rotating it revokes every private link. |
| `tokens` | `VITRINE_TOKENS` | required | Client tokens as `name:sha256hex[:scope+scope…]`, comma-separated; at least one. Omitted scopes mean all four. |

## Policy

| Key | Environment | Default | Description |
|---|---|---|---|
| `public_mode` | `VITRINE_PUBLIC_MODE` | `confirm` | `confirm`, `disabled` or `unrestricted` (spec § 6) |
| `renderers` | `VITRINE_RENDERERS` | `["md", "csv", "tsv", "json"]` | Renderers to enable; `[]` serves everything raw |

## Limits

| Key | Environment | Default | Description |
|---|---|---|---|
| `max_upload` | `VITRINE_MAX_UPLOAD` | `100MB` | Request body limit on `/api` and `/u` |
| `max_mcp_body` | `VITRINE_MAX_MCP_BODY` | `4MB` | Request body limit on `/mcp` |
| `max_artifact` | `VITRINE_MAX_ARTIFACT` | `100MB` | Largest single version |
| `max_files` | `VITRINE_MAX_FILES` | `200` | Files per version |
| `quota` | `VITRINE_QUOTA` | `10GB` | Total storage; writes beyond it fail with 507 |
| `retain_versions` | `VITRINE_RETAIN_VERSIONS` | `10` | Versions kept per artifact |
| `ref_ttl` | `VITRINE_REF_TTL` | `24h` | How long a `client_ref` stays bound (spec § 4.5) |
| `rate_writes` | `VITRINE_RATE_WRITES` | `60/m` | Per-client write bucket |
| `rate_links` | `VITRINE_RATE_LINKS` | `120/m` | Per-client link bucket |
| `rate_reads` | `VITRINE_RATE_READS` | `600/m` | Per-client read bucket |
| `rate_views` | `VITRINE_RATE_VIEWS` | `1200/m` | Per-IP view bucket |

## Links

| Key | Environment | Default | Description |
|---|---|---|---|
| `link_ttl` | `VITRINE_LINK_TTL` | `7d` | Default private link lifetime |
| `link_ttl_max` | `VITRINE_LINK_TTL_MAX` | `90d` | Longest private link lifetime |
| `upload_ttl` | `VITRINE_UPLOAD_TTL` | `15m` | Default upload URL lifetime |
| `upload_ttl_max` | `VITRINE_UPLOAD_TTL_MAX` | `1h` | Longest upload URL lifetime |

## Operations

| Key | Environment | Default | Description |
|---|---|---|---|
| `sweep_interval` | `VITRINE_SWEEP_INTERVAL` | `1h` | Period of the expiry, staging, idempotency-record and audit-retention sweep |
| `audit_retention` | `VITRINE_AUDIT_RETENTION` | `365d` | Audit months older than this are deleted |
| `metrics_port` | `VITRINE_METRICS_PORT` | `0` | Prometheus listener, `0` for off; needs the `metrics` extra |
| `log_level` | `VITRINE_LOG_LEVEL` | `INFO` | Log level |
| `log_format` | `VITRINE_LOG_FORMAT` | `text` | `text` or `json` |
