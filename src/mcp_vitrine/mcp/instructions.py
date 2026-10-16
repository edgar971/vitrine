"""The server ``instructions`` (spec § 5.4). Part of the contract: a change is a minor version."""

from __future__ import annotations

INSTRUCTIONS = """\
vitrine publishes files as links. Use it for anything the user should open in a browser,
revise later or pass on: HTML pages, reports, charts, PDFs, images.
- Everything you publish is private: links are signed and expire.
- To revise, call artifact_update with the same id; the link stays the same.
- Only call artifact_set_visibility(public) when the user asked for a public link.
  The user is asked to confirm. Say clearly when a link is public.
- Pass client_ref when a call may be retried, so a retry never creates a duplicate.
- Small images or files that only matter in this conversation: attach them instead."""
