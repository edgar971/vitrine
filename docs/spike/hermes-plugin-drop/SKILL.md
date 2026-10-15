---
name: drop
description: Share files as links. Use when output is HTML, a report or dashboard, a PDF or image the user will open in a browser, revise later, or pass to someone else.
version: 0.1.0
---
# Sharing artifacts with drop

- Text you write yourself (HTML, Markdown, SVG, CSV): `mcp_drop_artifact_create`.
- Files already on disk, binaries, or a folder with assets: `artifact_publish_files`.
- Revising: pass the same `id` so the link stays the same.
- Everything is private by default. Never request public unless the user asked for it in this
  conversation; going public asks the user to confirm. Say "public link" when a link is public.
- Small images or files that only matter in this chat: attach them instead (deliverable mode).
