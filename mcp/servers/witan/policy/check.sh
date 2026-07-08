#!/usr/bin/env bash
# Validate + unit-test the witan v1 Cedar policy bundle against the omnigraph
# binary. Used by CI (.github/workflows/witan-tests.yml, job witan-policy) and
# runnable locally: `mcp/servers/witan/policy/check.sh`.
#
# Requires `omnigraph` on PATH. Install it the same way CI does:
#   uv run python -c "from witan.setup import install_omnigraph; install_omnigraph(dry_run=False)"
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

echo "==> policy bundle OK"
