#!/usr/bin/env bash
# Request a first-pass bot code review (Copilot and/or Claude) on a PR.
#
# Refuses to run on drafts, or on PRs that already have review activity
# (a submitted review or a pending requested reviewer), unless --force -
# this is meant to be safe to call on a whole "needs_first_pass_review"
# batch from classify-prs.sh without re-pinging PRs that have moved on.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat >&2 <<'USAGE'
Usage: request-review.sh <owner/repo> <number> [copilot|claude|all] [options]

  copilot|claude|all      Which bot(s) to request a review from. Default: all.

Options:
  --force                  Request even if the PR is a draft or already has
                           review activity (a review or a requested reviewer).
  --claude-trigger TEXT    Comment body used to invoke a repo's Claude GitHub
                           Action review workflow (e.g. anthropics/claude-code-action),
                           if one is configured to listen for PR comments.
                           Default: "@claude review this PR".
USAGE
  exit 1
}

[[ $# -lt 2 ]] && usage

repo="$1"; shift
number="$1"; shift

target="all"
force=false
claude_trigger="@claude review this PR"

while [[ $# -gt 0 ]]; do
  case "$1" in
    copilot|claude|all) target="$1"; shift ;;
    --force) force=true; shift ;;
    --claude-trigger) [[ $# -ge 2 ]] || usage; claude_trigger="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

status_json="$("${script_dir}/pr-detail.sh" "$repo" "$number")"
is_draft="$(jq -r '.isDraft' <<<"$status_json")"
review_count="$(jq '(.latestReviews // []) | length' <<<"$status_json")"
request_count="$(jq '(.reviewRequests // []) | length' <<<"$status_json")"

if [[ "$is_draft" == "true" && "$force" != "true" ]]; then
  echo "PR ${repo}#${number} is a draft - skipping (use --force to override)." >&2
  exit 1
fi

if [[ "$review_count" -gt 0 || "$request_count" -gt 0 ]] && [[ "$force" != "true" ]]; then
  echo "PR ${repo}#${number} already has review activity (reviews: ${review_count}, requested reviewers: ${request_count}) - skipping (use --force to override)." >&2
  exit 1
fi

request_copilot() {
  echo "Requesting Copilot review on ${repo}#${number}..." >&2
  if gh api "repos/${repo}/pulls/${number}/requested_reviewers" \
       --method POST -f "reviewers[]=copilot-pull-request-reviewer[bot]" > /dev/null 2>&1; then
    echo "  Copilot review requested." >&2
  else
    echo "  Failed - GitHub Copilot code review is likely not enabled on ${repo}." >&2
    return 1
  fi
}

request_claude() {
  echo "Posting a Claude review-trigger comment on ${repo}#${number}..." >&2
  if gh pr comment "$number" -R "$repo" --body "$claude_trigger" > /dev/null; then
    echo "  Comment posted: \"${claude_trigger}\"" >&2
    echo "  This only produces a review if ${repo} has a Claude GitHub Action" >&2
    echo "  (e.g. anthropics/claude-code-action) configured to react to PR comments -" >&2
    echo "  otherwise it's a harmless no-op. If no such workflow is installed, run" >&2
    echo "  '/review ${repo}#${number}' in a Claude Code session instead for an" >&2
    echo "  equivalent first-pass review." >&2
  else
    echo "  Failed to post comment." >&2
    return 1
  fi
}

status=0
case "$target" in
  copilot) request_copilot || status=1 ;;
  claude) request_claude || status=1 ;;
  all)
    request_copilot || status=1
    request_claude || status=1
    ;;
esac

exit "$status"
