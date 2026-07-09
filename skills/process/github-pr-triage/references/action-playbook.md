# Action Playbook

Command-level detail for Phase 5 of the main skill. Every action here is
visible to others (comments, review requests) or hard to reverse (merges) —
get explicit confirmation on the specific PR(s) before running any of these,
even if the user approved "act on the ready ones" in general terms up front.
Show the list, then act.

---

## `needs_first_pass_review` — kick off a first pass

Two independent options; either or both, per the user's preference:

**Request GitHub Copilot as a reviewer:**

```bash
./skills/process/github-pr-triage/scripts/request-copilot-review.sh mitodl/agent-kit 90
```

This calls `POST /repos/{owner}/{repo}/pulls/{number}/requested_reviewers`
with `reviewers[]=copilot-pull-request-reviewer[bot]`. Requires the repo to
have Copilot code review enabled (org/repo setting) — a 422 usually means it
isn't. Copilot then posts its review as a normal `COMMENTED`-state review
within a minute or two; it does not block on this call.

**Have Claude do the first pass directly:** invoke this session's built-in
`/review <pr-url>` (not `/code-review`, which reviews your *working* diff).
This posts Claude's own review as a PR comment/review, giving the same kind of
first-pass coverage as Copilot without depending on org Copilot settings. Good
default when Copilot isn't enabled on a repo, or when the user explicitly asked
for "a Claude review" (they said "usually copilot, but maybe claude" — ask
which they want if unclear, or just do both).

Do **not** request a human reviewer on the user's behalf unless they name one —
that's a social action with more weight than a bot request.

---

## `approved_ready_to_merge` — merge

```bash
./skills/process/github-pr-triage/scripts/merge-pr.sh mitodl/ol-infrastructure 4902 squash
```

Merge method defaults to `squash` if omitted. Before calling it:

1. Confirm with the user which specific PR(s) — paste the list, don't assume
   "all of them" even if they said "merge the ready ones". A short recap
   ("these 3: #4902, #4924, #92 — go ahead?") is enough; don't require a
   PR-by-PR round trip for a small, already-shown batch.
2. Check whether the repo has a house merge-method convention (squash is by
   far the most common default across mitodl repos, but check
   `gh repo view <repo> --json squashMergeAllowed,mergeCommitAllowed,rebaseMergeAllowed`
   if unsure, or just ask).
3. Re-verify `mergeable`/checks are still green right before merging if any
   time has passed since classification — CI can go red between the report and
   the act phase.

`merge-pr.sh` always passes `--delete-branch`. If the user wants the branch
kept (e.g. it's used elsewhere), merge manually with `gh pr merge` instead of
the script.

---

## `changes_requested` / `has_review_comments` (not `feedback_likely_addressed`)

This is the bucket that needs real judgment, not just a script call:

1. Pull the full thread for context, not just the summary already in the
   classified JSON:
   ```bash
   gh pr view <number> -R <repo> --json latestReviews,comments,reviews \
     --jq '.latestReviews[] | {author: .author.login, state, body}'
   ```
2. Read what's actually being asked. Bot reviews (Copilot, Gemini, etc.) often
   bundle several findings in one comment body — treat each as a separate
   item, not a single ask.
3. If a code fix is needed, work in the actual local checkout for that repo
   (not the tracker/triage context) — check out the PR's branch
   (`gh pr checkout <number> -R <repo>`), fix, run the repo's own tests/lint,
   commit, push.
4. After pushing, comment summarizing what changed (mirrors the pattern
   already used on PR threads in this org — see e.g. how prior "Addressed in
   `<sha>`: ..." comments are written) and consider re-requesting review from
   whoever left the original feedback.

**Multiple PRs across different repos need fixes in parallel** — this is
exactly the shape the `Agent` tool suits: one subagent per repo/PR pair, each
briefed with that PR's specific review comments, its repo's local checkout
path, and the instruction to fix, test, and push (not to merge or close
anything). Don't fan out subagents for a single PR or a single repo — that's
just sequential work wearing a parallel costume.

**Don't auto-trust `feedback_likely_addressed: true`** as a reason to skip a
PR entirely in the report — still list it, just flag it as lower priority to
re-read. It's a timestamp heuristic (newest comment postdates newest review),
not a content match; it can't tell whether the response actually resolved the
concern, and it's blind to fixes pushed without an accompanying comment.

---

## `awaiting_review` — usually nothing to do

By default, report only. If the user wants to nudge stale ones, ask for (or
infer) a staleness threshold, then post a short, non-pushy comment — don't
re-request review (that can reset review state / notify people again
unnecessarily) unless they specifically ask for that.

---

## `approved_blocked` — triage by `blocked_reason`

| `blocked_reason` | What to do |
|---|---|
| `unknown_pending_recompute` | Re-run `scripts/pr-detail.sh <repo> <number>` after a few seconds; GitHub just hasn't finished computing mergeability. Often resolves to `approved_ready_to_merge` on the next check with no other action needed. |
| `merge_conflict` | Needs a rebase/merge from base — this is code work, handle like the feedback bucket above (local checkout, resolve, push). Flag to the user rather than silently resolving conflicts in a way they haven't seen. |
| `checks_failing` | Look at the specific failing check via `statusCheckRollup[].detailsUrl` before guessing at a fix — CI failures are too varied to have a generic playbook step. |

---

## `draft` — report only

Never request reviews or merge a draft. If a draft looks stale and the user
wants it flagged, that's a report annotation, not an action.

---

## Common patterns

**Bot-authored PRs (Renovate, Dependabot) don't usually appear here.**
`fetch-prs.sh` defaults to `--author @me`; a bot's own PRs only show up if you
explicitly pass `--author renovate[bot]` or similar. If the user wants those
triaged, that's closer to the `dependency-updates` skill's job than this one —
point them there instead of reinventing it.

**Very large orgs / high PR counts.** `gh search prs` is capped at 200 in
`fetch-prs.sh` and GitHub's search API caps at 1000 results per query
regardless. If the count returned equals the limit, say so — the report may be
truncated, not exhaustive.

**A PR the user doesn't own but is listed as a reviewer on** is a different
query entirely (`--review-requested @me` instead of `--author @me`) — this
skill doesn't cover that mode; say so if asked rather than quietly mixing the
two result sets.
