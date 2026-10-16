---
name: verify-before-claiming
description: >
  Use this skill before stating the status of anything outside the conversation:
  a PR, CI run, review, witan task, branch, deploy, environment, or fix. Triggers
  on "what's the status of", "is it merged", "did CI pass", "did the review
  land", "is it deployed", "is it fixed", "is X done", "where are we on", and on
  any summary or handoff that reports status. Re-fetch live state instead of
  repeating memory or earlier output, check uncommitted work and other worktrees
  before calling work missing, and label each finding verified or inferred.
license: BSD-3-Clause
metadata:
  category: workflow
---

# Verify before claiming

State changes while you work. What you saw earlier in this conversation, or what
a summary or memory says, is not current state. These rules apply to every
status claim, not only to code review.

1. Re-fetch status in the same turn you state it, and declare an environment or
   fix done only after running the check that shows it works.
   - PR: `gh pr view <n> --json state,reviewDecision`
   - CI: `gh pr checks <n>`
   - witan task: `task_get(slug=...)`
   - Branch: `git fetch && git log origin/<branch> -1`
2. Never report an external event (a review landed, a check finished, someone
   replied, a deploy completed) without the API call that shows it.
3. Before concluding work is missing or not started, run `git status` and
   `git worktree list`. Uncommitted changes and other worktrees count.
4. Label each finding you report "verified (`<command>`)" or "inferred".
   Anything you did not check is inferred, and you say so.
