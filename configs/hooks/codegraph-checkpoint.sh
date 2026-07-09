#!/usr/bin/env bash
# codegraph-checkpoint.sh
#
# Stop hook: opportunistically compact the current repo's Layer-2 code-graph
# store(s) in the background. Every witan-code write (PostToolUse single-file
# reindex, SessionStart full index) appends a tiny Lance fragment + manifest
# version, and an un-compacted store bloats until *opening* it dominates query
# latency — the same failure mode witan's own store hit (#98). This spawns a
# throttled, detached `witan-code optimize` (at most once per
# WITAN_CODE_OPTIMIZE_INTERVAL, default daily) for the current repo's store
# and the shared cross-repo bridge store.
#
# Independent of witan's own `witan session-checkpoint` Stop hook (no
# cross-package coupling) — register it alone for a witan-code-only install.
#
# Best-effort and non-blocking: always exits 0, silences all output, and never
# delays the Stop hook — the underlying optimize (if due) is spawned detached.
#
# Install: symlink to ~/.claude/hooks/codegraph-checkpoint.sh
# Register in settings.json under hooks.Stop.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PKG_DIR="${SCRIPT_DIR}/../../mcp/servers/witan-code"

if command -v witan-code &>/dev/null; then
	witan-code checkpoint >/dev/null 2>&1 || true
elif command -v uvx &>/dev/null && [[ -d "$PKG_DIR" ]]; then
	uvx --from "$PKG_DIR" witan-code checkpoint >/dev/null 2>&1 || true
fi

exit 0
