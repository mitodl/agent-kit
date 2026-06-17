#!/usr/bin/env bash
# codegraph-reindex.sh
#
# PostToolUse hook (matcher: Edit|Write): incrementally re-index a single source
# file into the Layer-2 code graph after the agent edits it.
#
# Best-effort and non-blocking: always exits 0, never prints to stdout, and
# silences all indexer output. A missing binary, missing package, or parse
# failure must not interrupt the agent.
#
# Reads the Claude Code hook JSON from stdin and extracts
# tool_input.file_path. Only source files with known extensions trigger a
# reindex; everything else is a no-op.
#
# Install: symlink to ~/.claude/hooks/codegraph-reindex.sh
# Register in settings.json under hooks.PostToolUse with matcher "Edit|Write".

set -uo pipefail

# Read hook payload from stdin (may be empty if invoked manually).
INPUT="$(cat 2>/dev/null || true)"
[[ -z "$INPUT" ]] && exit 0

# Extract file_path without requiring jq.
FILE_PATH="$(printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ti = d.get('tool_input') or {}
print(ti.get('file_path') or '')
" 2>/dev/null || true)"

[[ -z "$FILE_PATH" ]] && exit 0
[[ -f "$FILE_PATH" ]] || exit 0

# Only index known source extensions (mirror indexer._EXT_TO_SPEC).
case "$FILE_PATH" in
	*.py|*.pyi|*.ts|*.tsx|*.js|*.jsx|*.mjs|*.cjs) ;;
	*) exit 0 ;;
esac

# Prefer an installed indexer; fall back to uvx from the package subdirectory.
if command -v omnigraph-codegraph-index &>/dev/null; then
	omnigraph-codegraph-index index "$FILE_PATH" >/dev/null 2>&1 || true
elif command -v uvx &>/dev/null; then
	SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
	PKG_DIR="${SCRIPT_DIR}/../../mcp/servers/omnigraph-codegraph"
	if [[ -d "$PKG_DIR" ]]; then
		uvx --from "$PKG_DIR" omnigraph-codegraph-index index "$FILE_PATH" \
			>/dev/null 2>&1 || true
	fi
fi

exit 0
