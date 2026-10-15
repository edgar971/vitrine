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
  `test:`, `ci:`, `chore:`, `refactor:`, `perf:`.
- One concern per commit; a commit that changes the contract also changes
  [`docs/spec.md`](docs/spec.md) (and [`docs/security.md`](docs/security.md) for a new threat or
  control).
- A decision that is expensive to reverse gets an ADR in [`docs/adr/`](docs/adr/).
- Update `CHANGELOG.md` under `## [Unreleased]`.

## Reporting a vulnerability

Do not open an issue. Follow [`SECURITY.md`](SECURITY.md).
