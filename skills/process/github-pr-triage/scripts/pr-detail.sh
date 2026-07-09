#!/usr/bin/env bash
# Fetch full review/check/mergeability detail for a single PR.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <owner/repo> <number>" >&2
  exit 1
fi

repo="$1"
number="$2"

gh pr view "$number" -R "$repo" --json \
number,title,url,isDraft,author,createdAt,updatedAt,reviewDecision,latestReviews,reviewRequests,statusCheckRollup,mergeable,mergeStateStatus,comments
