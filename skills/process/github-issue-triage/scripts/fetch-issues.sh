#!/usr/bin/env bash
# Fetch all open issues from a GitHub repo and write them to OUTFILE.
#
# Usage:
#   ./fetch-issues.sh <owner/repo> [outfile]
#
# Defaults:
#   outfile  ./issues_full.json
#
# Environment:
#   ISSUE_FETCH_LIMIT  Maximum number of issues to fetch (default: 500).
#                      Set to a higher value for large repos.
#
# Output: JSON array of issues, each with:
#   number, title, body, labels (name list), createdAt, updatedAt
#
# Requires: gh (GitHub CLI), jq

set -euo pipefail

REPO="${1:?Usage: $0 <owner/repo> [outfile]}"
OUTFILE="${2:-./issues_full.json}"
LIMIT="${ISSUE_FETCH_LIMIT:-500}"

echo "Fetching open issues from ${REPO} (limit: ${LIMIT})..." >&2

gh issue list \
  --repo "${REPO}" \
  --state open \
  --limit "${LIMIT}" \
  --json number,title,body,labels,createdAt,updatedAt \
| jq '[.[] | {
    number,
    title,
    body,
    labels: [.labels[].name],
    createdAt: .createdAt[:10],
    updatedAt: .updatedAt[:10]
  }]' \
> "${OUTFILE}"

COUNT=$(jq length "${OUTFILE}")
echo "Saved ${COUNT} issues to ${OUTFILE}" >&2
