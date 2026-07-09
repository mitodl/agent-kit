#!/usr/bin/env bash
# Merge an approved, green PR. This performs the merge immediately and
# unconditionally once invoked - get explicit user confirmation per PR
# *before* calling this script, never as a batch default.
set -euo pipefail

usage() {
  echo "Usage: $0 <owner/repo> <number> [merge|squash|rebase]" >&2
  echo "  Merge method defaults to squash." >&2
  exit 1
}

if [[ $# -lt 2 ]]; then
  usage
fi

repo="$1"
number="$2"
method="${3:-squash}"

case "$method" in
  merge|squash|rebase) ;;
  *) usage ;;
esac

gh pr merge "$number" -R "$repo" "--${method}" --delete-branch
