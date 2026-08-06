#!/usr/bin/env bash
# Validate + unit-test the witan v1 Cedar policy bundle against the omnigraph
# binary. Used by CI (.github/workflows/witan-tests.yml, job witan-policy) and
# runnable locally: `mcp/servers/witan/policy/check.sh`.
#
# Requires `omnigraph` on PATH. Install it the same way CI does:
#   uv run python -c "from witan_core.omnigraph_install import install_omnigraph; install_omnigraph(dry_run=False)"
#   export PATH="$HOME/.local/bin:$PATH"
#
# Covers the three per-graph bundles (memory / code-graph / bridge). The
# server-level bundle (server.policy.yaml, graph_list) is NOT exercised here —
# see policy/README.md § "Server-level bundle" for why the offline CLI cannot.
set -euo pipefail

cd "$(dirname "$0")"

# Structural lint of EVERY bundle, server.policy.yaml included. omnigraph's
# `policy validate` below only covers the per-graph bundles; this is the only
# gate the server bundle gets (see README § "Server-level bundle").
echo "==> linting bundle structure"
uv run python lint_bundles.py ./*.policy.yaml

if ! command -v omnigraph >/dev/null 2>&1; then
  echo "error: omnigraph not on PATH — install it first (see this script's header)" >&2
  exit 127
fi

# Fresh state each run — the __cluster/ and graphs/ working dirs are git-ignored.
# graphs/ holds the .omni stores `cluster apply` materializes; remove both so
# every run is hermetic.
rm -rf __cluster graphs

echo "==> converging fixture cluster"
omnigraph cluster import --config . >/dev/null
omnigraph cluster apply --config . >/dev/null

echo "==> validating per-graph bundles"
for g in memory code_example bridge; do
  omnigraph policy validate --cluster . --graph "$g"
done

echo "==> running declarative policy tests"
omnigraph policy test --cluster . --graph memory       --tests tests/memory.tests.yaml
omnigraph policy test --cluster . --graph code_example --tests tests/code-graph.tests.yaml
omnigraph policy test --cluster . --graph bridge       --tests tests/bridge.tests.yaml

# Everything above validates the COMMITTED bundles, whose fixture groups are all
# populated. That is not the shape that deploys: the image entrypoint rewrites
# membership from the live actor-token map (render_groups.py), and a group whose
# service account is not provisioned comes out unpopulated. omnigraph REFUSES TO
# BOOT on an empty group — "policy group '<name>' must not be empty" — so the
# rendered shape has to be validated too, or a bundle that passes every check
# above still crash-loops the data tier. It has done exactly that once.
#
# The token map here deliberately omits `svc-witan`, which is unprovisioned in
# every real environment, so this exercises the pruning path rather than the
# happy one.
echo "==> validating the RENDERED bundles (deployed shape)"
rendered="$(mktemp -d)"
trap 'rm -rf "${rendered}"' EXIT
cp cluster.yaml find.gq ./*.policy.yaml "${rendered}/"
mkdir -p "${rendered}/schema"
cp schema/stub.pg "${rendered}/schema/"
cat > "${rendered}/tokens.json" <<'JSON'
{"act-alice": "t1", "act-bob": "t2", "svc-witan-ci": "t3", "svc-witan-admin": "t4"}
JSON
uv run python render_groups.py \
  --tokens "${rendered}/tokens.json" "${rendered}"/*.policy.yaml
(
  cd "${rendered}"
  omnigraph cluster import --config . >/dev/null
  omnigraph cluster apply --config . >/dev/null
  for g in memory code_example bridge; do
    omnigraph policy validate --cluster . --graph "$g"
  done
)

echo "==> policy bundle OK"
