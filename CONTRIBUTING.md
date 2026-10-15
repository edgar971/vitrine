# Contributing to drop

Thanks for looking. drop is designed, not built: the contract is
[`docs/spec.md`](docs/spec.md), the build plan is [`docs/roadmap.md`](docs/roadmap.md), and
[`AGENTS.md`](AGENTS.md) is the short version of both for humans and coding agents.

## Setup

Only [uv](https://docs.astral.sh/uv/) and [just](https://github.com/casey/just) are needed.

```bash
uv sync                 # environment, from uv.lock
just                    # list the recipes
just check              # every gate CI runs
```

`just check` is the definition of done for a change; it runs `uv lock --check`, `ruff format
--check`, `ruff check`, `mypy --strict`, `lint-imports`, `pytest` with the coverage gate, and
`pip-audit`. `just test-browser` runs the Playwright specs, which the default run skips.

## The rules that are not negotiable

They come from [`docs/design.md`](docs/design.md) § 9 and [`docs/security.md`](docs/security.md) § 6:

- **Private by default.** Nothing becomes public except through `PublicGate`, which fails closed.
- **One path to public.** Do not add a second one, and do not add a `visibility` argument to a
  create or upload route.
- **Every HTTP route authenticates itself.** A reverse proxy is optional, never relied on; the
  route-table spec must classify any route you add.
- **Signed link segments and tokens are never logged.**
- **Every state change is audited**, and the audit log never holds artifact content.

Never weaken a check or widen a default to make a test pass. Fix the test, or ask.

## Tests are specs

Tests read as specifications, in the BDD style of [`docs/design.md`](docs/design.md) § 9:

- the file is `*_spec.py`;
- the class is `Describe<Unit>` with a nested `When<Situation>`;
- the function is `it_<one observable outcome>`.

One outcome per `it_`, no branching inside a test, givens as fixtures in the nearest
`conftest.py`. pytest is configured to collect only these names, so a stray `test_*` function
silently never runs.

New behaviour needs an `it_` that fails without it; a security fix also covers the rejection path.

## Commits and pull requests

- [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/): `feat:`, `fix:`, `docs:`,
  `test:`, `ci:`, `build:`, `chore:`, `refactor:`, `perf:`. The `commits` CI job refuses a pull
  request with any other message (merge commits are fine).
- One concern per commit; a commit that changes the contract also changes
  [`docs/spec.md`](docs/spec.md) (and [`docs/security.md`](docs/security.md) for a new threat or
  control).
- A decision that is expensive to reverse gets an ADR in [`docs/adr/`](docs/adr/).

## Changelog

`CHANGELOG.md` is generated from commit messages by [git-cliff](https://git-cliff.org)
([`cliff.toml`](cliff.toml)), so **the commit message is the changelog entry**: write it for
someone upgrading.

| Commit | Section |
|---|---|
| `fix(security): …` (any type with the `security` scope) | Security |
| `feat: …` | Added |
| `perf: …`, `refactor: …` | Changed |
| `fix: …` | Fixed |
| `revert: …` | Removed |
| `docs:`, `ci:`, `build:`, `test:`, `chore:`, `style:` | left out |

`just changelog` shows the section the next release would get. Do not edit `## [Unreleased]` by
hand; the one exception is `0.1.0`, whose notes were written before the changelog was generated.

## Releasing

1. **Rehearse with a pre-release.** `just release 0.2.0rc1` bumps every copy of the version
   (`pyproject.toml`, the example image tags, the Hermes plugin), re-locks and runs the gates.
   Commit it as `chore(release): 0.2.0rc1`, merge, and tag the merged commit:
   `git tag v0.2.0rc1 <sha> && git push origin v0.2.0rc1`. The release workflow pushes and signs
   `ghcr.io/edgar971/drop:0.2.0rc1`, publishes to **TestPyPI**, and creates a GitHub pre-release
   with notes rendered from the commits.
2. **Check the rehearsal**: install it with
   `uvx --index https://test.pypi.org/simple/ --index-strategy unsafe-best-match --from mcp-drop==0.2.0rc1 mcp-drop --version`
   (its dependencies come from PyPI),
   connect a client, and verify the image as [operations](docs/operations.md) § 9 says.
3. **Release.** `just release 0.2.0` does the same and also writes the `## [0.2.0]` section of
   `CHANGELOG.md`. Commit, merge and tag `v0.2.0`: this one goes to **PyPI**, and its notes are
   that section.

The tag must be PEP 440 (`v0.2.0rc1`, not `v0.2.0-rc.1`), and the workflow refuses a tag that
disagrees with `pyproject.toml`. The image is pushed and verified before anything reaches PyPI,
because a PyPI version can never be uploaded twice.

## Reporting a vulnerability

Do not open an issue. Follow [`SECURITY.md`](SECURITY.md).
