#!/usr/bin/env bash
# install.sh — Install omnigraph binary and initialise the local agent-memory graph.
#
# Usage:
#   ./install.sh                  # local-disk mode (default)
#   RUSTFS=1 ./install.sh         # local RustFS/S3 mode (requires Docker)
#
# After running, set WITAN_MEMORY_URI if you want a non-default graph path:
#   export WITAN_MEMORY_URI=s3://omnigraph-local/agent-memory/
set -euo pipefail

GRAPH_DIR="${WITAN_DATA_DIR:-${HOME}/.local/share/witan}"
GRAPH_PATH="${GRAPH_DIR}/graph.omni"
SCHEMA_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/schema/schema.pg"

echo "==> Checking for omnigraph binary..."
if ! command -v omnigraph &>/dev/null; then
	echo "    Not found — installing from GitHub releases..."
	curl -fsSL https://raw.githubusercontent.com/ModernRelay/omnigraph/main/scripts/install.sh | bash
	# The installer puts binaries in ~/.local/bin; ensure it's on PATH.
	export PATH="${HOME}/.local/bin:${PATH}"
fi
echo "    omnigraph: $(omnigraph version)"

# ── Local RustFS mode ─────────────────────────────────────────────
if [[ "${RUSTFS:-}" == "1" ]]; then
	echo ""
	echo "==> Starting local RustFS (S3-compatible) storage..."
	echo "    Requires Docker. This may take a minute on first run."
	BUCKET=omnigraph-local \
		PREFIX=agent-memory \
		BIND=127.0.0.1:8081 \
		curl -fsSL https://raw.githubusercontent.com/ModernRelay/omnigraph/main/scripts/local-rustfs-bootstrap.sh | bash
	echo ""
	echo "    RustFS running. Set:"
	echo "    export WITAN_MEMORY_URI=s3://omnigraph-local/agent-memory/"
	echo "    export AWS_ACCESS_KEY_ID=rustfsadmin"
	echo "    export AWS_SECRET_ACCESS_KEY=rustfsadmin"
	echo "    export AWS_REGION=us-east-1"
	echo "    export AWS_ENDPOINT_URL=http://127.0.0.1:9000"
	echo "    export AWS_ENDPOINT_URL_S3=http://127.0.0.1:9000"
	echo "    export AWS_ALLOW_HTTP=true"
	echo "    export AWS_S3_FORCE_PATH_STYLE=true"
	exit 0
fi

# ── Local-disk mode ───────────────────────────────────────────────
echo ""
echo "==> Initialising local graph at ${GRAPH_PATH}..."

if [[ -d "${GRAPH_PATH}" ]]; then
	echo "    Graph already exists — planned schema changes (applied below):"
	omnigraph schema plan --schema "${SCHEMA_FILE}" "${GRAPH_PATH}" || true
else
	mkdir -p "${GRAPH_DIR}"
	omnigraph init --schema "${SCHEMA_FILE}" "${GRAPH_PATH}"
	echo "    Graph initialised."
fi

echo ""
echo "==> Applying schema + building indexes..."
# Idempotent + additive: migrates an existing graph to new columns/indexes and
# builds the FTS/BTREE indexes the search queries need.
omnigraph schema apply --schema "${SCHEMA_FILE}" "${GRAPH_PATH}"

echo ""
echo "Done."
echo ""
echo "Next steps:"
echo "  1. Add the MCP server to your agent config:"
echo "     pi:      copy config/pi.json into ~/.pi/agent/mcp.json"
echo "     Claude:  copy config/claude.json into claude_desktop_config.json"
echo "     Copilot: copy config/copilot.json into .vscode/mcp.json"
echo ""
echo "  2. Optional — override defaults in your shell profile:"
echo "     export WITAN_MEMORY_URI=${GRAPH_PATH}"
echo "     export WITAN_AUTHOR=\$(git config user.name)"
