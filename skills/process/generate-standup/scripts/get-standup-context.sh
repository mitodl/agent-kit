#!/usr/bin/env bash
# get-standup-context.sh
# Fetches GitHub activity for daily standup generation.
#
# Usage:
#   bash skills/process/generate-standup/scripts/get-standup-context.sh [OPTIONS]
#
# Options:
#   -t YYYY-MM-DD   "Today" date (default: today UTC)
#   -o ORGS         Comma-separated list of GitHub orgs to search
#                   (default: mitodl,openedx)
#
# Output: JSON — keys: meta, checkin_discussion, prs_authored, prs_reviewed,
#                       issues, rfc_discussions
# Requires: gh (authenticated), jq

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────

TODAY="$(date -u +%Y-%m-%d)"
ORGS="mitodl,openedx"

# ── Argument parsing ──────────────────────────────────────────────────────────

while getopts "t:o:" opt; do
  case "$opt" in
    t) TODAY="$OPTARG" ;;
    o) ORGS="$OPTARG" ;;
    *) echo "Usage: $0 [-t YYYY-MM-DD] [-o org1,org2]" >&2; exit 1 ;;
  esac
done

# 48-hour lookback window (GNU date and BSD date compatible)
SINCE="$(date -u -d "${TODAY} -48 hours" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
       || date -u -v-48H -j -f "%Y-%m-%d" "$TODAY" +%Y-%m-%dT%H:%M:%SZ)"

USERNAME="$(gh api user -q '.login' 2>/dev/null)" || true
if [[ -z "$USERNAME" ]]; then
  echo "Error: could not detect GitHub username; run 'gh auth login' first" >&2
  exit 1
fi

# ── Helpers ───────────────────────────────────────────────────────────────────

IFS=',' read -ra ORG_LIST <<< "$ORGS"

# Fetch PRs across all orgs for a given gh search flag, deduplicated by URL
_search_prs() {
  local flag="$1"
  local since="$2"
  (for org in "${ORG_LIST[@]}"; do
    gh search prs \
      "$flag" "$USERNAME" \
      --owner "$org" \
      --updated ">=$since" \
      --json number,title,state,url,updatedAt,isDraft \
      --limit 50 2>/dev/null || echo "[]"
  done) | jq -s 'add | unique_by(.url)'
}

# Fetch issues across all orgs (authored or commented on), deduplicated by URL.
# Filters out bot-generated noise (Renovate, Dependabot) by author login and
# known title patterns as a fallback when author is unavailable.
_search_issues() {
  local since="$1"
  (for org in "${ORG_LIST[@]}"; do
    gh search issues "involves:$USERNAME" \
      --owner "$org" \
      --updated ">=$since" \
      --json number,title,state,url,updatedAt,author \
      --limit 50 2>/dev/null || echo "[]"
  done) | jq -s 'add | unique_by(.url) | map(select(
    (.author.login // "" | test("\\[bot\\]$|^renovate$|^dependabot$"; "i") | not) and
    (.title | test("^Dependency Dashboard$|^Renovate Dashboard|^Action Required: Fix Renovate"; "") | not)
  ))'
}

# Fetch RFC-category discussions from mitodl/hq created today by the user
_rfc_discussions() {
  gh api graphql -f query='
  query {
    repository(owner: "mitodl", name: "hq") {
      discussions(first: 50, orderBy: {field: CREATED_AT, direction: DESC}) {
        nodes {
          number title url createdAt
          author { login }
          category { name }
        }
      }
    }
  }' 2>/dev/null \
  | jq --arg today "$TODAY" --arg username "$USERNAME" \
      '[.data.repository.discussions.nodes[]
        | select(.category.name == "RFC"
                 and (.createdAt | startswith($today))
                 and .author.login == $username)]' \
  || echo "[]"
}

# Fetch the most recent Check-ins discussion from mitodl/hq (post target)
_checkin_discussion() {
  local result
  result="$(gh api graphql -f query='
  query {
    repository(owner: "mitodl", name: "hq") {
      discussions(first: 50, orderBy: {field: CREATED_AT, direction: DESC}) {
        nodes {
          id number title url createdAt
          category { name }
        }
      }
    }
  }' \
  -q '[.data.repository.discussions.nodes[]
       | select(.category.name | ascii_downcase == "check-ins")] | first' \
  2>/dev/null || true)"
  echo "${result:-null}"
}

# ── Fetch ─────────────────────────────────────────────────────────────────────

echo "Fetching GitHub activity for @${USERNAME} (since=${SINCE}, today=${TODAY}) …" >&2

PRS_AUTHORED="$(_search_prs "--author"      "$SINCE")"
PRS_REVIEWED="$(_search_prs "--reviewed-by" "$SINCE")"
ISSUES="$(_search_issues "$SINCE")"
RFC_DISCUSSIONS="$(_rfc_discussions)"
CHECKIN_DISCUSSION="$(_checkin_discussion)"

# ── Emit JSON ─────────────────────────────────────────────────────────────────

jq -n \
  --arg  username          "$USERNAME" \
  --arg  today             "$TODAY" \
  --arg  since             "$SINCE" \
  --argjson prs_authored        "$PRS_AUTHORED" \
  --argjson prs_reviewed        "$PRS_REVIEWED" \
  --argjson issues              "$ISSUES" \
  --argjson rfc_discussions     "$RFC_DISCUSSIONS" \
  --argjson checkin_discussion  "$CHECKIN_DISCUSSION" \
  '{
    meta: { username: $username, today: $today, since: $since },
    checkin_discussion:  $checkin_discussion,
    prs_authored:        $prs_authored,
    prs_reviewed:        $prs_reviewed,
    issues:              $issues,
    rfc_discussions:     $rfc_discussions
  }'
