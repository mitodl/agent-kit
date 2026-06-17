#!/usr/bin/env bash
# workflow-context-inject.sh
#
# UserPromptSubmit hook: inject active workflow projects AND ready tasks for the
# current repository into every Claude Code prompt.
#
# Output is injected as <context> by the Claude Code harness. When there are no
# active projects and no ready tasks, this script emits nothing (exit 0 silently).
#
# Install: symlink to ~/.claude/hooks/workflow-context-inject.sh
# Register in settings.json under hooks.UserPromptSubmit

set -euo pipefail

# Locate the omnigraph-memory queries dir relative to this script.
# Adjust if the agent-kit repo is installed elsewhere.
# Resolve the real script location even when invoked via a symlink in ~/.claude/hooks.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
QUERIES_DIR="${SCRIPT_DIR}/../../mcp/servers/omnigraph-memory/queries"

OMNIGRAPH_URI="${OMNIGRAPH_MEMORY_URI:-${HOME}/.local/share/omnigraph-memory/graph.omni}"
OMNIGRAPH_TOKEN="${OMNIGRAPH_MEMORY_TOKEN:-}"

# Detect repo key from git remote (mirrors repo.py::_normalise — canonical HTTPS URI)
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
GIT_REMOTE=$(git -C "$PROJECT_DIR" remote get-url origin 2>/dev/null) || exit 0
[[ -z "$GIT_REMOTE" ]] && exit 0

REPO_SLUG=$(python3 - "$GIT_REMOTE" <<'PYEOF'
import re, sys
# Canonical HTTPS project URI — must match omnigraph_memory/repo.py::_normalise.
url = re.sub(r'\.git$', '', sys.argv[1].strip()).rstrip('/')
if m := re.match(r'(?:ssh://)?[^@]+@([^:/]+)[:/](.+)', url):
    print(f"https://{m.group(1)}/{m.group(2)}")
elif m := re.match(r'https?://(?:[^@/]+@)?([^/]+)/(.+)', url):
    print(f"https://{m.group(1)}/{m.group(2)}")
else:
    print(url)
PYEOF
) || exit 0

[[ -z "$REPO_SLUG" ]] && exit 0

# Helper to run a read query; prints "[]" on any failure so the formatter can
# still produce the other section.
run_query() {
    local name="$1" params="$2"
    # omnigraph CLI auth is via the OMNIGRAPH_SERVER_BEARER_TOKEN env var, not a
    # --token flag. Pass the token through when set.
    OMNIGRAPH_SERVER_BEARER_TOKEN="${OMNIGRAPH_TOKEN:-${OMNIGRAPH_SERVER_BEARER_TOKEN:-}}" \
        omnigraph query --store "$OMNIGRAPH_URI" --query "${QUERIES_DIR}/read.gq" "$name" \
        --params "$params" --format json 2>/dev/null || echo "[]"
}

PROJECTS_RAW=$(run_query list_projects_by_repo_status \
    "{\"repo\": \"${REPO_SLUG}\", \"status\": \"active\"}")
TASKS_RAW=$(run_query list_tasks_by_repo "{\"repo\": \"${REPO_SLUG}\"}")

python3 - "$PROJECTS_RAW" "$TASKS_RAW" <<'PYEOF'
import json, sys

_PRIORITY = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}


def rows(raw):
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        items = data.get("rows")
        if not isinstance(items, list):
            return []
    elif isinstance(data, list):
        items = data
    else:
        return []
    # Strip alias prefix: "p.slug" -> "slug"
    return [{k.split(".", 1)[-1]: v for k, v in row.items()} for row in items]


projects = rows(sys.argv[1])
tasks = rows(sys.argv[2])

# Ready work: not-yet-started tasks whose blockers are all closed (matches the
# server's task_ready: status open OR blocked, every blocker closed).
status_by_slug = {t["slug"]: t.get("status") for t in tasks}
ready = [
    t for t in tasks
    if t.get("status") in ("open", "blocked")
    and all(status_by_slug.get(b, "closed") == "closed" for b in (t.get("blocked_by") or []))
]
ready.sort(key=lambda t: _PRIORITY.get(t.get("priority"), 9))

lines = []

if projects:
    lines += [
        "## Active Workflow Projects",
        "",
        f"This repository has {len(projects)} active tracked project(s):",
        "",
    ]
    for p in projects[:3]:
        lines.append(f"- **{p['title']}** (slug: `{p['slug']}`)")
        lines.append(f"  Phase: {p['phase']}")
        if p.get("github_issue"):
            lines.append(f"  Issue: {p['github_issue']}")
        if p.get("tags"):
            lines.append(f"  Tags: {', '.join(p['tags'])}")
    lines += [
        "",
        "If this session is contributing to one of the projects above, call",
        "`workflow_session_start` with the matching slug and the current phase",
        "before doing substantive work.",
        "",
    ]

if ready:
    lines += [
        "## Ready Tasks",
        "",
        f"{len(ready)} task(s) are ready to work (no open blockers):",
        "",
    ]
    for t in ready[:5]:
        ext = f" · {t['external_uri']}" if t.get("external_uri") else ""
        lines.append(f"- `[{t.get('priority', 'p2')}]` **{t['title']}** (slug: `{t['slug']}`){ext}")
    lines += [
        "",
        "Use `task_update`/`task_close` (or the `/task` skill) to claim and progress them.",
        "",
    ]

if not lines:
    sys.exit(0)

if projects:
    lines.append("If this is unrelated work, ignore the above.")

print("\n".join(lines))
PYEOF
