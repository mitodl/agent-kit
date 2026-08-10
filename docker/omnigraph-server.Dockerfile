# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
#
# omnigraph-server — witan's data tier (ADR-0009 decision point 2 / option 3).
#
# A minimal container around the upstream omnigraph release
# (https://github.com/ModernRelay/omnigraph, an external Rust project not
# vendored in this repo). It bakes BOTH release binaries: `omnigraph-server`
# (the HTTP server the witan MCP tier talks to over the cluster network) and
# `omnigraph` (the CLI, used by the entrypoint to converge the cluster catalog
# on boot). All three cluster schemas are baked under ${OMNIGRAPH_CLUSTER_DIR}/:
# schema.pg (the `council` memory/work graph), code-schema.pg (per-repo
# `code-<repo>` graphs), and bridge-schema.pg (the shared `code-bridge` graph).
# The `omnigraph` Pulumi stack mounts a generated cluster.yaml alongside them
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
FROM debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS fetch
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
FROM debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS runtime
ARG OMNIGRAPH_VERSION
LABEL org.opencontainers.image.title="omnigraph-server" \
      org.opencontainers.image.description="witan data tier — S3-backed omnigraph graph server" \
      org.opencontainers.image.source="https://github.com/mitodl/agent-kit" \
      org.opencontainers.image.version="${OMNIGRAPH_VERSION}"

# ca-certificates: the omnigraph binaries talk to S3 over TLS.
# python3-minimal + python3-yaml: the entrypoint renders the Cedar bundles'
# group membership from the mounted actor-token map before converging (see
# policy/render_groups.py). Deliberately a real YAML parser rather than a
# sed/awk rewrite of a file that decides who can write the graph — a silent
# mis-render here denies every user at once.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates python3-minimal python3-yaml \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --user-group --create-home omnigraph

COPY --from=fetch /out/omnigraph /usr/local/bin/omnigraph
COPY --from=fetch /out/omnigraph-server /usr/local/bin/omnigraph-server
COPY docker/omnigraph-server-entrypoint.sh /usr/local/bin/omnigraph-server-entrypoint.sh
COPY mcp/servers/witan/policy/render_groups.py /usr/local/bin/render-policy-groups.py

# The cluster config dir holds the baked-in schema.pg, the ConfigMap-mounted
# cluster.yaml (added at deploy time), and the ephemeral `__cluster/` state the
# entrypoint's `cluster import`/`apply` writes — so it must be owned by the
# non-root runtime user. Keep this path in lockstep with CLUSTER_CONFIG_DIR in
# ol-infrastructure src/ol_infrastructure/applications/omnigraph/data_tier.py.
ENV OMNIGRAPH_CLUSTER_DIR=/etc/omnigraph/cluster
RUN mkdir -p "${OMNIGRAPH_CLUSTER_DIR}" \
    && chmod +x /usr/local/bin/omnigraph-server-entrypoint.sh \
    && chmod +x /usr/local/bin/render-policy-groups.py
# All three cluster schemas are baked in: schema.pg (witan memory/work graph →
# the `council` graph), code-schema.pg (per-repo `code-<repo>` graphs), and
# bridge-schema.pg (the shared `code-bridge` graph). The deploy-time cluster.yaml
# ConfigMap references each by these paths; all three are self-contained (soft
# refs only, no hard cross-store edges).
COPY mcp/servers/witan/schema/schema.pg /etc/omnigraph/cluster/schema.pg
COPY mcp/servers/witan-code/witan_code/schema/code-schema.pg /etc/omnigraph/cluster/code-schema.pg
COPY mcp/servers/witan-code/witan_code/schema/bridge-schema.pg /etc/omnigraph/cluster/bridge-schema.pg
# The four Cedar bundles, baked alongside the schemas for the same reason: the
# `omnigraph` Pulumi stack has no access to this tree at apply time, and these
# are agent-kit's tested deliverable (policy/tests/*.tests.yaml). The deploy-time
# cluster.yaml references them by these paths via its `policies:` block. Their
# committed `groups:` are fixtures — the entrypoint rewrites membership from the
# mounted actor-token map before `cluster apply`, so what ships in the image is
# never what is enforced.
COPY mcp/servers/witan/policy/memory.policy.yaml /etc/omnigraph/cluster/memory.policy.yaml
COPY mcp/servers/witan/policy/code-graph.policy.yaml /etc/omnigraph/cluster/code-graph.policy.yaml
COPY mcp/servers/witan/policy/bridge.policy.yaml /etc/omnigraph/cluster/bridge.policy.yaml
COPY mcp/servers/witan/policy/server.policy.yaml /etc/omnigraph/cluster/server.policy.yaml
RUN chown -R omnigraph:omnigraph /etc/omnigraph

USER omnigraph
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/omnigraph-server-entrypoint.sh"]
# Overridden by the `omnigraph` stack's Deployment `args`
# (--cluster /etc/omnigraph/cluster --bind 0.0.0.0:8080); this default keeps the
# image runnable standalone and documents the expected invocation.
CMD ["--cluster", "/etc/omnigraph/cluster", "--bind", "0.0.0.0:8080"]
