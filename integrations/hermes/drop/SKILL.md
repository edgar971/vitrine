---
name: drop
description: Share output as a link. Use when the output is HTML, a report or dashboard, a PDF or image the user will open in a browser, revise later, or pass to someone else.
version: 0.1.0
---
# Sharing artifacts with drop

- Text you write yourself (HTML, Markdown, SVG, CSV): `mcp_drop_artifact_create`.
- Files already on disk, binaries, or a folder with assets: `artifact_publish_files`.
- Revising: pass the same `id`, so the link the user already has shows the new version.
- Everything is private by default. Never make an artifact public unless the user asked for it
  in this conversation. `mcp_drop_artifact_set_visibility` asks the user to confirm; if they
  decline, or nobody answers, it stays private. Say "public link" whenever a link is public.
- A private link is a credential: share it only with the person who asked for it.
- Small images or files that only matter in this chat: attach them instead.
