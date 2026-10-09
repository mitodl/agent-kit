#!/usr/bin/env bash
# Post a top-level PR conversation comment -- for replying to a discussion-level
# comment (which has no "thread" to resolve) or posting a final consolidated
# summary of what was addressed. Not for inline review threads: use
# resolve-thread.sh for those, since a plain PR comment doesn't reply in-thread
# or affect resolution state.
set -euo pipefail

usage() {
  echo "Usage: $0 <owner/repo> <pr-number> <body>" >&2
  echo "       $0 <owner/repo> <pr-number> --body-file <file>   (- reads stdin)" >&2
  echo "Prefer --body-file: a body passed as a shell argument has its backticks" >&2
  echo "and \$ expanded before this script sees it." >&2
  exit 1
}
[[ $# -lt 3 ]] && usage

repo="$1"
pr="$2"

if [[ "$3" == "--body-file" ]]; then
  file="${4:?--body-file needs a path (use - for stdin)}"
  if [[ "$file" == "-" ]]; then
    body="$(cat)"
  elif [[ -r "$file" ]]; then
    body="$(cat "$file")"
  else
    echo "Error: cannot read body file: ${file}" >&2
    exit 1
  fi
else
  body="$3"
fi

[[ -z "$body" ]] && { echo "Error: comment body is empty" >&2; exit 1; }

gh pr comment "$pr" --repo "$repo" --body "$body"
