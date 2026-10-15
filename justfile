# drop — one task runner for every gate. `just --list` shows the recipes.
# CI runs these same recipes (docs/design.md § 10), so nothing is checked only locally.

set shell := ["bash", "-euo", "pipefail", "-c"]

# Refresh the environment from uv.lock
sync:
    uv sync

# Every gate CI runs. Run this before every commit.
check: lock-check lint types layers test audit

# uv.lock agrees with pyproject.toml
lock-check:
    uv lock --check

# ruff format and ruff check
lint:
    uv run ruff format --check
    uv run ruff check

# Apply the formatter and the safe lint fixes
format:
    uv run ruff format
    uv run ruff check --fix

# mypy --strict with the pydantic plugin
types:
    uv run mypy

# The layering contracts (docs/design.md § 3)
layers:
    uv run lint-imports

# The specs, with the 90% coverage gate
test:
    uv run pytest

# The browser specs (needs `uv run playwright install chromium`)
test-browser:
    uv run pytest -m browser --no-cov
# Known vulnerabilities in the locked dependencies (uv.lock has no pip-audit reader,
# so the lock is exported to a pinned requirements list first)
audit:
    #!/usr/bin/env bash
    set -euo pipefail
    requirements="$(mktemp -t drop-audit.XXXXXX.txt)"
    trap 'rm -f "$requirements"' EXIT
    uv export --frozen --no-emit-project --no-hashes --quiet --output-file "$requirements"
    uvx pip-audit --requirement "$requirements" --no-deps --disable-pip --progress-spinner off

# One spec: just one DescribeThePackage
one name:
    uv run pytest -k "{{name}}" --no-cov

# Serve locally with the environment's DROP_* settings
dev:
    uv run mcp-drop serve
