#!/usr/bin/env bash
set -euo pipefail

# The Witan MCP package embeds a copy of the workflow skills so users who install
# Witan directly still get the same agent instructions as the top-level skill
# catalog. Keep these mirrors byte-for-byte identical.

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$repo_root"

pairs=(
  "skills/workflow/agent-memory/SKILL.md:mcp/servers/witan/witan/skills/agent-memory/SKILL.md"
  "skills/workflow/project-tracker/SKILL.md:mcp/servers/witan/witan/skills/project-tracker/SKILL.md"
  "skills/workflow/session-start/SKILL.md:mcp/servers/witan/witan/skills/workflow/SKILL.md"
  "skills/workflow/task-tracker/SKILL.md:mcp/servers/witan/witan/skills/task/SKILL.md"
)

failed=0
for pair in "${pairs[@]}"; do
  source_file=${pair%%:*}
  mirror_file=${pair#*:}

  if [[ ! -f "$source_file" ]]; then
    echo "Missing source skill: $source_file" >&2
    failed=1
    continue
  fi

  if [[ ! -f "$mirror_file" ]]; then
    echo "Missing Witan skill mirror: $mirror_file" >&2
    failed=1
    continue
  fi

  if ! cmp -s "$source_file" "$mirror_file"; then
    echo "Witan skill mirror drift: $mirror_file must match $source_file" >&2
    diff -u "$source_file" "$mirror_file" || true
    failed=1
  fi
done

if (( failed )); then
  echo "Witan skill mirror check failed." >&2
  exit 1
fi

echo "Witan skill mirrors are in sync."
