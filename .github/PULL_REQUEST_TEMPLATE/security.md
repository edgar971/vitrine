<!--
Title: fix(security): … (it lands under Security in the changelog).
A vulnerability is reported privately first (SECURITY.md). Open this PR only when the fix can be
public: do not describe an unpatched exploit here.
-->

## What and why

The threat, the control that failed, and what the fix changes. Link the advisory once it is
published.

## Checklist

- [ ] `just check` is green.
- [ ] A spec covers the rejection path: it fails without the fix.
- [ ] `docs/security.md` is updated: the threat and control in § 3, a residual risk in § 5, or the spec list in § 6.
- [ ] No invariant in `AGENTS.md` is weakened, and no check or default is loosened to make a test pass.
- [ ] Nothing new logs a token, a signed link segment or artifact content.
