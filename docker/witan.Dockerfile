# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
#
# witan — the MCP tier (ADR-0009). Packages the `witan` umbrella CLI (which
# mounts `witan code` from witan-code) and runs it as a streamable-HTTP MCP
# server. The toolhive_witan Pulumi stack hosts this behind the ToolHive
# operator and points it at the omnigraph-server data tier over the cluster
# network (WITAN_MEMORY_URI).
#
# The image bakes the pinned `omnigraph` CLI binary on PATH: witan's
# OmnigraphClient shells out to it for EVERY graph call (local and remote alike
# — witan_core/omnigraph.py), and constructs one at import time, so the binary
# must be present or the server fails to start. Baking it in also removes the
# runtime `witan setup` download from the cold-start path.
#
# Built from the repo root so the whole uv workspace (root pyproject.toml +
# uv.lock + the five member packages) is in the build context:
#   docker build -f docker/witan.Dockerfile -t witan:$(git rev-parse --short HEAD) .

ARG PYTHON_VERSION=3.14
ARG OMNIGRAPH_VERSION=0.8.1
# Keep in lockstep with witan-council's version (mcp/servers/witan/pyproject.toml
# [project].version / [tool.bumpversion]); it labels the built image.
ARG WITAN_VERSION=0.4.0

# ── Fetch the pinned omnigraph CLI binary (checksum-verified) ─────────────────
FROM debian:trixie-slim@sha256:020c0d20b9880058cbe785a9db107156c3c75c2ac944a6aa7ab59f2add76a7bd AS omnigraph-fetch
ARG OMNIGRAPH_VERSION
ARG TARGETARCH=amd64
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) arch=x86_64 ;; \
        arm64) arch=arm64 ;; \
        *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    base="omnigraph-linux-${arch}"; \
    url="https://github.com/ModernRelay/omnigraph/releases/download/v${OMNIGRAPH_VERSION}"; \
    cd /tmp; \
    curl -fsSL -o "${base}.tar.gz" "${url}/${base}.tar.gz"; \
    curl -fsSL -o "${base}.sha256" "${url}/${base}.sha256"; \
    sha256sum -c "${base}.sha256"; \
    mkdir -p /out /stage; \
    tar -xzf "${base}.tar.gz" -C /stage; \
    found="$(find /stage -type f -name omnigraph | head -n1)"; \
    [ -n "$found" ] || { echo "omnigraph binary not found in ${base}.tar.gz" >&2; exit 1; }; \
    install -m 0755 "$found" /out/omnigraph; \
    /out/omnigraph --version

# ── Build the relocatable venv from the uv workspace ──────────────────────────
FROM python:${PYTHON_VERSION}-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.0@sha256:606e70c71c852d03f611b1e56a195d08648507018a7057fab82c4974c4eae105 /uv /uvx /usr/local/bin/

# build-essential is insurance for any dependency that ships only an sdist; the
# runtime stage discards it. UV_PYTHON_DOWNLOADS=never keeps uv on the image's
# own interpreter; UV_LINK_MODE=copy avoids hardlink warnings across layers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /src
# The whole workspace: root lock + every member's source. Editable workspace
# installs reference these paths at runtime, so the same tree is copied into the
# runtime stage below.
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY mcp/servers/witan/ mcp/servers/witan/
COPY mcp/servers/witan-code/ mcp/servers/witan-code/

RUN uv venv --relocatable /opt/venv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --all-packages

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-trixie AS runtime
ARG WITAN_VERSION
LABEL org.opencontainers.image.title="witan" \
      org.opencontainers.image.description="witan MCP server — agent memory, task, and code graph" \
      org.opencontainers.image.source="https://github.com/mitodl/agent-kit" \
      org.opencontainers.image.version="${WITAN_VERSION}"

RUN useradd --uid 1000 --user-group --create-home witan

COPY --from=omnigraph-fetch /out/omnigraph /usr/local/bin/omnigraph
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /src /src

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH
WORKDIR /src
USER witan

# omnigraph must resolve on PATH (OmnigraphClient constructs at import).
RUN witan --help >/dev/null && omnigraph --version

EXPOSE 8000
ENTRYPOINT ["witan"]
# Overridden by the toolhive_witan MCPServer `args`; this default documents the
# deployed invocation and keeps the image runnable standalone. The transport
# serves both protocol eras: a 2026-07-28 client is answered statelessly (no
# handshake, no Mcp-Session-Id, so replicas need no session affinity), an older
# one still gets the handshake. See mcp/servers/witan/docs/adr/0006.
CMD ["serve", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
