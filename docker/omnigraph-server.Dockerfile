# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
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
# (packages/witan-core/witan_core/omnigraph_install.py :: _OMNIGRAPH_VERSION)
# and docker/witan.Dockerfile's. omnigraph uses strict single-version storage:
# a binary refuses a graph written by a different on-disk format, in either
# direction, so a client and a server on different releases cannot talk to the
# same store at all.
#
# Renovate's custom manager covers all three files (renovate.json), and
# `just check-omnigraph-pins` fails CI if they ever drift — the second belt
# exists because this file went a full release cycle claiming the manager
# covered it when it did not, and a partial bump is silent until deploy.

ARG OMNIGRAPH_VERSION=0.10.0
# Upstream tag to fetch from. `edge` is the rolling build of upstream main,
# republished on every push there; a real release is `v${OMNIGRAPH_VERSION}`.
# Kept separate because on a moving tag the two differ — see
# witan_core/omnigraph_install.py :: _OMNIGRAPH_RELEASE_TAG.
ARG OMNIGRAPH_RELEASE_TAG=edge
ARG OMNIGRAPH_SHA256_X86_64=063fc1b31fc2d3528b189573afe2d09094e7983dbf52ef4a819818c7ed736e04
ARG OMNIGRAPH_SHA256_ARM64=426047934fffe7a94e65a5f63813ff04b9f64d080c3f1780c3db2d269bd23337

# ── Fetch + checksum-verify the release, extract both binaries ────────────────
FROM debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS fetch
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
    for b in omnigraph omnigraph-server; do \
        found="$(find /stage -type f -name "$b" | head -n1)"; \
        [ -n "$found" ] || { echo "binary $b not found in ${base}.tar.gz" >&2; exit 1; }; \
        install -m 0755 "$found" "/out/$b"; \
    done; \
    /out/omnigraph --version

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS runtime
ARG OMNIGRAPH_VERSION
ARG OMNIGRAPH_RELEASE_TAG
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
