# syntax=docker/dockerfile:1.7
# vitrine's image: multi-arch (amd64, arm64), non-root, and fine on a read-only root filesystem.
# Everything it writes goes to /data (artifacts, staging, audit), so mount a volume there.

ARG PYTHON_VERSION=3.15

# Base images are pinned by digest, so a moved tag cannot change a signed release; Dependabot
# bumps the tag and the digest together.
FROM ghcr.io/astral-sh/uv:0.13.7-trixie-slim@sha256:f6da342fe598dc462a7e6f61e7b3bf7fc84e381467efbdf29ccc1d32f2fc68a4 AS build
ARG PYTHON_VERSION
# A uv-managed CPython under /python, copied whole into the runtime image; bytecode is compiled
# here because the runtime filesystem is read-only.
ENV UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1
WORKDIR /app
# Behind a TLS-intercepting proxy, pass its CA bundle as a build secret:
#   docker build --secret id=ca_bundle,src=ca.pem .
# It is read only while a step runs and never lands in a layer.
COPY docker/with-ca.sh /usr/local/bin/with-ca
RUN --mount=type=secret,id=ca_bundle,required=false \
    with-ca uv python install "${PYTHON_VERSION}"
# Dependencies first, so a source change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv --mount=type=secret,id=ca_bundle,required=false \
    with-ca uv sync --no-dev --extra metrics --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv --mount=type=secret,id=ca_bundle,required=false \
    with-ca uv sync --no-dev --extra metrics --no-editable

FROM debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4
LABEL org.opencontainers.image.title="vitrine" \
      org.opencontainers.image.description="Private and public links for agent output (MCP server)" \
      org.opencontainers.image.source="https://github.com/edgar971/vitrine" \
      org.opencontainers.image.licenses="MIT"
RUN groupadd --system --gid 10001 vitrine \
    && useradd --system --uid 10001 --gid vitrine --no-create-home --shell /usr/sbin/nologin vitrine \
    && mkdir /data && chown vitrine:vitrine /data
COPY --from=build /python /python
COPY --from=build /app/.venv /app/.venv
COPY docker/healthcheck.py /app/healthcheck.py
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VITRINE_HOST=0.0.0.0 \
    VITRINE_PORT=8080 \
    VITRINE_DATA_DIR=/data
USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]
ENTRYPOINT ["mcp-vitrine"]
CMD ["serve"]
