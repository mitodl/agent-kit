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

# The accumulated logs are the one payload here that grows without bound, so
# they reach jq as a file rather than a command-line argument: Linux caps a
# single argument at 128 KiB, and seven failing workflow runs at the
# truncation cap below is enough for one `--argjson` to abort the fetch with
# "argument list too long". `--slurpfile` reads the same JSON from a file and
# wraps it in an outer array, hence the `[0]` on each use. An individual log
# stays an `--arg`, since max_log_chars already bounds it well under the cap.
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT
checks_file="${work_dir}/checks.json"
logs_file="${work_dir}/logs.json"
tmp_file="${work_dir}/tmp.json"

gh pr checks "$pr" -R "$repo" \
  --json name,bucket,state,description,workflow,link,startedAt,completedAt \
  > "$checks_file"

if [[ "$include_passing" == false ]]; then
  jq '[.[] | select(.bucket != "pass" and .bucket != "skipping")]' "$checks_file" > "$tmp_file"
  mv "$tmp_file" "$checks_file"
fi

jq '[.[] | . + {
  run_id: (.link | if test("/actions/runs/[0-9]+") then (capture("/actions/runs/(?<id>[0-9]+)").id | tonumber) else null end)
}]' "$checks_file" > "$tmp_file"
mv "$tmp_file" "$checks_file"

# Pull failed-step logs for GitHub Actions checks; dedupe by run id since one
# workflow run can produce several job-level checks.
echo '{}' > "$logs_file"
max_log_chars=20000
while read -r run_id; do
  [[ -z "$run_id" ]] && continue
  raw_log="$(gh run view "$run_id" -R "$repo" --log-failed 2>/dev/null || echo "(failed to fetch log)")"
  if [[ ${#raw_log} -gt $max_log_chars ]]; then
    log="[...truncated, showing last ${max_log_chars} chars...]
$(printf '%s' "$raw_log" | tail -c "$max_log_chars")"
  else
    log="$raw_log"
  fi
  jq -n --slurpfile acc "$logs_file" --arg id "$run_id" --arg log "$log" \
    '$acc[0] + {($id): $log}' > "$tmp_file"
  mv "$tmp_file" "$logs_file"
done < <(jq -r '[.[] | select(.bucket == "fail" and .run_id != null) | .run_id] | unique[]' "$checks_file")

jq -n \
  --arg repo "$repo" \
  --argjson pr "$pr" \
  --slurpfile checks "$checks_file" \
  --slurpfile action_run_logs "$logs_file" \
  '{repo: $repo, pr: $pr, checks: $checks[0], action_run_logs: $action_run_logs[0]}' \
  > "$output"

fail_count="$(jq '[.[] | select(.bucket == "fail")] | length' "$checks_file")"
pending_count="$(jq '[.[] | select(.bucket == "pending")] | length' "$checks_file")"
echo "Fetched $(jq 'length' "$checks_file") check(s) for ${repo}#${pr}: ${fail_count} failing, ${pending_count} pending" >&2
