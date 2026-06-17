#!/usr/bin/env bash
# workflow-session-checkpoint.sh
#
# Stop hook: auto-close the active WorkflowSession when Claude Code stops.
#
# Reads the session state file written by workflow_session_start and calls
# update_workflow_session_end with a placeholder summary. If workflow_session_end
# was already called explicitly by the agent, the state file will already be
# gone and this script exits cleanly.
#
# Install: symlink to ~/.claude/hooks/workflow-session-checkpoint.sh
# Register in settings.json under hooks.Stop

set -euo pipefail

# Resolve the real script location even when invoked via a symlink in ~/.claude/hooks.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
QUERIES_DIR="${SCRIPT_DIR}/../../mcp/servers/omnigraph-memory/queries"

OMNIGRAPH_URI="${OMNIGRAPH_MEMORY_URI:-${HOME}/.local/share/omnigraph-memory/graph.omni}"
OMNIGRAPH_TOKEN="${OMNIGRAPH_MEMORY_TOKEN:-}"

SESSION_ID="${CLAUDE_SESSION_ID:-}"
[[ -z "$SESSION_ID" ]] && exit 0

STATE_FILE="${TMPDIR:-/tmp}/workflow-session-${SESSION_ID}.json"
[[ -f "$STATE_FILE" ]] || exit 0

SESSION_SLUG=$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get('session_slug', ''))
" "$STATE_FILE" 2>/dev/null) || exit 0

[[ -z "$SESSION_SLUG" ]] && exit 0

# Collect changed files from git diff (best-effort)
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
FILES_JSON=$(git -C "$PROJECT_DIR" diff --name-only HEAD 2>/dev/null \
    | head -50 \
    | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().splitlines()))" \
    2>/dev/null || echo "null")

NOW=$(python3 -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())")

PARAMS=$(python3 -c "
import json, sys
print(json.dumps({
    'slug': sys.argv[1],
    'summary': 'Session ended (auto-closed by Stop hook — call workflow_session_end explicitly for a better summary)',
    'tools_used': None,
    'files_changed': json.loads(sys.argv[2]),
    'ended_at': sys.argv[3],
}))
" "$SESSION_SLUG" "$FILES_JSON" "$NOW" 2>/dev/null) || { rm -f "$STATE_FILE"; exit 0; }

# omnigraph CLI auth is via OMNIGRAPH_SERVER_BEARER_TOKEN, not a --token flag.
OMNIGRAPH_SERVER_BEARER_TOKEN="${OMNIGRAPH_TOKEN:-${OMNIGRAPH_SERVER_BEARER_TOKEN:-}}" \
    omnigraph mutate --store "$OMNIGRAPH_URI" \
    --query "${QUERIES_DIR}/mutations.gq" update_workflow_session_end \
    --params "$PARAMS" 2>/dev/null || true

rm -f "$STATE_FILE"
