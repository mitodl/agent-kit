# syntax=docker/dockerfile:1
#
# omnigraph-server — witan's data tier (ADR-0009 decision point 2 / option 3).
#
# A minimal container around the upstream omnigraph release
# (https://github.com/ModernRelay/omnigraph, an external Rust project not
# vendored in this repo). It bakes BOTH release binaries: `omnigraph-server`
# (the HTTP server the witan MCP tier talks to over the cluster network) and
# `omnigraph` (the CLI, used by the entrypoint to converge the cluster catalog
# on boot). All three cluster schemas are baked under ${CLUSTER_CONFIG_DIR}/:
# schema.pg (the `council` memory/work graph), code-schema.pg (per-repo
# `code-<repo>` graphs), and bridge-schema.pg (the shared `code-bridge` graph).
# The toolhive_witan Pulumi stack mounts a generated cluster.yaml alongside them
# via a ConfigMap `subPath` (single-file overlay) precisely so it does not
# shadow these baked-in schemas.
#
# Build (from the repo root, so schema.pg is in the build context):
#   docker build -f docker/omnigraph-server.Dockerfile \
#     -t omnigraph-server:$(git rev-parse --short HEAD) .
#
# The pinned OMNIGRAPH_VERSION MUST match witan_core's installer pin
# (packages/witan-core/witan_core/omnigraph_install.py :: _OMNIGRAPH_VERSION);
# the same Renovate custom-manager pin drives both, so bump them together.

ARG OMNIGRAPH_VERSION=0.8.1

# ── Fetch + checksum-verify the release, extract both binaries ────────────────
FROM debian:trixie-slim AS fetch
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
    for b in omnigraph omnigraph-server; do \
        found="$(find /stage -type f -name "$b" | head -n1)"; \
        [ -n "$found" ] || { echo "binary $b not found in ${base}.tar.gz" >&2; exit 1; }; \
        install -m 0755 "$found" "/out/$b"; \
    done; \
    /out/omnigraph --version

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM debian:trixie-slim AS runtime
ARG OMNIGRAPH_VERSION
LABEL org.opencontainers.image.title="omnigraph-server" \
      org.opencontainers.image.description="witan data tier — S3-backed omnigraph graph server" \
      org.opencontainers.image.source="https://github.com/mitodl/agent-kit" \
      org.opencontainers.image.version="${OMNIGRAPH_VERSION}"

# ca-certificates: the omnigraph binaries talk to S3 over TLS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --user-group --create-home omnigraph

COPY --from=fetch /out/omnigraph /usr/local/bin/omnigraph
COPY --from=fetch /out/omnigraph-server /usr/local/bin/omnigraph-server
COPY docker/omnigraph-server-entrypoint.sh /usr/local/bin/omnigraph-server-entrypoint.sh

# The cluster config dir holds the baked-in schema.pg, the ConfigMap-mounted
# cluster.yaml (added at deploy time), and the ephemeral `__cluster/` state the
# entrypoint's `cluster import`/`apply` writes — so it must be owned by the
# non-root runtime user. Keep this path in lockstep with CLUSTER_CONFIG_DIR in
# ol-infrastructure src/ol_infrastructure/applications/toolhive_witan/data_tier.py.
ENV OMNIGRAPH_CLUSTER_DIR=/etc/omnigraph/cluster
RUN mkdir -p "${OMNIGRAPH_CLUSTER_DIR}" \
    && chmod +x /usr/local/bin/omnigraph-server-entrypoint.sh
# All three cluster schemas are baked in: schema.pg (witan memory/work graph →
# the `council` graph), code-schema.pg (per-repo `code-<repo>` graphs), and
# bridge-schema.pg (the shared `code-bridge` graph). The deploy-time cluster.yaml
# ConfigMap references each by these paths; all three are self-contained (soft
# refs only, no hard cross-store edges).
COPY mcp/servers/witan/schema/schema.pg /etc/omnigraph/cluster/schema.pg
COPY mcp/servers/witan-code/witan_code/schema/code-schema.pg /etc/omnigraph/cluster/code-schema.pg
COPY mcp/servers/witan-code/witan_code/schema/bridge-schema.pg /etc/omnigraph/cluster/bridge-schema.pg
RUN chown -R omnigraph:omnigraph /etc/omnigraph

USER omnigraph
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/omnigraph-server-entrypoint.sh"]
# Overridden by the toolhive_witan Deployment's `args`
# (--cluster /etc/omnigraph/cluster --bind 0.0.0.0:8080); this default keeps the
# image runnable standalone and documents the expected invocation.
CMD ["--cluster", "/etc/omnigraph/cluster", "--bind", "0.0.0.0:8080"]
