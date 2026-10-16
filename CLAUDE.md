@AGENTS.md

# CLAUDE.md

`AGENTS.md` (imported above) is the canonical guide. This file adds only Claude Code specifics.

## Claude Code

- Rules (`.claude/rules/`): `mcp.md` loads for `src/mcp_vitrine/mcp/**`; `web.md` for
  `src/mcp_vitrine/web/**` and `app.py`; `tests.md` for `tests/**`.
- Hooks (`.claude/settings.json`): `ruff format` + `ruff check --fix` on every edited `.py` file.
- Gates: `just check` runs everything CI runs. Personal preferences go in `CLAUDE.local.md`
  (gitignored), never here.
