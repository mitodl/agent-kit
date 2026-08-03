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
#                       issues, discussions_opened, discussion_comments
#
#   meta.today      — the date the script was run
#   meta.yesterday  — previous weekday (Friday if today is Monday)
#   meta.tomorrow   — next weekday (Monday if today is Friday)
#   meta.since      — ISO timestamp: midnight UTC on meta.yesterday (fetch window start)
#
# Every PR/issue carries createdAt, updatedAt, and closedAt (null when still
# open); each authored PR that is still open also carries reviewDecision and
# reviewRequests. All timestamps are UTC.
#
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
	*)
		echo "Usage: $0 [-t YYYY-MM-DD] [-o org1,org2]" >&2
		exit 1
		;;
	esac
done

# ── Weekday helpers ───────────────────────────────────────────────────────────

# Portable previous weekday (GNU date and BSD date compatible)
# Returns the most recent weekday before $TODAY (skips Saturday/Sunday).
_prev_weekday() {
	local ref="$1"
	local dow
	# %u: 1=Mon … 7=Sun
	dow="$(date -u -d "$ref" +%u 2>/dev/null || date -u -j -f "%Y-%m-%d" "$ref" +%u)"
	local offset
	case "$dow" in
	1) offset=3 ;; # Monday → Friday
	*) offset=1 ;;
	esac
	date -u -d "$ref -${offset} days" +%Y-%m-%d 2>/dev/null ||
		date -u -v-"${offset}"d -j -f "%Y-%m-%d" "$ref" +%Y-%m-%d
}

# Portable next weekday
_next_weekday() {
	local ref="$1"
	local dow
	dow="$(date -u -d "$ref" +%u 2>/dev/null || date -u -j -f "%Y-%m-%d" "$ref" +%u)"
	local offset
	case "$dow" in
	5) offset=3 ;; # Friday → Monday
	6) offset=2 ;; # Saturday → Monday (edge case)
	7) offset=1 ;; # Sunday → Monday (edge case)
	*) offset=1 ;;
	esac
	date -u -d "$ref +${offset} days" +%Y-%m-%d 2>/dev/null ||
		date -u -v+"${offset}"d -j -f "%Y-%m-%d" "$ref" +%Y-%m-%d
}

YESTERDAY="$(_prev_weekday "$TODAY")"
TOMORROW="$(_next_weekday "$TODAY")"

# Fetch window: midnight UTC on the previous weekday.
# Wide enough to catch all activity from yesterday and today.
SINCE="${YESTERDAY}T00:00:00Z"

USER_JSON="$(gh api user 2>/dev/null || echo '{}')"
USERNAME="$(jq -r '.login // empty' <<<"$USER_JSON")"
DISPLAY_NAME="$(jq -r '.name // empty' <<<"$USER_JSON")"
if [[ -z "$USERNAME" ]]; then
	echo "Error: could not detect GitHub username; run 'gh auth login' first" >&2
	exit 1
fi
if [[ -z "$DISPLAY_NAME" ]]; then
	DISPLAY_NAME="$USERNAME"
fi

# ── Helpers ───────────────────────────────────────────────────────────────────

IFS=',' read -ra ORG_LIST <<<"$ORGS"

# gh search emits 0001-01-01T00:00:00Z for an unset closedAt. Null it out so a
# still-open item can't read as "closed in year 1".
_NULL_CLOSED='map(if (.closedAt // "") | startswith("0001-01-01") then .closedAt = null else . end)'

# Fetch PRs across all orgs for a given gh search flag, deduplicated by URL.
# createdAt/closedAt are what license the words "opened" and "merged" — see
# SKILL.md "Timestamp discipline". updatedAt alone can only support "worked on".
_search_prs() {
	local flag="$1"
	local since="$2"
	(for org in "${ORG_LIST[@]}"; do
		gh search prs \
			"$flag" "$USERNAME" \
			--owner "$org" \
			--updated ">=${since%T*}" \
			--json number,title,state,url,createdAt,updatedAt,closedAt,isDraft \
			--limit 50 2>/dev/null || echo "[]"
	done) | jq -s "add | unique_by(.url) | $_NULL_CLOSED"
}

# Add reviewDecision + reviewRequests to each open PR in an authored-PR list.
# `gh search` cannot return review state, so every open PR needs its own lookup.
# This is the only reliable "does it still need review" signal: "needs review"
# labels and project fields go stale when reviewers forget to clear them.
_enrich_review_state() {
	local prs="$1"
	local open_urls
	open_urls="$(jq -r '.[] | select(.state == "open") | .url' <<<"$prs")"
	if [[ -z "$open_urls" ]]; then
		echo "$prs"
		return
	fi

	local details="[]" detail
	while IFS= read -r url; do
		detail="$(gh pr view "$url" --json url,reviewDecision,reviewRequests 2>/dev/null || echo '{}')"
		details="$(jq -s 'add' <<<"$details"$'\n'"[$detail]")"
	done <<<"$open_urls"

	# reviewDecision is null when no review has been requested or left at all.
	jq -s '.[0] as $prs | .[1] as $details
    | $prs | map(
        . as $pr
        | (($details[] | select(.url == $pr.url)) // null) as $d
        | if $d then $pr + {
            reviewDecision: ($d.reviewDecision // null),
            reviewRequests: [($d.reviewRequests // [])[] | .login // .slug // .name]
          } else $pr end
      )' <<<"$prs"$'\n'"$details"
}

# Fetch issues across all orgs involving the user, deduplicated by URL.
# Filters out bot-generated noise (Renovate, Dependabot).
_search_issues() {
	local since_date="${1%T*}"
	(for org in "${ORG_LIST[@]}"; do
		gh search issues "involves:$USERNAME" \
			--owner "$org" \
			--updated ">=${since_date}" \
			--json number,title,state,url,createdAt,updatedAt,closedAt,author \
			--limit 50 2>/dev/null || echo "[]"
	done) | jq -s "add | unique_by(.url) | $_NULL_CLOSED | "'map(select(
    (.author.login? // "" | test("\\\\[bot\\\\]$|^renovate$|^dependabot$"; "i") | not) and
    (.title | test("^Dependency Dashboard$|^Renovate Dashboard|^Action Required: Fix Renovate"; "") | not)
  ))'
}

# GitHub search ORs repeated org: qualifiers — "org:mitodl org:openedx".
_org_qualifier() {
	local q=""
	for org in "${ORG_LIST[@]}"; do q+="org:${org} "; done
	echo "${q% }"
}

# Discussions the user OPENED in the window, any category, any org.
# Opening a new discussion is announcement-worthy, so this is deliberately not
# narrowed to the RFC category — design work lands in Ideas and elsewhere too.
_discussions_opened() {
	# shellcheck disable=SC2016  # $q is a GraphQL variable, bound via -f q=
	gh api graphql -f query='
  query($q: String!) {
    search(query: $q, type: DISCUSSION, first: 50) {
      nodes { ... on Discussion {
        number title url createdAt
        category { name }
        repository { nameWithOwner }
      } }
    }
  }' -f q="$(_org_qualifier) author:${USERNAME} created:>=${SINCE%T*}" 2>/dev/null |
		jq '[.data.search.nodes[] | select(. != null)]' 2>/dev/null || echo "[]"
}

# Discussion comments (and replies) the user left in the window.
# Substantive design work often lives in a comment on someone else's thread and
# has no PR or issue attached, so it is invisible without this.
# Check-ins is excluded — those are standup posts, and reporting them would be
# circular.
_discussion_comments() {
	# shellcheck disable=SC2016  # $q is a GraphQL variable, bound via -f q=
	gh api graphql -f query='
  query($q: String!) {
    search(query: $q, type: DISCUSSION, first: 25) {
      nodes { ... on Discussion {
        number title url
        category { name }
        repository { nameWithOwner }
        comments(last: 30) {
          nodes {
            author { login } createdAt url bodyText
            replies(last: 30) { nodes { author { login } createdAt url bodyText } }
          }
        }
      } }
    }
  }' -f q="$(_org_qualifier) commenter:${USERNAME} updated:>=${SINCE%T*}" 2>/dev/null |
		jq --arg username "$USERNAME" --arg since "$SINCE" '
      [ .data.search.nodes[]
        | select(. != null and .category.name != "Check-ins")
        | . as $d
        | [ .comments.nodes[], .comments.nodes[].replies.nodes[] ]
        | .[]
        | select(.author.login == $username and .createdAt >= $since)
        | { repository: $d.repository.nameWithOwner,
            discussion_number: $d.number,
            discussion_title: $d.title,
            discussion_url: $d.url,
            category: $d.category.name,
            createdAt: .createdAt,
            url: .url,
            excerpt: (.bodyText[:300] | gsub("\\s+"; " ")) }
      ]' 2>/dev/null || echo "[]"
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

PRS_AUTHORED="$(_enrich_review_state "$(_search_prs "--author" "$SINCE")")"
PRS_REVIEWED="$(_search_prs "--reviewed-by" "$SINCE")"
ISSUES="$(_search_issues "$SINCE")"
DISCUSSIONS_OPENED="$(_discussions_opened)"
DISCUSSION_COMMENTS="$(_discussion_comments)"
CHECKIN_DISCUSSION="$(_checkin_discussion)"

# ── Emit JSON ─────────────────────────────────────────────────────────────────

jq -n \
	--arg username "$USERNAME" \
	--arg display_name "$DISPLAY_NAME" \
	--arg today "$TODAY" \
	--arg yesterday "$YESTERDAY" \
	--arg tomorrow "$TOMORROW" \
	--arg since "$SINCE" \
	--argjson prs_authored "$PRS_AUTHORED" \
	--argjson prs_reviewed "$PRS_REVIEWED" \
	--argjson issues "$ISSUES" \
	--argjson discussions_opened "$DISCUSSIONS_OPENED" \
	--argjson discussion_comments "$DISCUSSION_COMMENTS" \
	--argjson checkin_discussion "$CHECKIN_DISCUSSION" \
	'{
    meta: {
      username:     $username,
      display_name: $display_name,
      today:        $today,
      yesterday:    $yesterday,
      tomorrow:     $tomorrow,
      since:        $since
    },
    checkin_discussion:  $checkin_discussion,
    prs_authored:        $prs_authored,
    prs_reviewed:        $prs_reviewed,
    issues:              $issues,
    discussions_opened:  $discussions_opened,
    discussion_comments: $discussion_comments
  }'
