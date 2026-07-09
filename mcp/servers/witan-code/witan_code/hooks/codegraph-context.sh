#!/usr/bin/env bash
# codegraph-context.sh
#
# UserPromptSubmit hook: print a short code-graph status block (indexed repo,
# file count, last-indexed time, in-progress flag) so an agent knows the code
# graph exists — and when it's still being built — instead of discovering it
# only by trying a code_* tool and getting an empty result. Independent of
# witan's own `witan inject-context` hook (no cross-package coupling): register
# both, or just this one, for a witan-code-only install.
#
# Best-effort and non-blocking: always exits 0, silent on any failure or when
# the current repo has no store and no index in flight.
#
# Install: symlink to ~/.claude/hooks/codegraph-context.sh
# Register in settings.json under hooks.UserPromptSubmit.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PKG_DIR="${SCRIPT_DIR}/../../mcp/servers/witan-code"

if command -v witan-code &>/dev/null; then
	witan-code inject-context 2>/dev/null || true
elif command -v uvx &>/dev/null && [[ -d "$PKG_DIR" ]]; then
	uvx --from "$PKG_DIR" witan-code inject-context 2>/dev/null || true
fi

exit 0
