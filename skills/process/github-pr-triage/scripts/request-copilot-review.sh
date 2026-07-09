#!/usr/bin/env bash
# Request a GitHub Copilot code review on a PR.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <owner/repo> <number>" >&2
  exit 1
fi

repo="$1"
number="$2"

gh api "repos/${repo}/pulls/${number}/requested_reviewers" \
  --method POST \
  -f "reviewers[]=copilot-pull-request-reviewer[bot]"
