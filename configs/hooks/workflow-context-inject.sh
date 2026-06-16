#!/usr/bin/env bash
# workflow-context-inject.sh
#
# UserPromptSubmit hook: inject active workflow project context into every
# Claude Code prompt for the current repository.
#
# Output is injected as <context> by the Claude Code harness. When there are
# no active projects, this script emits nothing (exit 0 silently).
#
# Install: symlink to ~/.claude/hooks/workflow-context-inject.sh
# Register in settings.json under hooks.UserPromptSubmit

set -euo pipefail

# Locate the omnigraph-memory queries dir relative to this script.
# Adjust if the agent-kit repo is installed elsewhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUERIES_DIR="${SCRIPT_DIR}/../../mcp/servers/omnigraph-memory/queries"

OMNIGRAPH_URI="${OMNIGRAPH_MEMORY_URI:-${HOME}/.local/share/omnigraph-memory/graph.omni}"
OMNIGRAPH_TOKEN="${OMNIGRAPH_MEMORY_TOKEN:-}"

# Detect repo slug from git remote (mirrors repo.py logic)
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
GIT_REMOTE=$(git -C "$PROJECT_DIR" remote get-url origin 2>/dev/null) || exit 0
[[ -z "$GIT_REMOTE" ]] && exit 0

REPO_SLUG=$(python3 - "$GIT_REMOTE" <<'PYEOF'
import re, sys
url = sys.argv[1].strip().rstrip('/')
url = re.sub(r'\.git$', '', url)
if m := re.match(r'git@([^:]+):(.+)', url):
    print(f"{m.group(1)}/{m.group(2)}")
elif m := re.match(r'https?://([^/]+)/(.+)', url):
    print(f"{m.group(1)}/{m.group(2)}")
else:
    print(url)
PYEOF
) || exit 0

[[ -z "$REPO_SLUG" ]] && exit 0

# Build omnigraph args
OG_ARGS=(--store "$OMNIGRAPH_URI" --query "${QUERIES_DIR}/read.gq" list_projects_by_repo_status)
if [[ -n "$OMNIGRAPH_TOKEN" ]]; then
    OG_ARGS+=(--token "$OMNIGRAPH_TOKEN")
fi

PROJECTS_RAW=$(omnigraph query "${OG_ARGS[@]}" \
    --params "{\"repo\": \"${REPO_SLUG}\", \"status\": \"active\"}" \
    --format json 2>/dev/null) || exit 0

# Emit nothing if no active projects
COUNT=$(python3 -c "
import json,sys
d=json.loads(sys.argv[1])
rows = d.get('rows', d) if isinstance(d, dict) else d
print(len(rows))
" "$PROJECTS_RAW" 2>/dev/null) || exit 0
[[ "$COUNT" -eq 0 ]] && exit 0

python3 - "$PROJECTS_RAW" <<'PYEOF'
import json, sys

raw = json.loads(sys.argv[1])
rows = raw.get('rows', raw) if isinstance(raw, dict) else raw
# Strip alias prefix: "p.slug" -> "slug"
projects = [{k.split('.', 1)[-1]: v for k, v in row.items()} for row in rows]

lines = [
    "## Active Workflow Projects",
    "",
    f"This repository has {len(projects)} active tracked project(s):",
    "",
]

for p in projects[:3]:
    lines.append(f"- **{p['title']}** (slug: `{p['slug']}`)")
    lines.append(f"  Phase: {p['phase']}")
    if p.get("githubIssue"):
        lines.append(f"  Issue: {p['githubIssue']}")
    if p.get("tags"):
        lines.append(f"  Tags: {', '.join(p['tags'])}")

lines += [
    "",
    "If this session is contributing to one of the projects above, call",
    "`workflow_session_start` with the matching slug and the current phase",
    "before doing substantive work.",
    "",
    "If this is unrelated work, ignore the above.",
]

print("\n".join(lines))
PYEOF
