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

threads_file="$(mktemp)"
trap 'rm -f "$threads_file" "${threads_file}.tmp"' EXIT
echo '[]' > "$threads_file"

cursor=""
while :; do
  if [[ -n "$cursor" ]]; then
    page="$(gh api graphql -f query="$query" -f owner="$owner" -f name="$name" -F number="$pr" -f cursor="$cursor")"
  else
    page="$(gh api graphql -f query="$query" -f owner="$owner" -f name="$name" -F number="$pr")"
  fi

  nodes="$(echo "$page" | jq '.data.repository.pullRequest.reviewThreads.nodes // []')"
  jq -n --argjson a "$(cat "$threads_file")" --argjson b "$nodes" '$a + $b' > "${threads_file}.tmp"
  mv "${threads_file}.tmp" "$threads_file"

  has_next="$(echo "$page" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage')"
  [[ "$has_next" == "true" ]] || break
  cursor="$(echo "$page" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.endCursor')"
done

if [[ "$include_resolved" == false ]]; then
  jq '[.[] | select(.isResolved == false)]' "$threads_file" > "${threads_file}.tmp"
  mv "${threads_file}.tmp" "$threads_file"
fi

# Normalize GraphQL's Actor __typename into the same author_type field the REST
# paths below emit, so categorization reads one field regardless of source.
jq '[.[] | .comments.nodes |= map(.author_type = (.author.__typename // "Unknown"))]' \
  "$threads_file" > "${threads_file}.tmp"
mv "${threads_file}.tmp" "$threads_file"

# `--slurp` wraps the pages in one outer array; the `.[][]` flattens it back.
# Without it, `--paginate` emits each page as a separate top-level JSON array,
# which concatenate into something --argjson rejects outright ("invalid JSON
# text") the moment a PR passes 100 comments or 100 reviews — the long-running
# PRs this script exists for. `--slurp` is refused alongside `--jq`, so the
# filter moves into a pipe.
discussion="$(gh api "repos/${repo}/issues/${pr}/comments" --paginate --slurp \
  | jq '[.[][] | {id, author: .user.login, author_type: .user.type, body, created_at, html_url}]')"

reviews="$(gh api "repos/${repo}/pulls/${pr}/reviews" --paginate --slurp \
  | jq '[.[][] | {id, html_url, author: .user.login, author_type: .user.type, state, body, submitted_at}]')"

# GitHub shows a pending review only to its author, so this finds the
# viewer's own. The comment bodies are a snapshot for reading; the user can
# edit them in the browser at any time, so re-fetch one before editing it.
# shellcheck disable=SC2016  # GraphQL variables, not shell
pending_query='
query($owner: String!, $name: String!, $number: Int!) {
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

pending_review="$(gh api graphql -f query="$pending_query" -f owner="$owner" -f name="$name" -F number="$pr" \
  | jq '[.data.repository.pullRequest.reviews.nodes[] | select(.viewerDidAuthor)] | .[0]
        | if . == null then null else {id, comment_count: .comments.totalCount, comments_truncated: .comments.pageInfo.hasNextPage, comments: .comments.nodes} end')"

jq -n \
  --arg repo "$repo" \
  --argjson pr "$pr" \
  --argjson review_threads "$(cat "$threads_file")" \
  --argjson discussion_comments "$discussion" \
  --argjson reviews "$reviews" \
  --argjson pending_review "$pending_review" \
  '{repo: $repo, pr: $pr, review_threads: $review_threads, discussion_comments: $discussion_comments, reviews: $reviews, pending_review: $pending_review}' \
  > "$output"

# Only the thread count has any resolution state behind it, and reporting it
# alone is how an unread discussion comment or a finding buried in a review
# body gets called "addressed". Count each channel separately and say outright
# that two of them have no resolved state.
unresolved_count="$(jq '[.[] | select(.isResolved == false)] | length' "$threads_file")"
discussion_count="$(jq 'length' <<<"$discussion")"
review_count="$(jq 'length' <<<"$reviews")"
# A review with an empty body carried only thread comments. Counting those as
# feedback to read overstates the pile and buries the ones that aren't empty.
reviews_with_body="$(jq '[.[] | select((.body // "") != "")] | length' <<<"$reviews")"

echo "${repo}#${pr}: ${unresolved_count} unresolved review thread(s), ${discussion_count} discussion comment(s), ${reviews_with_body} review(s) with a body (of ${review_count})" >&2
echo "Discussion comments and review bodies have no resolved state - read each one; the thread count alone does not mean a PR is addressed." >&2

# Named because a colleague's architectural judgment usually arrives here
# rather than pinned to a line, and it is the easiest feedback on the PR to
# scroll past. author_type is GitHub's own actor type, so a service account on
# a user token lands in this list too; over-naming is the safe direction.
human_commenters="$(jq -r '[.[] | select(.author_type == "User") | .author] | unique | join(", ")' <<<"$discussion")"
if [[ -n "$human_commenters" ]]; then
  echo "Discussion comment(s) from: ${human_commenters}" >&2
fi

if [[ "$pending_review" != "null" ]]; then
  echo "You have a pending review with $(echo "$pending_review" | jq '.comment_count') comment(s) on ${repo}#${pr}. It is unsubmitted; submitting it is your call." >&2
  if [[ "$(echo "$pending_review" | jq '.comments_truncated')" == "true" ]]; then
    echo "Only the first 100 pending comments were fetched; page the rest with comments(after:) before editing any of them." >&2
  fi
fi
