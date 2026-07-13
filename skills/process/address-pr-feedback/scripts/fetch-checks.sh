#!/usr/bin/env bash
# Fetch CI/status checks for a PR: GitHub Actions jobs plus third-party
# checks (pre-commit.ci, GitGuardian, Sentry, CodeQL, etc.). For failing
# GitHub Actions checks, also pulls the failed-step log output so the
# failure can be diagnosed without leaving the terminal. Third-party checks
# have no log to fetch here -- their `link` points at the external service.
set -euo pipefail

usage() {
  echo "Usage: $0 <owner/repo> <pr-number> [output.json] [--include-passing]" >&2
  echo "  output.json defaults to stdout" >&2
  exit 1
}

include_passing=false
positional=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --include-passing) include_passing=true; shift ;;
    -h|--help) usage ;;
    *) positional+=("$1"); shift ;;
  esac
done

repo="${positional[0]:-}"
pr="${positional[1]:-}"
output="${positional[2]:-/dev/stdout}"
[[ -z "$repo" || -z "$pr" ]] && usage

checks="$(gh pr checks "$pr" -R "$repo" \
  --json name,bucket,state,description,workflow,link,startedAt,completedAt 2>/dev/null || echo '[]')"

if [[ "$include_passing" == false ]]; then
  checks="$(echo "$checks" | jq '[.[] | select(.bucket != "pass" and .bucket != "skipping")]')"
fi

checks="$(echo "$checks" | jq '[.[] | . + {
  run_id: (.link | if test("/actions/runs/[0-9]+") then capture("/actions/runs/(?<id>[0-9]+)").id else null end)
}]')"

# Pull failed-step logs for GitHub Actions checks; dedupe by run id since one
# workflow run can produce several job-level checks.
run_ids="$(echo "$checks" | jq -r '[.[] | select(.bucket == "fail" and .run_id != null) | .run_id] | unique[]')"

logs_json='{}'
max_log_chars=20000
for run_id in $run_ids; do
  raw_log="$(gh run view "$run_id" -R "$repo" --log-failed 2>/dev/null || echo "(failed to fetch log)")"
  if [[ ${#raw_log} -gt $max_log_chars ]]; then
    log="[...truncated, showing last ${max_log_chars} chars...]
$(printf '%s' "$raw_log" | tail -c "$max_log_chars")"
  else
    log="$raw_log"
  fi
  logs_json="$(jq -n --argjson acc "$logs_json" --arg id "$run_id" --arg log "$log" '$acc + {($id): $log}')"
done

jq -n \
  --arg repo "$repo" \
  --argjson pr "$pr" \
  --argjson checks "$checks" \
  --argjson action_run_logs "$logs_json" \
  '{repo: $repo, pr: $pr, checks: $checks, action_run_logs: $action_run_logs}' \
  > "$output"

fail_count="$(echo "$checks" | jq '[.[] | select(.bucket == "fail")] | length')"
pending_count="$(echo "$checks" | jq '[.[] | select(.bucket == "pending")] | length')"
echo "Fetched $(echo "$checks" | jq 'length') check(s) for ${repo}#${pr}: ${fail_count} failing, ${pending_count} pending" >&2
