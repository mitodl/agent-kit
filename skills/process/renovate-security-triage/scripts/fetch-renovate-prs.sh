#!/usr/bin/env bash
# Enumerate open Renovate PRs in <org>, restricted to the repos listed in an
# active-repos.sh output file.
#
# READ-ONLY. Queries only -- this skill never mutates GitHub.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "${script_dir}/lib.sh"

usage() {
  cat >&2 <<'USAGE'
Usage: fetch-renovate-prs.sh <org> [active-repos.json] [output.json] [options]

Defaults to reading $active_json and writing $renovate_json (see paths.sh) --
prefer that form, so the command string stays identical between runs.

Options:
  --author SPEC   PR author to match (default: app/renovate). Use
                  "app/dependabot" for Dependabot, or a self-hosted bot login.
  --all-repos     Ignore the active-repos filter and take the whole org.

Emits: [{repo, number, title, url, isDraft, createdAt, updatedAt,
         security_marked}]
USAGE
  exit 1
}

author="app/renovate"
all_repos=false
positional=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --author) [[ $# -ge 2 ]] || usage; author="$2"; shift 2 ;;
    --all-repos) all_repos=true; shift ;;
    -h|--help) usage ;;
    *) positional+=("$1"); shift ;;
  esac
done

org="${positional[0]:-}"
active="${positional[1]:-$active_json}"
output="${positional[2]:-$renovate_json}"
[[ -z "$org" ]] && usage
if [[ "$all_repos" == false && ! -f "$active" ]]; then
  echo "Error: active-repos file not found: ${active}" >&2
  exit 1
fi

limit=1000
raw="$(mktemp)"
trap 'rm -f "$raw"' EXIT

# One org-wide search rather than per-repo iteration. `--author app/renovate`
# is the App-author form; the underlying PR author login is "renovate[bot]".
gh search prs --owner "$org" --author "$author" --state open \
  --json number,title,url,repository,isDraft,createdAt,updatedAt \
  --limit "$limit" > "$raw"

found="$(jq 'length' "$raw")"
# GitHub's search API caps out at 1000 results. If we hit the limit the result
# is silently truncated, which would read as "these are all of them".
if [[ "$found" -ge "$limit" ]]; then
  echo "Warning: hit the ${limit}-result search cap; results are truncated. Narrow with --author or run per-repo." >&2
fi

if [[ "$all_repos" == true ]]; then
  repo_filter='[]'
else
  repo_filter="$(jq '[.[].repo]' "$active")"
fi

jq --argjson repos "$repo_filter" --argjson all "$all_repos" '
  map({
    repo: .repository.nameWithOwner,
    number, title, url, isDraft, createdAt, updatedAt,
    # Renovate appends "[SECURITY]" to the title when the update closes a
    # vulnerability alert. Labels are NOT reliable for this -- every Renovate
    # PR sampled in mitodl had an empty label list.
    security_marked: (.title | test("\\[SECURITY\\]"))
  })
  | if $all then . else map(select(.repo as $r | $repos | index($r))) end
  | sort_by(.repo, .number)
' "$raw" > "$output"

kept="$(jq 'length' "$output")"
sec="$(jq '[.[] | select(.security_marked)] | length' "$output")"
echo "Found ${found} open ${author} PR(s) in ${org}; ${kept} in active repos (${sec} marked [SECURITY]) -> $output" >&2
