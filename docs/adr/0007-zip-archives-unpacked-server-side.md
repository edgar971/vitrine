# 0007 — Zip archives are unpacked server-side, under the write path's rules

- **Status:** Accepted
- **Date:** 2026-10-17
- **Context:** [`docs/spec.md`](../spec.md) § 4.3, § 7.1, § 7.3;
  [`docs/security.md`](../security.md) § 3 (T6, T9); [`docs/research.md`](../research.md) § 2,
  which this reverses

## Context

A built site (a `dist/` folder) can already be published as multipart, one part per file. An agent
that only has a shell and an upload URL cannot: the URL takes one body. The one-liner agents reach
for is `cd dist && zip -qr - . | curl -T - "$put_url"`, and other artifact hosts accept archives.

Research § 2 rejected zip bundles because they bring the zip-bomb and zip-slip classes. Those are
real, but they are bounded problems with known controls, and the shell-only agent has no other way
to publish a site.

## Decision

- **Server-side, opt-in.** `PUT /u/{sig}?unpack=zip`, and `unpack=zip` with exactly one
  `application/zip` part on `POST` / `PUT /api/artifacts`. Nothing else is ever unpacked, and a
  nested archive is stored as a file.
- **The write path's rules, applied first.** Every entry passes the § 4.3 path rule after one
  shared top-level folder is stripped; symlinks, encrypted entries, methods other than stored and
  deflate, non-UTF-8 names and duplicate paths are refused. These checks need only the central
  directory, so a refused archive writes nothing.
- **Sizes are counted, not believed.** Entries are inflated one by one, streaming, with a running
  total against `max_artifact` and a per-entry ratio against `unpack_max_ratio` past 1 MiB. A size
  header that lies fails its CRC.
- **One outcome.** Any refusal fails the whole upload. The inflated files then go through the
  normal write protocol (stage, quota, rename, `meta.json`), so quota applies to inflated bytes.
- **A site is a whole.** An unpacked archive replaces the version's files unless `merge=true`.
- Request bodies stream to `data_dir/tmp` rather than memory, which unpacking needs and which also
  closes the #15 item about whole bodies held in memory.

The alternative, unpacking in the client (the Hermes plugin, the `vitrine` CLI), does not help the
agent that has only `curl`, and a client-side check is not a control the server can rely on.

## Consequences

- New parsing surface, owned by `services/archives.py` and specified control by control in
  `tests/unit/services/archives_spec.py`, with a fuzz that no path escapes the version root.
- A site built for `/` requests `/assets/x.js`, outside `/a/{id}/`. vitrine does not rewrite agent
  HTML; the result warns, and the docs say how to build with a relative base.
- A site's `fallback` (an SPA's `index.html` or a `404.html`) answers missing routes, never a
  missing asset.
