#!/usr/bin/env bash
# Bucket enriched PR detail records (see enrich-prs.sh) by required action.
#
# Buckets (mutually exclusive):
#   draft                    - not ready for review yet
#   needs_first_pass_review  - no reviews, no reviewer requested -> kick off a first pass
#   awaiting_review          - a reviewer (human or bot) is requested, nothing given yet
#   has_review_comments      - a bot/human left COMMENTED-state feedback, no formal decision
#   changes_requested        - a formal CHANGES_REQUESTED review
#   approved_ready_to_merge  - APPROVED, mergeable, checks green
#   approved_blocked         - APPROVED but conflicting or checks red
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <enriched.json>" >&2
  exit 1
fi

jq '
  # Report order the SKILL doc promises ("most actionable first") - group_by
  # sorts its groups lexically by key, which does not match this, so the
  # final sort re-orders by this explicit priority instead.
  def bucket_order: [
    "approved_ready_to_merge", "approved_blocked", "changes_requested",
    "has_review_comments", "awaiting_review", "needs_first_pass_review", "draft"
  ];
  def has_review_state($s): (.latestReviews // []) | any(.state == $s);
  def checks_failing:
    (.statusCheckRollup // []) | any(
      ((.conclusion // "") | ascii_upcase | IN("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"))
      or ((.state // "") | ascii_upcase | IN("FAILURE", "ERROR"))
    );
  def blocked_reason:
    if .mergeable == "CONFLICTING" then "merge_conflict"
    elif checks_failing then "checks_failing"
    elif .mergeable == "UNKNOWN" then "unknown_pending_recompute"
    else "other"
    end;
  # Cheap heuristic, not proof: if the newest PR comment postdates the newest
  # review, someone likely already responded to the feedback. Verify by
  # reading the actual thread before trusting this for any bucket decision.
  def latest_review_at: ([(.latestReviews // [])[].submittedAt // empty] | max) // null;
  def latest_comment_at: ([(.comments // [])[].createdAt // empty] | max) // null;
  def feedback_likely_addressed:
    (latest_review_at != null) and (latest_comment_at != null) and (latest_comment_at > latest_review_at);

  map(
    . + {
      bucket: (
        if .isDraft then "draft"
        elif .reviewDecision == "APPROVED" then
          (if .mergeable == "MERGEABLE" and (checks_failing | not)
           then "approved_ready_to_merge"
           else "approved_blocked"
           end)
        elif .reviewDecision == "CHANGES_REQUESTED" then "changes_requested"
        elif has_review_state("COMMENTED") then "has_review_comments"
        elif (.reviewRequests // []) != [] then "awaiting_review"
        else "needs_first_pass_review"
        end
      )
    }
  )
  | group_by(.bucket)
  | map({
      bucket: .[0].bucket,
      count: length,
      prs: (map({
        number, repo, title, url, updatedAt, mergeable, mergeStateStatus,
        checks_failing: checks_failing,
        blocked_reason: (if .bucket == "approved_blocked" then blocked_reason else null end),
        feedback_likely_addressed: (
          if (.bucket == "has_review_comments" or .bucket == "changes_requested")
          then feedback_likely_addressed else null end
        )
      }) | sort_by(.updatedAt))
    })
  | sort_by(. as $g | bucket_order | index($g.bucket))
' "$1"
