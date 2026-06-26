#!/usr/bin/env bash
# Cross-reference a single GitHub issue against a local git repository.
#
# Runs a battery of searches to surface evidence that an issue is complete,
# outdated, or still open. Prints structured output suitable for copy-paste
# into an agent brief or for direct reading.
#
# Usage (direct repo):
#   ./explore-issue.sh <issue-number> <repo-path> <issues-json>
#
# Usage (tracker repo — resolve codebase from product label):
#   ./explore-issue.sh <issue-number> <fallback-path> <issues-json> <label-map-json>
#
# Arguments:
#   issue-number    GitHub issue number (e.g. 1749)
#   repo-path       Local git checkout to search, OR fallback path if label
#                   map is provided and a match is found
#   issues-json     Path to the JSON file produced by fetch-issues.sh
#   label-map-json  Optional: JSON object mapping label names to local repo
#                   paths, e.g. {"product:mit-learn": "/home/user/mit-learn"}.
#                   When provided, the first matching label overrides repo-path.
#
# Requires: jq, git, rg (ripgrep)

set -euo pipefail

ISSUE_NUM="${1:?Usage: $0 <issue-number> <repo-path> <issues-json> [label-map-json]}"
FALLBACK_PATH="${2:?Usage: $0 <issue-number> <repo-path> <issues-json> [label-map-json]}"
ISSUES_JSON="${3:?Usage: $0 <issue-number> <repo-path> <issues-json> [label-map-json]}"
LABEL_MAP="${4:-}"

# Pull title, creation date, and labels from the JSON
TITLE=$(jq -r --argjson n "${ISSUE_NUM}" \
  '.[] | select(.number == $n) | .title' "${ISSUES_JSON}")
CREATED=$(jq -r --argjson n "${ISSUE_NUM}" \
  '.[] | select(.number == $n) | .createdAt' "${ISSUES_JSON}")
BODY=$(jq -r --argjson n "${ISSUE_NUM}" \
  '.[] | select(.number == $n) | .body' "${ISSUES_JSON}" | head -c 600)
LABELS=$(jq -r --argjson n "${ISSUE_NUM}" \
  '[.[] | select(.number == $n) | .labels[]] | join(", ")' "${ISSUES_JSON}")

# Resolve the target repo path from the label map, if provided
REPO_PATH="${FALLBACK_PATH}"
if [[ -n "${LABEL_MAP}" && -f "${LABEL_MAP}" ]]; then
  RESOLVED=$(jq -r --argjson n "${ISSUE_NUM}" \
    --slurpfile lm "${LABEL_MAP}" '
      [ .[] | select(.number == $n) | .labels[] ] as $lbls |
      $lm[0] | to_entries |
      map(select(.key as $k | $lbls | contains([$k]))) |
      first.value // ""
    ' "${ISSUES_JSON}")
  if [[ -n "${RESOLVED}" && -d "${RESOLVED}" ]]; then
    REPO_PATH="${RESOLVED}"
    echo "INFO: label map resolved repo → ${REPO_PATH}" >&2
  elif [[ -n "${RESOLVED}" ]]; then
    echo "WARN: label map resolved '${RESOLVED}' but directory not found; " \
         "falling back to ${FALLBACK_PATH}" >&2
  else
    echo "WARN: no label in [${LABELS}] matched the label map; " \
         "falling back to ${FALLBACK_PATH}" >&2
  fi
fi

echo "===== Issue #${ISSUE_NUM}: ${TITLE} (opened ${CREATED}) ====="
echo "Labels: ${LABELS:-none}"
echo "Searching: ${REPO_PATH}"
echo

echo "--- Body (excerpt) ---"
echo "${BODY}"
echo

# Derive search keywords from the title (lower-case words >= 5 chars)
KEYWORDS=$(echo "${TITLE}" | tr '[:upper:]' '[:lower:]' \
  | grep -oE '[a-z]{5,}' | sort -u | head -6 | tr '\n' '|' | sed 's/|$//')

echo "--- Keywords extracted: ${KEYWORDS} ---"
echo

echo "--- Recent commits mentioning these keywords ---"
git -C "${REPO_PATH}" log --oneline --since="${CREATED}" \
  --grep="${KEYWORDS}" --regexp-ignore-case 2>/dev/null | head -15 || true
echo

echo "--- Code references (rg, src/ only) ---"
SRC_DIR="${REPO_PATH}/src"
[[ -d "${SRC_DIR}" ]] || SRC_DIR="${REPO_PATH}"
for kw in $(echo "${KEYWORDS}" | tr '|' '\n'); do
  echo "  [${kw}]:"
  rg -r --include="*.py" --include="*.yaml" --include="*.yml" \
    -l "${kw}" "${SRC_DIR}" 2>/dev/null | head -5 || true
done
echo

echo "--- Remote branches mentioning keywords ---"
git -C "${REPO_PATH}" branch -r 2>/dev/null \
  | grep -iE "${KEYWORDS}" | head -10 || echo "  (none)"
echo

echo "--- Closed PRs referencing this issue ---"
REMOTE_URL=$(git -C "${REPO_PATH}" remote get-url origin 2>/dev/null || echo "")
if [[ -n "${REMOTE_URL}" ]]; then
  SLUG=$(echo "${REMOTE_URL}" \
    | sed 's|.*github.com[:/]\(.*\)\.git|\1|;s|.*github.com[:/]\(.*\)|\1|')
  gh pr list --repo "${SLUG}" \
    --state closed --search "#${ISSUE_NUM}" --limit 5 \
    --json number,title,mergedAt 2>/dev/null \
    | jq -r '.[] | "#\(.number) \(.title) [merged: \(.mergedAt // "n/a")]"' \
    || echo "  (gh query failed — check auth)"
else
  echo "  (could not determine remote URL)"
fi
echo
