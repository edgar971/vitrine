# syntax=docker/dockerfile:1.7
# drop's image: multi-arch (amd64, arm64), non-root, and fine on a read-only root filesystem.
# Everything it writes goes to /data (artifacts, staging, audit), so mount a volume there.

ARG PYTHON_VERSION=3.15

# Base images are pinned by digest, so a moved tag cannot change a signed release; Dependabot
# bumps the tag and the digest together.
FROM ghcr.io/astral-sh/uv:0.13.1-trixie-slim@sha256:2a4b12b30aa1a0f1d1f420ad3f4ac27716d5c2962302ac8a7af67ac3287bf36c AS build
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

FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f
LABEL org.opencontainers.image.title="drop" \
      org.opencontainers.image.description="Private and public links for agent output (MCP server)" \
      org.opencontainers.image.source="https://github.com/edgar971/drop" \
      org.opencontainers.image.licenses="MIT"
RUN groupadd --system --gid 10001 drop \
    && useradd --system --uid 10001 --gid drop --no-create-home --shell /usr/sbin/nologin drop \
    && mkdir /data && chown drop:drop /data
COPY --from=build /python /python
COPY --from=build /app/.venv /app/.venv
COPY docker/healthcheck.py /app/healthcheck.py
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DROP_HOST=0.0.0.0 \
    DROP_PORT=8080 \
    DROP_DATA_DIR=/data
USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]
ENTRYPOINT ["mcp-drop"]
CMD ["serve"]
