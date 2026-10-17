# 0006 — Links become records with a `lid`; a passcode unlock rides in the path, not a cookie

- **Status:** Accepted
- **Date:** 2026-10-17
- **Context:** [`docs/spec.md`](../spec.md) § 3, § 4.2, § 7.1, § 11.4;
  [`docs/security.md`](../security.md) § 3 (T5, T22, T23), § 5 (R7); ADR 0003

## Context

A private link is a bearer capability. It gets forwarded, pasted into tickets and fetched by chat
unfurlers that show a preview to a whole channel. Other artifact hosts offer a view password;
vitrine had no second factor. It also could not revoke one link without revoking all of them
(the epoch), and could not say which link was opened.

Two questions had to be settled: whether a passcode belongs to the artifact or to a link, and how
a browser that typed the right passcode proves it on every following request.

## Decision

- **Per link.** Every link `artifact_link` mints carries a link id (`lid`, 8 random bytes) inside
  its signature and has a record in `meta.json`: `{lid, label, created_at, expires_at, passcode}`.
  It verifies only while that record exists, so deleting the record revokes that one link. Two
  recipients can get two passcodes, and views are attributed to the link they came through.
- The passcode is stored as an scrypt hash (standard library, `n=2**15, r=8, p=1`) and returned
  once. The link a create or an update returns stays unrecorded: it is the author's working link,
  and recording it would fill `max_links` with every revision.
- **In the path.** A right passcode is traded (`POST /s/{sig}/unlock`) for a new signed kind,
  `unlocked`, carried in the same path segment a view signature uses: `/s/{usig}/…`. It is valid
  for `unlock_ttl` at most, and only while the link's record and epoch still hold.
- Wrong passcodes spend two buckets (per link and address, per link); a right one is refunded.

The alternative was a cookie set after the unlock. Agent HTML runs in an opaque origin (CSP
`sandbox` without `allow-same-origin`), so the browser treats the page's own requests for its
images, scripts and stylesheets as cross-site: a `SameSite=Lax` or `Strict` cookie never reaches
them and multi-file artifacts would break, while `SameSite=None` is sent on every cross-site
request. vitrine already keeps state in one path segment (`@{n}`), so the unlock does the same.

## Consequences

- `meta.json` changes when a link is minted, so minting is audited (`link_issued`) and capped
  (`max_links`). The epoch revoke and every change of visibility also drop every record.
- An unlocked URL is a bearer capability too: forwarded, it works for up to `unlock_ttl`
  (security R7). Revoking the link ends it at once.
- The unlock form is the only vitrine page under `/s/`; it is served with its own strict CSP and
  `Referrer-Policy: same-origin`, because under `no-referrer` browsers post the form with
  `Origin: null`, which the unlock refuses.
- Links minted before this change carry no `lid` and keep verifying as before, without a passcode.
