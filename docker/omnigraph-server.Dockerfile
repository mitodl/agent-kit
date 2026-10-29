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
#
# The CLI is the one exception to "both binaries come from the release
# tarball": see OMNIGRAPH_CLI_SOURCE below.

ARG OMNIGRAPH_VERSION=0.13.0
# Upstream tag to fetch from. A real release is `v${OMNIGRAPH_VERSION}`; `edge`
# is the rolling build of upstream main, republished on every push there.
# Kept separate because on a moving tag the two differ — see
# witan_core/omnigraph_install.py :: _OMNIGRAPH_RELEASE_TAG, which also records
# why this is a release tag again and what Lance 11 asks of an existing graph.
ARG OMNIGRAPH_RELEASE_TAG=v0.13.0
ARG OMNIGRAPH_SHA256_X86_64=0123daa64f0f2db9860284200bd71c79a22a6ad2c6f5501b6fca24cac86a7a30
ARG OMNIGRAPH_SHA256_ARM64=e61e7379131a05056bfeace206c8c5bcdf185d169d1ce86bc192d52f13693f96
# The on-disk storage format the pinned binary reads, mirrored from witan_core's
# declaration (packages/witan-core/witan_core/omnigraph_install.py ::
# _OMNIGRAPH_INTERNAL_SCHEMA) and stamped onto the image below as
# `edu.mit.ol.omnigraph.internal-schema`.
#
# WHY THE IMAGE HAS TO CARRY IT: ol-infrastructure deploys this image by digest
# and has no access to this repo, so the format it reads is opaque to the
# Pulumi program — its own preview check says it "does NOT verify either value
# against the image actually being deployed". Merging a format-bumping image
# into a cluster still serving the old root therefore surfaced as a
# cluster-apply Job dying mid-deploy (CI 2026-09-16, builds 187/188/189) rather
# than as a refused preview. Reading this label back out of ECR is what lets
# that preview compare the deploying image against the stack's committed
# `omnigraph:internal_schema_version`.
#
# The label name is a cross-repo contract: renaming it on either side turns the
# check off silently. `just check-omnigraph-pins` covers this value, the
# runtime stage's re-declaration of it, and the LABEL line that emits it — an
# ARG not re-declared in the stage expands to empty, which builds a clean image
# advertising no format at all.
ARG OMNIGRAPH_INTERNAL_SCHEMA=14
# Where the `omnigraph` CLI in this image comes from: `release` (the tarball
# above, like omnigraph-server) or `patched` (the cli-patched stage below).
#
# WHY A PATCHED CLI. Since 0.14 an inline --target is local-only, so the stock
# CLI cannot address the S3 cluster root at all, and with the server stopped
# there is then no way to converge the catalog on boot, run optimize/cleanup,
# or force-unlock a retained lock (upstream #1163, #1082). The cli-patched
# stage builds the CLI from OMNIGRAPH_CLI_PATCH_BASE_COMMIT plus
# docker/patches/omnigraph-cli-inline-s3-root.patch, a backport of upstream
# PR #1189, which admits an inline s3:// root for cluster
# plan/apply/status/force-unlock/upgrade-ledger and for
# optimize/cleanup/repair/rebuild-full-text-indexes with --graph. Only this
# image's CLI is patched: omnigraph-server here, the witan image and
# `witan setup` all keep the stock release digests.
#
# `just check-omnigraph-pins` holds the selector to the release pin: it must
# be `patched` exactly when OMNIGRAPH_RELEASE_TAG equals
# OMNIGRAPH_CLI_PATCH_BASE_TAG, so the patched CLI ships with the 0.14 bump
# and never beside a server from a different release (strict single-version
# storage, as above). Drop the patch, these three ARGs and the cli-patched
# stage once a release or an immutable edge build carries the change, or once
# served maintenance (upstream RFC #1186 / #1177) replaces the direct CLI.
ARG OMNIGRAPH_CLI_SOURCE=release
ARG OMNIGRAPH_CLI_PATCH_BASE_TAG=v0.14.0
# The tag's commit, fetched by id so a moved tag cannot change what is built.
ARG OMNIGRAPH_CLI_PATCH_BASE_COMMIT=60bfd19db780fdff3a5178f959c1204143420114

# ── Fetch + checksum-verify the release, extract both binaries ────────────────
FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS fetch
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

# ── CLI, stock: the release tarball's binary ──────────────────────────────────
FROM fetch AS cli-release

# ── CLI, patched: the release source plus the inline-S3-root backport ─────────
# Built only when OMNIGRAPH_CLI_SOURCE=patched (BuildKit skips a stage nothing
# selects). The compiler is the one the source's rust-toolchain.toml pins
# (rustup honours it over the image's default, and fetches the clippy and
# rustfmt components it lists), so bumping this image's tag changes the glibc
# and build tools but not rustc. Keep the image on that toolchain anyway, or
# rustup downloads a second one on every cold build. Trixie matches the
# runtime stage's glibc. RUSTFLAGS is set
# empty for the reason upstream's release workflow gives: it replaces the
# workspace's test-only `--cfg tokio_unstable`. The build is left unstamped
# (no OMNIGRAPH_RELEASE_CHANNEL), so it never claims to be the stable build.
FROM rust:1.97.1-slim-trixie@sha256:8e8cf8f7fd54a2d23d5a743b3a03f56e26b6c774276c33fa0595111704ebb15c AS cli-patched
ARG OMNIGRAPH_CLI_PATCH_BASE_TAG
ARG OMNIGRAPH_CLI_PATCH_BASE_COMMIT
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git protobuf-compiler libprotobuf-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git init -q . \
    && git fetch -q --depth 1 https://github.com/ModernRelay/omnigraph.git "${OMNIGRAPH_CLI_PATCH_BASE_COMMIT}" \
    && git checkout -q FETCH_HEAD
COPY docker/patches/omnigraph-cli-inline-s3-root.patch /tmp/
RUN git apply /tmp/omnigraph-cli-inline-s3-root.patch
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    RUSTFLAGS= cargo build --release --locked -p omnigraph-cli \
    && install -D -m 0755 target/release/omnigraph /out/omnigraph
# The source must be the release the selector is pinned against, or the CLI
# and the server would read different storage formats.
RUN set -eux; \
    reported="$(/out/omnigraph --version)"; \
    [ "v${reported##* }" = "${OMNIGRAPH_CLI_PATCH_BASE_TAG}" ] \
        || { echo "patched CLI reports '${reported}', expected ${OMNIGRAPH_CLI_PATCH_BASE_TAG}" >&2; exit 1; }

FROM cli-${OMNIGRAPH_CLI_SOURCE} AS cli

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS runtime
ARG OMNIGRAPH_VERSION
ARG OMNIGRAPH_RELEASE_TAG
ARG OMNIGRAPH_INTERNAL_SCHEMA
ARG OMNIGRAPH_CLI_SOURCE
LABEL org.opencontainers.image.title="omnigraph-server" \
      org.opencontainers.image.description="witan data tier — S3-backed omnigraph graph server" \
      org.opencontainers.image.source="https://github.com/mitodl/agent-kit" \
      org.opencontainers.image.version="${OMNIGRAPH_VERSION}" \
      edu.mit.ol.omnigraph.internal-schema="${OMNIGRAPH_INTERNAL_SCHEMA}" \
      edu.mit.ol.omnigraph.cli-source="${OMNIGRAPH_CLI_SOURCE}"

# ca-certificates: the omnigraph binaries talk to S3 over TLS.
# python3: the entrypoint (omnigraph_server_entrypoint.py) is Python and uses
# the standard library's urllib.request (for the Kubernetes API) and secrets,
# neither of which imports under python3-minimal.
# python3-yaml: the entrypoint renders the Cedar bundles' group membership from
# the mounted actor-token map before converging (see policy/render_groups.py).
# Deliberately a real YAML parser rather than a sed/awk rewrite of a file that
# decides who can write the graph — a silent mis-render here denies every user
# at once.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates python3 python3-yaml \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --user-group --create-home omnigraph

COPY --from=cli /out/omnigraph /usr/local/bin/omnigraph
COPY --from=fetch /out/omnigraph-server /usr/local/bin/omnigraph-server
COPY docker/omnigraph_server_entrypoint.py /usr/local/bin/omnigraph-server-entrypoint.py
# The maintenance window and the pre-deploy plan gate, which the `omnigraph`
# stack's CronJob and Job run as `command`. Both import the entrypoint for its
# Lease, pod check and CLI wrappers, hence the importable name beside them.
COPY docker/omnigraph_maintenance.py docker/omnigraph_plan_gate.py /usr/local/bin/
COPY mcp/servers/witan/policy/render_groups.py /usr/local/bin/render-policy-groups.py

# The cluster config dir holds the baked-in schema.pg, the ConfigMap-mounted
# cluster.yaml (added at deploy time), and the policy bundles the entrypoint
# re-renders in place on every boot — so it must be owned by the non-root
# runtime user. Keep this path in lockstep with CLUSTER_CONFIG_DIR in
# ol-infrastructure src/ol_infrastructure/applications/omnigraph/data_tier.py.
ENV OMNIGRAPH_CLUSTER_DIR=/etc/omnigraph/cluster
RUN mkdir -p "${OMNIGRAPH_CLUSTER_DIR}" \
    && chmod +x /usr/local/bin/omnigraph-server-entrypoint.py \
    && ln -s omnigraph-server-entrypoint.py /usr/local/bin/omnigraph_server_entrypoint.py \
    && chmod +x /usr/local/bin/omnigraph_maintenance.py /usr/local/bin/omnigraph_plan_gate.py \
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
ENTRYPOINT ["/usr/local/bin/omnigraph-server-entrypoint.py"]
# Overridden by the `omnigraph` stack's Deployment `args`
# (--cluster /etc/omnigraph/cluster --bind 0.0.0.0:8080); this default keeps the
# image runnable standalone and documents the expected invocation.
CMD ["--cluster", "/etc/omnigraph/cluster", "--bind", "0.0.0.0:8080"]
