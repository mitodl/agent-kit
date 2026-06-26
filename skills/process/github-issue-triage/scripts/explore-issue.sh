#!/usr/bin/env bash
# Cross-reference a single GitHub issue against a local git repository.
#
# Runs a battery of searches to surface evidence that an issue is complete,
# outdated, or still open. Prints structured output suitable for copy-paste
# into an agent brief or for direct reading.
#
# Usage:
#   ./explore-issue.sh <issue-number> <repo-path> <issues-json>
#
# Arguments:
#   issue-number   GitHub issue number (e.g. 1749)
#   repo-path      Absolute path to the local git checkout
#   issues-json    Path to the JSON file produced by fetch-issues.sh
#
# Requires: jq, git, rg (ripgrep)

set -euo pipefail

ISSUE_NUM="${1:?Usage: $0 <issue-number> <repo-path> <issues-json>}"
REPO_PATH="${2:?Usage: $0 <issue-number> <repo-path> <issues-json>}"
ISSUES_JSON="${3:?Usage: $0 <issue-number> <repo-path> <issues-json>}"

# Pull title + creation date from the JSON
TITLE=$(jq -r --argjson n "${ISSUE_NUM}" \
  '.[] | select(.number == $n) | .title' "${ISSUES_JSON}")
CREATED=$(jq -r --argjson n "${ISSUE_NUM}" \
  '.[] | select(.number == $n) | .createdAt' "${ISSUES_JSON}")
BODY=$(jq -r --argjson n "${ISSUE_NUM}" \
  '.[] | select(.number == $n) | .body' "${ISSUES_JSON}" | head -c 600)

echo "===== Issue #${ISSUE_NUM}: ${TITLE} (opened ${CREATED}) ====="
echo
echo "--- Body (excerpt) ---"
echo "${BODY}"
echo

# Derive search keywords from the title (lower-case words >=5 chars)
KEYWORDS=$(echo "${TITLE}" | tr '[:upper:]' '[:lower:]' \
  | grep -oE '[a-z]{5,}' | sort -u | head -6 | tr '\n' '|' | sed 's/|$//')

echo "--- Keywords extracted: ${KEYWORDS} ---"
echo

echo "--- Recent commits mentioning these keywords ---"
git -C "${REPO_PATH}" log --oneline --since="${CREATED}" \
  --grep="${KEYWORDS}" --regexp-ignore-case 2>/dev/null | head -15 || true
echo

echo "--- Code references (rg, src/ only) ---"
# Run one rg per keyword; suppress errors for no-match
for kw in $(echo "${KEYWORDS}" | tr '|' '\n'); do
  echo "  [${kw}]:"
  rg -r --include="*.py" --include="*.yaml" --include="*.yml" \
    -l "${kw}" "${REPO_PATH}/src" 2>/dev/null | head -5 || true
done
echo

echo "--- All branches mentioning keywords ---"
git -C "${REPO_PATH}" branch -r 2>/dev/null \
  | grep -iE "${KEYWORDS}" | head -10 || echo "  (none)"
echo

echo "--- Closed PRs referencing this issue ---"
gh pr list --repo "$(git -C "${REPO_PATH}" remote get-url origin \
  | sed 's|.*github.com[:/]\(.*\)\.git|\1|;s|.*github.com[:/]\(.*\)|\1|')" \
  --state closed --search "#${ISSUE_NUM}" --limit 5 \
  --json number,title,mergedAt 2>/dev/null \
  | jq -r '.[] | "#\(.number) \(.title) [merged: \(.mergedAt // "n/a")]"' \
  || echo "  (gh query failed — check auth)"
echo
