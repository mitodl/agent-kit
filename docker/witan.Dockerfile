# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
#
# witan — the MCP tier (ADR-0009). Packages the `witan` umbrella CLI (which
# mounts `witan code` from witan-code) and runs it as a streamable-HTTP MCP
# server. The `witan` Pulumi stack hosts this behind the ToolHive
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
# Keep in lockstep with witan_core's installer pin
# (packages/witan-core/witan_core/omnigraph_install.py :: _OMNIGRAPH_VERSION)
# and docker/omnigraph-server.Dockerfile's — see that file for why a split
# version is an outage. Renovate covers all three; `just check-omnigraph-pins`
# is the CI backstop.
ARG OMNIGRAPH_VERSION=0.10.0
# Upstream tag to fetch from. `edge` is the rolling build of upstream main,
# republished on every push there; a real release is `v${OMNIGRAPH_VERSION}`.
# Kept separate because on a moving tag the two differ — see
# witan_core/omnigraph_install.py :: _OMNIGRAPH_RELEASE_TAG.
ARG OMNIGRAPH_RELEASE_TAG=edge
ARG OMNIGRAPH_SHA256_X86_64=9f4dccb07cc3a4e4f6d1b367877f18f5ed9c7899c48c91c18da2a84e86a5c99e
ARG OMNIGRAPH_SHA256_ARM64=cfdf6c9950ee7f0b1ea26969e42d5f2e67ffa9b80c656e24474612687dea489d
# Keep in lockstep with witan-council's version (mcp/servers/witan/pyproject.toml
# [project].version / [tool.bumpversion]); it labels the built image.
ARG WITAN_VERSION=0.8.0

# ── Fetch the pinned omnigraph CLI binary (checksum-verified) ─────────────────
FROM debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS omnigraph-fetch
ARG OMNIGRAPH_VERSION
ARG OMNIGRAPH_RELEASE_TAG
ARG OMNIGRAPH_SHA256_X86_64
ARG OMNIGRAPH_SHA256_ARM64
ARG TARGETARCH=amd64
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
# The tarball is checked against the digest pinned IN THIS REPO, not against the
# .sha256 published beside it. On a moving tag (`edge`) that published file only
# attests to whichever build was current at download time, so it cannot tie this
# image to the build the repo was tested against — and the installer and the two
# image builds could each resolve the same tag to a different commit while every
# version/tag check still passed. Keep these in step with
# witan_core/omnigraph_install.py :: _OMNIGRAPH_ASSET_SHA256;
# `just check-omnigraph-pins` enforces it.
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) arch=x86_64 ;; \
        arm64) arch=arm64 ;; \
        *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    base="omnigraph-linux-${arch}"; \
    url="https://github.com/ModernRelay/omnigraph/releases/download/${OMNIGRAPH_RELEASE_TAG}"; \
    cd /tmp; \
    case "${arch}" in \
        x86_64) want="${OMNIGRAPH_SHA256_X86_64}" ;; \
        arm64)  want="${OMNIGRAPH_SHA256_ARM64}" ;; \
    esac; \
    [ -n "${want}" ] || { echo "no pinned sha256 for ${base}" >&2; exit 1; }; \
    curl -fsSL -o "${base}.tar.gz" "${url}/${base}.tar.gz"; \
    echo "${want}  ${base}.tar.gz" > "${base}.sha256"; \
    sha256sum -c "${base}.sha256"; \
    mkdir -p /out /stage; \
    tar -xzf "${base}.tar.gz" -C /stage; \
    found="$(find /stage -type f -name omnigraph | head -n1)"; \
    [ -n "$found" ] || { echo "omnigraph binary not found in ${base}.tar.gz" >&2; exit 1; }; \
    install -m 0755 "$found" /out/omnigraph; \
    /out/omnigraph --version

# ── Build the relocatable venv from the uv workspace ──────────────────────────
FROM python:${PYTHON_VERSION}-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc /uv /uvx /usr/local/bin/

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

# git is not optional here even for the MCP tier: witan-code shells out to it
# to resolve a checkout's repo URI, current branch, and root (witan_code/repo.py
# — git is the only correct parser of its own config, worktrees included). The
# CI indexer entrypoint below also clones with it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=omnigraph-fetch /out/omnigraph /usr/local/bin/omnigraph
# The CI code-graph indexer (ol-infrastructure `applications/witan/ci_indexer.py`
# runs it as a CronJob). Shipped in this image rather than its own so the
# process writing a shared code graph is the same build as the one serving it.
COPY --chmod=0755 docker/witan-ci-index.sh /usr/local/bin/witan-ci-index
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /src /src

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH
WORKDIR /src
USER witan

# omnigraph must resolve on PATH (OmnigraphClient constructs at import), and
# `witan code` must be mounted — the CI indexer entrypoint is nothing but that
# subcommand in a loop, and an image without witan-code installed would fail
# only once the CronJob first fired.
RUN witan --help >/dev/null && witan code --help >/dev/null && omnigraph --version && git --version

EXPOSE 8000
ENTRYPOINT ["witan"]
# Overridden by the `witan` stack's MCPServer `args`; this default documents the
# deployed invocation and keeps the image runnable standalone. The transport
# serves both protocol eras: a 2026-07-28 client is answered statelessly (no
# handshake, no Mcp-Session-Id, so replicas need no session affinity), an older
# one still gets the handshake. See mcp/servers/witan/docs/adr/0006.
CMD ["serve", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
