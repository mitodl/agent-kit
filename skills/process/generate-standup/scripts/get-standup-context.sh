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
# Output: JSON — keys: meta, checkin_targets, prs_authored, prs_reviewed,
#                       issues, discussions_opened, discussion_comments
#
#   checkin_targets.eod    — the Check-ins thread titled meta.tomorrow (EOD post target)
#   checkin_targets.bod    — the Check-ins thread titled meta.today (BOD post target)
#   checkin_targets.latest — newest Check-ins thread, for when the above is null
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

# Add review state to each open PR in an authored-PR list, and reduce it to a
# single `needs_review` boolean.
#
# `gh search` cannot return review state, so every open PR needs its own lookup.
# The boolean exists because reviewDecision alone is a footgun: gh reports it as
# "" (not null) when nobody has reviewed, and a draft PR still reports
# REVIEW_REQUIRED — so any rule phrased in terms of the raw field mis-buckets
# something. Decide it here, once, rather than restating the conditions at the
# call site.
_enrich_review_state() {
	local prs="$1"
	local open_urls
	open_urls="$(jq -r '.[] | select(.state == "open") | .url' <<<"$prs")"
	if [[ -z "$open_urls" ]]; then
		# Still emit the documented field, so a merged-only run doesn't leave
		# `needs_review` absent where the caller was promised a boolean.
		jq 'map(. + {needs_review: false})' <<<"$prs"
		return
	fi

	local details="[]" detail
	while IFS= read -r url; do
		# On lookup failure emit just the url, so the merge below can mark the
		# PR review_state_unknown instead of silently omitting the fields.
		detail="$(gh pr view "$url" --json url,reviewDecision,reviewRequests 2>/dev/null ||
			jq -n --arg u "$url" '{url: $u}')"
		details="$(jq -s 'add' <<<"$details"$'\n'"[$detail]")"
	done <<<"$open_urls"

	# gh emits reviewDecision as "" where GraphQL would say null, so normalize
	# before comparing. Bot review requests are dropped: an automated reviewer
	# pending does not mean a human owes one.
	jq -s --argjson bots '["copilot-pull-request-reviewer","copilot","gemini-code-assist","renovate","dependabot","sentry-io","sentry"]' '
    # The `[bot]` suffix is the general signal; the list catches the ones that
    # request review under a bare login. Testing the suffix first matters —
    # rtrimstr alone throws the signal away before comparing, so anything off
    # the list (claude[bot], coderabbitai[bot]) would read as a human.
    def is_bot: ascii_downcase | (endswith("[bot]") or (rtrimstr("[bot]") | IN($bots[])));
    .[0] as $prs | .[1] as $details
    | $prs | map(
        . as $pr
        # first(…) to take one match, // null because first(empty) yields no
        # output at all — without it, map drops every PR lacking a detail row.
        | (first($details[] | select(.url == $pr.url)) // null) as $d
        # $d is null exactly when the PR is not open (only open PRs are looked
        # up), so nobody owes a review on it.
        | if $d == null then $pr + {needs_review: false}
          else
            (if ($d.reviewDecision // "") == "" then null else $d.reviewDecision end) as $decision
            | ($d | has("reviewDecision") | not) as $unknown
            | $pr + {
                reviewDecision: $decision,
                # select(. != null …) first: a reviewer node with none of the
                # three name fields would otherwise reach ascii_downcase as
                # null and abort the whole run.
                reviewRequests: [($d.reviewRequests // [])[]
                                 | (.login // .slug // .name)
                                 | select(. != null and (is_bot | not))],
                review_state_unknown: $unknown,
                needs_review: (
                  if $unknown or ($pr.isDraft // false) then false
                  else $decision == null or $decision == "REVIEW_REQUIRED"
                  end
                )
              }
          end
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
    (.author.login? // "" | test("\\[bot\\]$|^renovate$|^dependabot$"; "i") | not) and
    (.title | test("^Dependency Dashboard$|^Renovate Dashboard|^Action Required: Fix Renovate"; "") | not)
  ))'
}

# GitHub search ORs repeated org: qualifiers — "org:mitodl org:openedx".
_org_qualifier() {
	local q=""
	for org in "${ORG_LIST[@]}"; do q+="org:${org} "; done
	echo "${q% }"
}

# Echo $1 if it is exactly one JSON array, else "[]".
#
# Guards the --argjson calls at the bottom: `gh api graphql` exits non-zero
# whenever the response carries an `errors` array *even when `data` is
# populated* (a SAML-gated org, a lost-access repo, a timeout on a nested
# connection). Under `set -o pipefail` a `cmd | jq || echo "[]"` chain then
# appends "[]" to output jq has already written, and --argjson rejects the
# two-value result — losing the whole run over a partial failure. So capture
# first, filter second, and validate here.
#
# -s (slurp) is what makes this a real guard rather than a restatement of the
# bug: it rejects multi-document input, and a parse error under -s produces no
# output at all, so the `|| echo '[]'` fallback cannot append to a partial write.
_json_array() {
	jq -se 'if length == 1 and (.[0] | type) == "array" then .[0] else error end' \
		<<<"${1:-}" 2>/dev/null || echo '[]'
}

# Warn on stderr when a GraphQL response carries an `errors` array.
#
# A nested connection that trips RESOURCE_LIMITS_EXCEEDED still returns 200 with
# `data` present — but every field it was asked for comes back null, which the
# filters below then discard as "no matches". Undercounting silently is worse
# than a noisy run, so say something rather than emitting a plausible [].
_warn_graphql_errors() {
	local raw="$1" label="$2" msgs
	msgs="$(jq -r '[.errors[]?.message] | unique | map("  - " + .) | .[]' \
		<<<"${raw:-null}" 2>/dev/null || true)"
	if [[ -n "$msgs" ]]; then
		printf 'Warning: %s query returned errors; results may be incomplete:\n%s\n' \
			"$label" "$msgs" >&2
	fi
}

# Discussions the user OPENED in the window, any category, any org.
# Opening a new discussion is announcement-worthy, so this is deliberately not
# narrowed to the RFC category — design work lands in Ideas and elsewhere too.
# Check-ins is excluded for the same reason as in _discussion_comments: the
# standup should not announce the thread it is being posted to.
_discussions_opened() {
	local raw
	# shellcheck disable=SC2016  # $q is a GraphQL variable, bound via -f q=
	raw="$(gh api graphql -f query='
  query($q: String!) {
    search(query: $q, type: DISCUSSION, first: 50) {
      nodes { ... on Discussion {
        number title url createdAt
        category { name }
        repository { nameWithOwner }
      } }
    }
  }' -f q="$(_org_qualifier) author:${USERNAME} sort:updated-desc created:>=${SINCE%T*}" 2>/dev/null || true)"
	_warn_graphql_errors "$raw" "discussions-opened"

	_json_array "$(jq '[.data.search.nodes[]?
      | select(. != null and .category.name != "Check-ins")
      | { repository: .repository.nameWithOwner,
          number, title, url, createdAt,
          category: .category.name }]' <<<"${raw:-null}" 2>/dev/null || true)"
}

# Discussion comments (and replies) the user left in the window.
# Substantive design work often lives in a comment on someone else's thread and
# has no PR or issue attached, so it is invisible without this.
# Check-ins is excluded — those are standup posts, and reporting them would be
# circular. It cannot move server-side: `category:` only works in DISCUSSION
# search alongside `repo:`, and this query is org-scoped.
#
# The nested `last:` values are a node budget, not a preference. GitHub bills
# 25 × 20 × (1 + 10) ≈ 5.5k nodes here; past ~10k the connection trips
# RESOURCE_LIMITS_EXCEEDED, which returns 200 with every requested field nulled
# rather than failing outright. Raising any of the three numbers needs a
# measurement, not a guess.
_discussion_comments() {
	local raw
	# shellcheck disable=SC2016  # $q is a GraphQL variable, bound via -f q=
	raw="$(gh api graphql -f query='
  query($q: String!) {
    search(query: $q, type: DISCUSSION, first: 25) {
      nodes { ... on Discussion {
        number title url
        category { name }
        repository { nameWithOwner }
        comments(last: 20) {
          nodes {
            author { login } createdAt url bodyText
            replies(last: 10) { nodes { author { login } createdAt url bodyText } }
          }
        }
      } }
    }
  }' -f q="$(_org_qualifier) commenter:${USERNAME} sort:updated-desc updated:>=${SINCE%T*}" 2>/dev/null || true)"
	_warn_graphql_errors "$raw" "discussion-comments"

	_json_array "$(jq --arg username "$USERNAME" --arg since "$SINCE" '
      [ .data.search.nodes[]?
        | select(. != null and .category.name != "Check-ins")
        | . as $d
        | [ .comments.nodes[]?, .comments.nodes[]?.replies.nodes[]? ]
        | .[]
        | select(.author.login == $username and .createdAt >= $since)
        | { repository: $d.repository.nameWithOwner,
            discussion_number: $d.number,
            discussion_title: $d.title,
            discussion_url: $d.url,
            category: $d.category.name,
            createdAt: .createdAt,
            url: .url,
            excerpt: ((.bodyText // "")[:300] | gsub("\\s+"; " ")) }
      ]' <<<"${raw:-null}" 2>/dev/null || true)"
}

# Recent Check-ins discussions in mitodl/hq, each annotated with the calendar
# date its title names.
#
# The date has to come from the title, not from createdAt: a thread for day D is
# opened on D-1, so "newest thread" resolves to D's thread or D+1's depending on
# nothing more than what time of day the script runs.
_checkin_discussions() {
	local raw
	raw="$(gh api graphql -f query='
  query {
    repository(owner: "mitodl", name: "hq") {
      discussions(first: 50, orderBy: {field: CREATED_AT, direction: DESC}) {
        nodes {
          id number title url createdAt
          category { name }
          comments { totalCount }
        }
      }
    }
  }' 2>/dev/null || true)"
	_warn_graphql_errors "$raw" "checkin-discussions"

	_json_array "$(jq '
    def month_num: {jan:"01",feb:"02",mar:"03",apr:"04",may:"05",jun:"06",
                    jul:"07",aug:"08",sep:"09",oct:"10",nov:"11",dec:"12"}[.];
    # Titles read "Tuesday, August 11th, 2026". Matching month/day/year only
    # keeps a missing weekday, an abbreviated month, or a dropped ordinal
    # suffix from turning into an unresolvable target.
    # Wrapped in [] because capture emits an empty stream on no match, which
    # would otherwise drop the whole discussion instead of yielding null.
    def title_date:
      [ascii_downcase
       | capture("(?<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\\s+(?<day>\\d{1,2})(st|nd|rd|th)?,?\\s+(?<year>\\d{4})")]
      | if length == 0 then null
        else .[0]
             | "\(.year)-\(.mon | month_num)-\(if (.day | length) == 1 then "0" + .day else .day end)"
        end;
    [ .data.repository.discussions.nodes[]?
      | select(. != null and (.category.name | ascii_downcase) == "check-ins")
      | { id, number, title, url, createdAt,
          date: (.title | title_date),
          comment_count: (.comments.totalCount // 0) } ]' \
		<<<"${raw:-null}" 2>/dev/null || true)"
}

# The Check-ins thread for one calendar date, or null if it does not exist yet.
#
# Duplicate threads for the same day do happen (two "Friday, August 7th, 2026"
# discussions existed). Prefer the one the team actually posted in, then the
# newer of the two — a thread nobody replied to is the abandoned one.
_checkin_target() {
	jq --arg d "$2" '[.[] | select(.date == $d)] | sort_by(.comment_count, .createdAt) | last' \
		<<<"$1"
}

# ── Fetch ─────────────────────────────────────────────────────────────────────

echo "Fetching GitHub activity for @${USERNAME} (since=${SINCE}, today=${TODAY}) …" >&2

# Kept as separate statements: nesting the search inside the enrichment call
# would discard the search's exit status, so `set -e` could not see it fail.
PRS_AUTHORED="$(_search_prs "--author" "$SINCE")"
PRS_AUTHORED="$(_enrich_review_state "$PRS_AUTHORED")"
PRS_REVIEWED="$(_search_prs "--reviewed-by" "$SINCE")"
ISSUES="$(_search_issues "$SINCE")"
DISCUSSIONS_OPENED="$(_discussions_opened)"
DISCUSSION_COMMENTS="$(_discussion_comments)"

CHECKIN_DISCUSSIONS="$(_checkin_discussions)"
CHECKIN_EOD="$(_checkin_target "$CHECKIN_DISCUSSIONS" "$TOMORROW")"
CHECKIN_BOD="$(_checkin_target "$CHECKIN_DISCUSSIONS" "$TODAY")"
CHECKIN_LATEST="$(jq 'sort_by(.createdAt) | last' <<<"$CHECKIN_DISCUSSIONS")"

# Final guard: --argjson dies on an empty or malformed value, which would throw
# away an otherwise complete run.
PRS_AUTHORED="$(_json_array "$PRS_AUTHORED")"
PRS_REVIEWED="$(_json_array "$PRS_REVIEWED")"
ISSUES="$(_json_array "$ISSUES")"

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
	--argjson checkin_eod "$CHECKIN_EOD" \
	--argjson checkin_bod "$CHECKIN_BOD" \
	--argjson checkin_latest "$CHECKIN_LATEST" \
	'{
    meta: {
      username:     $username,
      display_name: $display_name,
      today:        $today,
      yesterday:    $yesterday,
      tomorrow:     $tomorrow,
      since:        $since
    },
    checkin_targets: {
      eod:    $checkin_eod,
      bod:    $checkin_bod,
      latest: $checkin_latest
    },
    prs_authored:        $prs_authored,
    prs_reviewed:        $prs_reviewed,
    issues:              $issues,
    discussions_opened:  $discussions_opened,
    discussion_comments: $discussion_comments
  }'
