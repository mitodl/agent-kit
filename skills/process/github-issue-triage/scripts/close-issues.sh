#!/usr/bin/env bash
# Close or comment on a list of GitHub issues from a triage report.
#
# Reads issue numbers from STDIN (one per line) and either:
#   --dry-run   prints the actions that would be taken (default)
#   --close     closes each issue with a standard triage comment
#   --comment   posts a comment without closing
#
# Usage:
#   echo "1749\n822\n407" | ./close-issues.sh --dry-run <owner/repo>
#   echo "1749\n822\n407" | ./close-issues.sh --close  <owner/repo>
#
# The closing comment explains why the issue is being closed so that
# future readers have context.  Edit CLOSE_REASON below to customise.
#
# Requires: gh (GitHub CLI)

set -euo pipefail

MODE="${1:?Usage: $0 --dry-run|--close|--comment <owner/repo>}"
REPO="${2:?Usage: $0 --dry-run|--close|--comment <owner/repo>}"

CLOSE_REASON="${ISSUE_TRIAGE_REASON:-"Closed during automated issue triage: the work described in this issue has been completed, the approach has been superseded, or a newer issue now tracks this scope. See the triage report for details."}"

while IFS= read -r ISSUE_NUM; do
  [[ -z "${ISSUE_NUM}" ]] && continue
  [[ "${ISSUE_NUM}" =~ ^[0-9]+$ ]] || { echo "SKIP: '${ISSUE_NUM}' is not a number" >&2; continue; }

  case "${MODE}" in
    --dry-run)
      echo "DRY-RUN: would close #${ISSUE_NUM} in ${REPO}"
      ;;
    --close)
      echo "Closing #${ISSUE_NUM}..."
      gh issue comment "${ISSUE_NUM}" --repo "${REPO}" --body "${CLOSE_REASON}"
      gh issue close "${ISSUE_NUM}" --repo "${REPO}" --reason "completed"
      echo "  Closed #${ISSUE_NUM}"
      ;;
    --comment)
      echo "Commenting on #${ISSUE_NUM}..."
      gh issue comment "${ISSUE_NUM}" --repo "${REPO}" --body "${CLOSE_REASON}"
      echo "  Commented on #${ISSUE_NUM}"
      ;;
    *)
      echo "Unknown mode: ${MODE}" >&2
      exit 1
      ;;
  esac
done
