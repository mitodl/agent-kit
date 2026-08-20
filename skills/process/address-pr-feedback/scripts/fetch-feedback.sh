#!/usr/bin/env bash
# Fetch all feedback on a PR in one shot: inline review threads (GraphQL,
# cursor-paginated), top-level discussion comments (REST, --paginate), and
# formal reviews (REST, --paginate). Excludes resolved threads by default.
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

discussion="$(gh api "repos/${repo}/issues/${pr}/comments" --paginate \
  --jq '[.[] | {id, author: .user.login, author_type: .user.type, body, created_at, html_url}]')"

reviews="$(gh api "repos/${repo}/pulls/${pr}/reviews" --paginate \
  --jq '[.[] | {author: .user.login, author_type: .user.type, state, body, submitted_at}]')"

jq -n \
  --arg repo "$repo" \
  --argjson pr "$pr" \
  --argjson review_threads "$(cat "$threads_file")" \
  --argjson discussion_comments "$discussion" \
  --argjson reviews "$reviews" \
  '{repo: $repo, pr: $pr, review_threads: $review_threads, discussion_comments: $discussion_comments, reviews: $reviews}' \
  > "$output"

unresolved_count="$(jq '[.[] | select(.isResolved == false)] | length' "$threads_file")"
echo "Fetched ${unresolved_count} unresolved review thread(s), $(echo "$discussion" | jq 'length') discussion comment(s), $(echo "$reviews" | jq 'length') review(s) for ${repo}#${pr}" >&2
