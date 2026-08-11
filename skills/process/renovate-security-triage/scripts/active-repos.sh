#!/usr/bin/env bash
# Enumerate repos in <org> that one person has personally contributed to inside
# a time window, so downstream Renovate triage can ignore the rest of the org.
# (mitodl has 436 open Renovate PRs across 78 repos; a typical person is active
# in ~15 of them, so this filter is what makes the report readable.)
#
# READ-ONLY. Queries only -- this skill never mutates GitHub.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "${script_dir}/lib.sh"

usage() {
  cat >&2 <<'USAGE'
Usage: active-repos.sh <org> [output.json] [options]

Writes to $active_json (see paths.sh) when no output path is given -- prefer
that form, so the command string stays identical between runs.

Options:
  --since YYYY-MM-DD     Start of the activity window (default: 365 days ago).
  --user LOGIN           Login to measure (default: the authenticated user).
  --min-contributions N  Drop repos with fewer than N total contributions
                         (default: 1). Raise to exclude drive-by fixes.

Emits: [{repo, signals:{commits,prs,reviews,issues,searched}, total}]
       sorted by total, descending.
USAGE
  exit 1
}

since=""
login=""
min_contributions=1
positional=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --since) [[ $# -ge 2 ]] || usage; since="$2"; shift 2 ;;
    --user) [[ $# -ge 2 ]] || usage; login="$2"; shift 2 ;;
    --min-contributions) [[ $# -ge 2 ]] || usage; min_contributions="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) positional+=("$1"); shift ;;
  esac
done

org="${positional[0]:-}"
output="${positional[1]:-$active_json}"
[[ -z "$org" ]] && usage

# GraphQL search qualifiers and contributionsCollection do not accept "@me",
# so resolve a concrete login before building any query.
viewer_login="$(gh api user --jq '.login' 2>/dev/null || true)"
if [[ -z "$viewer_login" ]]; then
  echo "Error: could not detect GitHub login; run 'gh auth login' first" >&2
  exit 1
fi
[[ -z "$login" ]] && login="$viewer_login"

# Portable "N days ago": GNU date first, BSD/macOS date as the fallback.
days_ago() {
  date -u -d "-${1} days" +%Y-%m-%d 2>/dev/null ||
    date -u -v-"${1}"d +%Y-%m-%d
}

today="$(date -u +%Y-%m-%d)"
one_year_ago="$(days_ago 365)"
[[ -z "$since" ]] && since="$one_year_ago"

# contributionsCollection rejects a from->to span wider than one year outright,
# so clamp rather than letting the whole query fail.
if [[ "$since" < "$one_year_ago" ]]; then
  echo "Warning: --since ${since} exceeds GitHub's 1-year contributions window; clamping to ${one_year_ago}" >&2
  since="$one_year_ago"
fi

# contributionsCollection on `viewer` includes private contributions the token
# can see; on `user(login:)` it does not. Use viewer when measuring self.
if [[ "$login" == "$viewer_login" ]]; then
  root='viewer'
else
  root="user(login: \"${login}\")"
fi

by_repo='{ repository { nameWithOwner } contributions { totalCount } }'
contrib_query="
{ ${root} { contributionsCollection(from: \"${since}T00:00:00Z\", to: \"${today}T23:59:59Z\") {
    commitContributionsByRepository(maxRepositories: 100) ${by_repo}
    pullRequestContributionsByRepository(maxRepositories: 100) ${by_repo}
    issueContributionsByRepository(maxRepositories: 100) ${by_repo}
    pullRequestReviewContributionsByRepository(maxRepositories: 100) ${by_repo}
} } }"

echo "Measuring ${login}'s activity in ${org} since ${since}..." >&2

# `gh api graphql` exits non-zero when the response carries an `errors` array,
# and it prints the whole {data, errors} envelope to stdout without running a
# `--jq` filter. Filtering inline with `|| echo '{}'` would therefore append a
# second JSON document to the envelope, and `--argjson contrib` would abort on
# the concatenation instead of taking the search-only fallback below. Capture
# raw, then extract and validate separately (as the advisory lookup does).
contrib_raw="$(gh api graphql -f query="$contrib_query" 2>/dev/null || true)"
contrib="$(jq -ce '(.data.viewer // .data.user).contributionsCollection // empty' \
  <<<"$contrib_raw" 2>/dev/null || true)"
[[ -z "$contrib" ]] && contrib='{}'

if [[ "$contrib" == '{}' ]]; then
  echo "Warning: contributionsCollection returned nothing (token scope, or no activity); falling back to search only" >&2
fi

# contributionsCollection is the primary source -- it is the only surface that
# covers commits, PRs, reviews AND issues in one date-scoped call. But it counts
# only contributions GitHub attributes to the default branch, so union it with
# search, which catches PRs on other branches and review work it misses.
searched="$(
  for flag in --author --reviewed-by; do
    gh search prs --owner "$org" "$flag" "$login" --updated ">=${since}" \
      --json repository --limit 200 2>/dev/null || echo '[]'
  done | jq -s 'add // [] | [.[].repository.nameWithOwner]'
)"

jq -n \
  --arg org "$org" \
  --argjson contrib "$contrib" \
  --argjson searched "$searched" \
  --argjson min "$min_contributions" \
  -f "${jq_dir}/active-repos.jq" > "$output"

echo "Found $(jq 'length' "$output") active repo(s) for ${login} in ${org} -> $output" >&2
