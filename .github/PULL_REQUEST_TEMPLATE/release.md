<!-- Title: chore(release): X.Y.Z (or X.Y.ZrcN for a rehearsal). CONTRIBUTING.md § Releasing has the whole procedure. -->

## Release X.Y.Z

## Before merging

- [ ] Made with `just release X.Y.Z`, which bumped `pyproject.toml`, the example image tags and the Hermes plugin, re-locked, and ran the gates.
- [ ] `CHANGELOG.md` has a `## [X.Y.Z]` section that reads well for someone upgrading (a pre-release leaves it alone).
- [ ] An on-disk format change bumps `schema_version`, ships a migration, and the changelog says to run `mcp-vitrine migrate`.
- [ ] For a final release: the rehearsal (`X.Y.ZrcN`) was installed from TestPyPI and checked, and its image signature verified ([operations](https://github.com/edgar971/vitrine/blob/main/docs/operations.md#9-deployments) § 9).

## After merging

- [ ] Tag the merged commit: `git tag vX.Y.Z <sha> && git push origin vX.Y.Z`.
- [ ] The release workflow passed: image pushed, smoke-tested and signed; package on (Test)PyPI; GitHub release created.
- [ ] A final release that is the newest one moved `ghcr.io/edgar971/vitrine:stable`.
