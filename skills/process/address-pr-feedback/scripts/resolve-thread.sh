#!/usr/bin/env bash
# Reply to (optional) and resolve a single PR review thread by its GraphQL
# node ID (the `id` field from fetch-feedback.sh's review_threads output).
set -euo pipefail

usage() {
  echo "Usage: $0 --thread-id <PRRT_...> [--comment <reply body> | --comment-file <file>] [--dry-run]" >&2
  echo "       --comment-file - reads the reply from stdin" >&2
  echo "       Prefer --comment-file: a reply passed as a shell argument has its" >&2
  echo "       backticks and \$ expanded before this script sees it." >&2
  exit 1
}

thread_id=""
comment=""
comment_file=""
dry_run=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --thread-id) thread_id="${2:?}"; shift 2 ;;
    --comment) comment="${2:?}"; shift 2 ;;
    --comment-file) comment_file="${2:?}"; shift 2 ;;
    --dry-run) dry_run=true; shift ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done
[[ -z "$thread_id" ]] && usage

if [[ -n "$comment_file" ]]; then
  if [[ -n "$comment" ]]; then
    echo "Error: pass --comment or --comment-file, not both" >&2
    exit 1
  fi
  if [[ "$comment_file" == "-" ]]; then
    comment="$(cat)"
  elif [[ -r "$comment_file" ]]; then
    comment="$(cat "$comment_file")"
  else
    echo "Error: cannot read comment file: ${comment_file}" >&2
    exit 1
  fi
  if [[ -z "$comment" ]]; then
    echo "Error: comment file is empty: ${comment_file}" >&2
    exit 1
  fi
fi

if [[ "$dry_run" == true ]]; then
  echo "DRY-RUN: would resolve ${thread_id}$([[ -n "$comment" ]] && echo " after replying: ${comment}")"
  exit 0
fi

if [[ -n "$comment" ]]; then
  # shellcheck disable=SC2016  # single-quoted on purpose: $threadId/$body are GraphQL variables, not shell
  gh api graphql -f query='
    mutation($threadId: ID!, $body: String!) {
      addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) {
        comment { id url }
      }
    }' -f threadId="$thread_id" -f body="$comment" > /dev/null
fi

# shellcheck disable=SC2016  # single-quoted on purpose: $threadId is a GraphQL variable, not shell
gh api graphql -f query='
  mutation($threadId: ID!) {
    resolveReviewThread(input: {threadId: $threadId}) {
      thread { id isResolved }
    }
  }' -f threadId="$thread_id" --jq '.data.resolveReviewThread.thread'
