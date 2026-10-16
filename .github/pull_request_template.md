<!-- The title is a Conventional Commit; it may become a changelog entry (CONTRIBUTING.md § Changelog). -->

## What and why

## Checklist

- [ ] `just check` is green.
- [ ] New behaviour has an `it_*` that fails without it; a security change also covers the rejection path.
- [ ] A contract change updates `docs/spec.md` (and `docs/security.md` for a new threat or control).
- [ ] A decision that is expensive to reverse has an ADR in `docs/adr/`.
