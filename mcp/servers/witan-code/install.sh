#!/usr/bin/env bash
# install.sh — Ensure the omnigraph binary is present for the Layer-2 code graph.
#
# Unlike witan, code stores are per-repo and created LAZILY on first
# index (the indexer runs `omnigraph init --schema code-schema.pg <store>` when
# the store is missing). This script only verifies the binary and prints the
# one-shot index hint.
#
# Usage:
#   ./install.sh
#
# Override the code store directory (default ~/.local/share/witan/code):
#   export WITAN_CODE_DIR=/path/to/code-stores
set -euo pipefail

CODE_DIR="${WITAN_CODE_DIR:-${HOME}/.local/share/witan/code}"
SCHEMA_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/schema/code-schema.pg"

echo "==> Checking for omnigraph binary..."
if ! command -v omnigraph &>/dev/null; then
	echo "    Not found — installing from GitHub releases..."
	curl -fsSL https://raw.githubusercontent.com/ModernRelay/omnigraph/main/scripts/install.sh | bash
	export PATH="${HOME}/.local/bin:${PATH}"
fi
echo "    omnigraph: $(omnigraph version)"

echo ""
echo "==> Preparing code-store directory at ${CODE_DIR}..."
mkdir -p "${CODE_DIR}"
echo "    Ready. Per-repo stores (<slug>.omni) are created lazily on first index."
echo "    Schema: ${SCHEMA_FILE}"

echo ""
echo "Next steps:"
echo "  1. Add the MCP server to your agent config:"
echo "     pi:      copy config/pi.json into ~/.pi/agent/mcp.json"
echo "     Claude:  copy config/claude.json into claude_desktop_config.json"
echo "     Copilot: copy config/copilot.json into .vscode/mcp.json"
echo ""
echo "  2. Build the code graph for the current repo (one-shot):"
echo "     uvx --from . witan-code index"
echo "     # or, once installed on PATH:  witan-code index"
echo ""
echo "  3. Optional — keep it fresh automatically via the PostToolUse hook:"
echo "     configs/hooks/codegraph-reindex.sh"
echo ""
echo "Done."
