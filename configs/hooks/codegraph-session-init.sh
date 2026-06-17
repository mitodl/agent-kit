#!/usr/bin/env bash
# codegraph-session-init.sh
#
# SessionStart hook: seed/refresh the per-repo Layer-2 code graph in the
# background. The first session in a repo builds the full index; later sessions
# re-hash and skip unchanged files (cheap). Paired with codegraph-reindex.sh
# (PostToolUse), this keeps the whole code graph current with no manual step.
#
# Non-blocking: detaches the indexer and exits 0 immediately, prints nothing
# (no context injection), and never interrupts session start. A missing binary,
# missing package, or parse failure is silently ignored.
#
# Install: symlink to ~/.claude/hooks/codegraph-session-init.sh
# Register in settings.json under hooks.SessionStart.

set -uo pipefail

# SessionStart runs with cwd = project dir; CLAUDE_PROJECT_DIR is also set.
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"

# Only index inside a git repository.
git -C "$PROJECT_DIR" rev-parse --is-inside-work-tree &>/dev/null || exit 0

# Resolve the real script location even when invoked via a symlink.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PKG_DIR="${SCRIPT_DIR}/../../mcp/servers/omnigraph-codegraph"

# Prefer the installed indexer; fall back to uvx from the local package.
if command -v omnigraph-codegraph-index &>/dev/null; then
	INDEX_CMD=(omnigraph-codegraph-index index "$PROJECT_DIR")
elif command -v uvx &>/dev/null && [[ -d "$PKG_DIR" ]]; then
	INDEX_CMD=(uvx --from "$PKG_DIR" omnigraph-codegraph-index index "$PROJECT_DIR")
else
	exit 0
fi

# Per-repo lock dir (atomic mkdir) so overlapping sessions don't index at once.
LOCK="${TMPDIR:-/tmp}/codegraph-init-$(printf '%s' "$PROJECT_DIR" | cksum | cut -d' ' -f1).lock"

# Detach into a new session: build/refresh in the background, return immediately.
# The single-quoted script is deliberate — $lock/$@ are expanded by the inner shell.
# shellcheck disable=SC2016
setsid bash -c '
	lock="$1"; shift
	mkdir "$lock" 2>/dev/null || exit 0
	trap "rmdir \"$lock\" 2>/dev/null" EXIT
	"$@" >/dev/null 2>&1
' _ "$LOCK" "${INDEX_CMD[@]}" >/dev/null 2>&1 </dev/null &

exit 0
