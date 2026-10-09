#!/usr/bin/env bash
# Close or comment on a list of GitHub issues from a triage report.
#
# Reads issue numbers from STDIN (one per line) and either:
#   --dry-run   prints the actions that would be taken
#   --close     closes each issue with a standard triage comment
#   --comment   posts a comment without closing
#
# Usage:
#   printf "1749\n822\n407\n" | ./close-issues.sh --dry-run <owner/repo>
#   printf "1749\n822\n407\n" | ./close-issues.sh --close  <owner/repo>
#
# The closing comment explains why the issue is being closed so that
# future readers have context.  Override it with ISSUE_TRIAGE_REASON (a
# one-liner) or ISSUE_TRIAGE_REASON_FILE (a path).  Prefer the file for
# anything containing backticks, $, or quotes: a reason set inline on the
# command line is expanded by the caller's shell first, which silently drops
# backticked identifiers from the comment that gets posted.  There is no
# stdin option for the reason -- stdin is the issue-number list.
#
# Requires: gh (GitHub CLI)

set -euo pipefail

MODE="${1:?Usage: $0 --dry-run|--close|--comment <owner/repo>}"
REPO="${2:?Usage: $0 --dry-run|--close|--comment <owner/repo>}"

DEFAULT_REASON="Closed during automated issue triage: the work described in this issue has been completed, the approach has been superseded, or a newer issue now tracks this scope. See the triage report for details."

if [[ -n "${ISSUE_TRIAGE_REASON_FILE:-}" ]]; then
  if [[ -n "${ISSUE_TRIAGE_REASON:-}" ]]; then
    echo "Error: set ISSUE_TRIAGE_REASON or ISSUE_TRIAGE_REASON_FILE, not both" >&2
    exit 1
  fi
  if [[ -r "${ISSUE_TRIAGE_REASON_FILE}" ]]; then
    CLOSE_REASON="$(cat "${ISSUE_TRIAGE_REASON_FILE}")"
  else
    echo "Error: cannot read ISSUE_TRIAGE_REASON_FILE: ${ISSUE_TRIAGE_REASON_FILE}" >&2
    exit 1
  fi
  [[ -z "${CLOSE_REASON}" ]] && { echo "Error: ISSUE_TRIAGE_REASON_FILE is empty" >&2; exit 1; }
else
  CLOSE_REASON="${ISSUE_TRIAGE_REASON:-${DEFAULT_REASON}}"
fi

while IFS= read -r line; do
  ISSUE_NUM=$(echo "${line}" | tr -d '\r' | xargs)
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
