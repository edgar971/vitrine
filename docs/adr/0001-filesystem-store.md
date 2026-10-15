# 0001 — The default `Store` is the filesystem, not a database

- **Status:** Accepted
- **Date:** 2026-10-15
- **Context:** [`docs/design.md`](../design.md) § 1, § 5; [`docs/spec.md`](../spec.md) § 4;
  [`docs/research.md`](../research.md) § 2

## Context

drop stores artifacts and their metadata, and its target deployment is one agent host or one small
server: one process, one port, one directory. The data is a set of files, written once per version
and addressed by id. The candidate designs were a SQL database (SQLite or PostgreSQL) with the
blobs on disk, a blob store such as S3, or the filesystem alone.

A database would add a second source of truth that has to stay consistent with the files during
writes, backups and sweeps, and a second thing for a self-hoster to run. At the target scale the
only thing a database buys is an index — and writes are already serialised per artifact.

## Decision

The default `Store` is `FileStore`, over one `data_dir`:

- one directory per artifact, one immutable directory per version, and a `meta.json` replaced with
  a single rename;
- the write protocol in design § 5: stage into `tmp/<uuid>` (streaming, with the path, size and
  count limits applied), rename the stage into place, then replace `meta.json`;
- writers are serialised per artifact by a lock; readers take no lock, because versions are
  immutable and metadata arrives in one rename;
- `Store` is a protocol, so a different implementation does not change any caller.

## Consequences

- Backup is a copy of the directory, and restore is putting it back
  ([`docs/operations.md`](../operations.md)).
- A crash before the rename leaves garbage in `tmp/`, removed by the startup sweep; a crash between
  the two renames leaves one orphan version that nothing references and the next write replaces.
- `list` scans `meta.json` files, which is fine to roughly 10⁴ artifacts. An index, if it is ever
  needed, is internal to `FileStore`.
- The file locks assume one process. More than one replica needs another `Store` (an S3-compatible
  one is the planned seam), not a change to the services above it.
- Every `Store` implementation runs the same contract suite (`tests/contract/`), so the seam is
  tested rather than aspirational.
