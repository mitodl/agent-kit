#!/usr/bin/env bash
# Fetch all feedback on a PR in one shot: inline review threads (GraphQL,
# cursor-paginated), top-level discussion comments (REST, --paginate), and
# formal reviews (REST, --paginate). Excludes resolved threads by default.
# Also reports the viewer's own pending (unsubmitted) review, if there is one.
set -euo pipefail

usage() {
  echo "Usage: $0 <owner/repo> <pr-number> [output.json] [--include-resolved]" >&2
  echo "  output.json defaults to stdout" >&2
  exit 1
}

include_resolved=false
positional=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --include-resolved) include_resolved=true; shift ;;
    -h|--help) usage ;;
    *) positional+=("$1"); shift ;;
  esac
done

repo="${positional[0]:-}"
pr="${positional[1]:-}"
output="${positional[2]:-/dev/stdout}"
[[ -z "$repo" || -z "$pr" ]] && usage

owner="${repo%%/*}"
name="${repo##*/}"

# shellcheck disable=SC2016  # single-quoted on purpose: $owner etc. are GraphQL variables, not shell
query='
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 50) {
            nodes { databaseId body author { login __typename } createdAt }
          }
        }
      }
    }
  }
}'

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT
threads_file="${work_dir}/threads.json"
page_file="${work_dir}/page.json"
nodes_file="${work_dir}/nodes.json"
discussion_file="${work_dir}/discussion.json"
reviews_file="${work_dir}/reviews.json"
pending_file="${work_dir}/pending.json"
tmp_file="${work_dir}/tmp.json"
echo '[]' > "$threads_file"

# Every payload below moves between jq invocations as a file, never as a
# command-line argument. Linux caps a single argument at 128 KiB, so one
# `--argjson` carrying a long PR's feedback -- 100 review bodies averaging
# 1.5 KiB clears it -- aborts the fetch with "argument list too long".
# `--slurpfile` reads the same JSON with no such ceiling; it wraps the file's
# contents in an outer array, hence the `[0]` on each use.
cursor=""
while :; do
  if [[ -n "$cursor" ]]; then
    gh api graphql -f query="$query" -f owner="$owner" -f name="$name" -F number="$pr" -f cursor="$cursor" > "$page_file"
  else
    gh api graphql -f query="$query" -f owner="$owner" -f name="$name" -F number="$pr" > "$page_file"
  fi

  jq '.data.repository.pullRequest.reviewThreads.nodes // []' "$page_file" > "$nodes_file"
  jq -n --slurpfile a "$threads_file" --slurpfile b "$nodes_file" '$a[0] + $b[0]' > "$tmp_file"
  mv "$tmp_file" "$threads_file"

  has_next="$(jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage' "$page_file")"
  [[ "$has_next" == "true" ]] || break
  cursor="$(jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.endCursor' "$page_file")"
done

if [[ "$include_resolved" == false ]]; then
  jq '[.[] | select(.isResolved == false)]' "$threads_file" > "$tmp_file"
  mv "$tmp_file" "$threads_file"
fi

# Normalize GraphQL's Actor __typename into the same author_type field the REST
# paths below emit, so categorization reads one field regardless of source.
jq '[.[] | .comments.nodes |= map(.author_type = (.author.__typename // "Unknown"))]' \
  "$threads_file" > "$tmp_file"
mv "$tmp_file" "$threads_file"

# `--slurp` wraps the pages in one outer array; the `.[][]` flattens it back.
# Without it, `--paginate` emits each page as a separate top-level JSON array,
# which concatenate into something --argjson rejects outright ("invalid JSON
# text") the moment a PR passes 100 comments or 100 reviews — the long-running
# PRs this script exists for. `--slurp` is refused alongside `--jq`, so the
# filter moves into a pipe.
gh api "repos/${repo}/issues/${pr}/comments" --paginate --slurp \
  | jq '[.[][] | {id, author: .user.login, author_type: .user.type, body, created_at, html_url}]' \
  > "$discussion_file"

gh api "repos/${repo}/pulls/${pr}/reviews" --paginate --slurp \
  | jq '[.[][] | {id, html_url, author: .user.login, author_type: .user.type, state, body, submitted_at}]' \
  > "$reviews_file"

# GitHub shows a pending review only to its author, so this finds the
# viewer's own. The comment bodies are a snapshot for reading; the user can
# edit them in the browser at any time, so re-fetch one before editing it.
#
# `viewer { login }` rides along on this call rather than costing a second
# one. Without it there is no way to tell your own summary comment from a
# reviewer's: both are plain `User` entries on the same endpoint, so each
# pass would re-triage the summary the previous pass posted.
# shellcheck disable=SC2016  # GraphQL variables, not shell
pending_query='
query($owner: String!, $name: String!, $number: Int!) {
  viewer { login }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviews(states: [PENDING], first: 1) {
        nodes {
          id
          viewerDidAuthor
          comments(first: 100) {
            totalCount
            pageInfo { hasNextPage }
            nodes { id databaseId path line body }
          }
        }
      }
    }
  }
}'

gh api graphql -f query="$pending_query" -f owner="$owner" -f name="$name" -F number="$pr" > "$page_file"

viewer="$(jq -r '.data.viewer.login' "$page_file")"

jq '[.data.repository.pullRequest.reviews.nodes[] | select(.viewerDidAuthor)] | .[0]
        | if . == null then null else {id, comment_count: .comments.totalCount, comments_truncated: .comments.pageInfo.hasNextPage, comments: .comments.nodes} end' \
  "$page_file" > "$pending_file"

# Tag what is yours so the skill can skip it mechanically rather than by
# recognizing a login.
jq --arg me "$viewer" 'map(.is_viewer = (.author == $me))' "$discussion_file" > "$tmp_file"
mv "$tmp_file" "$discussion_file"
jq --arg me "$viewer" 'map(.is_viewer = (.author == $me))' "$reviews_file" > "$tmp_file"
mv "$tmp_file" "$reviews_file"

jq -n \
  --arg repo "$repo" \
  --argjson pr "$pr" \
  --arg viewer "$viewer" \
  --slurpfile review_threads "$threads_file" \
  --slurpfile discussion_comments "$discussion_file" \
  --slurpfile reviews "$reviews_file" \
  --slurpfile pending_review "$pending_file" \
  '{repo: $repo, pr: $pr, viewer: $viewer, review_threads: $review_threads[0], discussion_comments: $discussion_comments[0], reviews: $reviews[0], pending_review: $pending_review[0]}' \
  > "$output"

# Only the thread count has any resolution state behind it, and reporting it
# alone is how an unread discussion comment or a finding buried in a review
# body gets called "addressed". Count each channel separately and say outright
# that two of them have no resolved state.
unresolved_count="$(jq '[.[] | select(.isResolved == false)] | length' "$threads_file")"
review_count="$(jq 'length' "$reviews_file")"
# Both counts exclude your own, so they describe what is left to read rather
# than growing by one every time Phase 4 posts a summary comment. A review
# with an empty body carried only thread comments; counting those as feedback
# to read overstates the pile and buries the ones that aren't empty.
discussion_count="$(jq '[.[] | select(.is_viewer | not)] | length' "$discussion_file")"
reviews_with_body="$(jq '[.[] | select((.is_viewer | not) and (.body // "") != "")] | length' "$reviews_file")"

echo "${repo}#${pr}: ${unresolved_count} unresolved review thread(s), ${discussion_count} discussion comment(s), ${reviews_with_body} review(s) with a body -- yours excluded, ${review_count} review(s) in total" >&2
echo "Discussion comments and review bodies have no resolved state - read each one; the thread count alone does not mean a PR is addressed." >&2

# Named because a colleague's architectural judgment usually arrives here
# rather than pinned to a line, and it is the easiest feedback on the PR to
# scroll past. author_type is GitHub's own actor type, so a service account on
# a user token lands in this list too; over-naming is the safe direction.
human_commenters="$(jq -r '[.[] | select((.is_viewer | not) and .author_type == "User") | .author] | unique | join(", ")' "$discussion_file")"
if [[ -n "$human_commenters" ]]; then
  echo "Discussion comment(s) from: ${human_commenters}" >&2
fi

if [[ "$(jq -r 'type' "$pending_file")" != "null" ]]; then
  echo "You have a pending review with $(jq '.comment_count' "$pending_file") comment(s) on ${repo}#${pr}. It is unsubmitted; submitting it is your call." >&2
  if [[ "$(jq '.comments_truncated' "$pending_file")" == "true" ]]; then
    echo "Only the first 100 pending comments were fetched; page the rest with comments(after:) before editing any of them." >&2
  fi
fi
