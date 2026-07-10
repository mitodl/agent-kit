#!/usr/bin/env bash
# Post a top-level PR conversation comment -- for replying to a discussion-level
# comment (which has no "thread" to resolve) or posting a final consolidated
# summary of what was addressed. Not for inline review threads: use
# resolve-thread.sh for those, since a plain PR comment doesn't reply in-thread
# or affect resolution state.
set -euo pipefail

usage() { echo "Usage: $0 <owner/repo> <pr-number> <body>" >&2; exit 1; }
[[ $# -lt 3 ]] && usage

repo="$1"
pr="$2"
body="$3"

gh pr comment "$pr" --repo "$repo" --body "$body"
