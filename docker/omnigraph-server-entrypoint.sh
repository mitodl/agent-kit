#!/bin/sh
# Entrypoint for the omnigraph-server data-tier image.
#
# omnigraph-server boots from an already-converged cluster catalog: it serves
# the graphs that `omnigraph cluster apply` has created under the storage root,
# and it never creates them itself (POST /schema/apply is disabled for
# cluster-backed serving — see omnigraph docs/user/operations/server.md). On a
# fresh S3 storage root nothing exists yet, so the server would come up serving
# zero graphs. This entrypoint closes that gap by converging the cluster in the
# container's own start path, idempotently, before exec'ing the server — the
# remote analogue of witan's local `_ensure_graph` auto-bootstrap
# (tk-automate-cluster-bootstrap-schema-init-main-grap-6bd63b, option a).
#
# The local `__cluster/` state ledger lives under the cluster config dir and is
# ephemeral per pod (it is not persisted). The import -> apply sequence is what
# makes that safe: `import` re-observes the live graphs and rebuilds local state
# to match reality, so a restart against an existing S3 graph reconciles instead
# of trying to recreate it; `apply` then converges any real drift (first-boot
# graph creation, later schema updates) and is a no-op once converged. This is
# the exact recovery path omnigraph's own cli_cluster_e2e
# `lost_state_reimport_recovers_catalog` test exercises.
set -eu

# Discover the cluster config dir from the `--cluster <dir>` argument we forward
# to omnigraph-server, so this stays in lockstep with the deployment's args
# (the `omnigraph` Pulumi stack passes `--cluster /etc/omnigraph/cluster`).
cluster_arg=""
prev=""
for arg in "$@"; do
    if [ "${prev}" = "--cluster" ]; then
        cluster_arg="${arg}"
        break
    fi
    prev="${arg}"
done
cluster_dir="${cluster_arg:-${OMNIGRAPH_CLUSTER_DIR:-/etc/omnigraph/cluster}}"

case "${cluster_dir}" in
    s3://* | http://* | https://*)
        # Config-free serving straight from a storage-root URI: there is no
        # local cluster.yaml to converge, so bootstrap does not apply.
        echo "omnigraph-server: --cluster is a storage URI (${cluster_dir}); skipping cluster bootstrap"
        ;;
    *)
        echo "omnigraph-server: converging cluster catalog at ${cluster_dir}"
        # `apply` never initializes state on its own — it requires an existing
        # `__cluster/state.json` ledger. `import` creates that ledger, but only
        # on a genuinely first boot: once the ledger exists (with
        # `state.backend: cluster` it lives in the storage root and so survives
        # pod restarts) `import` refuses and points at `refresh`. So: import on
        # first boot, else refresh to re-observe the existing ledger; then apply
        # converges (graph creation on first boot, schema updates thereafter,
        # a no-op once converged).
        if omnigraph cluster import --config "${cluster_dir}" >/dev/null 2>&1; then
            echo "omnigraph-server: initialized cluster state (first boot)"
        else
            echo "omnigraph-server: cluster state exists; refreshing from live observations"
            omnigraph cluster refresh --config "${cluster_dir}"
        fi
        omnigraph cluster apply --config "${cluster_dir}"
        echo "omnigraph-server: cluster converged; starting server"
        ;;
esac

exec omnigraph-server "$@"
