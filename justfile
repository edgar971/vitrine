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

# Build the image for this machine. Behind a TLS-intercepting proxy, CA=<bundle.pem> passes
# its CA to the build as a secret (it never lands in a layer).
image tag="drop:dev":
    #!/usr/bin/env bash
    set -euo pipefail
    ca=()
    if [ -n "${CA:-}" ]; then ca=(--secret "id=ca_bundle,src=$CA"); fi
    # The ${ca[@]+…} form: macOS bash 3.2 calls an empty array unbound under set -u.
    docker build ${ca[@]+"${ca[@]}"} --tag "{{tag}}" .

# Build the image, then run it read-only, without capabilities, and probe /healthz
image-test tag="drop:dev": (image tag)
    docker/smoke.sh "{{tag}}"

# Validate examples/: kubeconform on the Kubernetes manifests, Compose and the Caddyfile
examples:
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v kubeconform >/dev/null; then
        kubeconform -strict -summary -skip Kustomization examples/kubernetes
    else
        docker run --rm -v "$PWD/examples/kubernetes:/k:ro" ghcr.io/yannh/kubeconform:v0.8.0 \
            -strict -summary -skip Kustomization /k
    fi
    DROP_SECRET=x DROP_TOKENS=x DROP_DOMAIN=drop.example.com \
        docker compose -f examples/compose.yaml config --quiet
    docker run --rm -e DROP_DOMAIN=drop.example.com -v "$PWD/examples/Caddyfile:/etc/caddy/Caddyfile:ro" \
        caddy:2.10 caddy adapt --config /etc/caddy/Caddyfile >/dev/null
    echo "examples: kubernetes, compose and Caddyfile are valid"

# Regenerate docs/configuration.md from Settings (a spec fails while it is stale)
docs:
    uv run python -m mcp_drop.reference > docs/configuration.md

# One spec: just one DescribeThePackage
one name:
    uv run pytest -k "{{name}}" --no-cov

# Serve locally with the environment's DROP_* settings
dev:
    uv run mcp-drop serve
