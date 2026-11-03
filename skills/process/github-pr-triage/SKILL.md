---
name: github-pr-triage
description: >
  Report the status of your open GitHub pull requests across an org (or one
  repo) in a single command: which are approved and ready to merge, which have
  review feedback you haven't addressed (unresolved review threads awaiting
  your reply, changes requested, failing CI, merge conflicts), which are
  waiting on a reviewer, which have no reviewer at all, and which are drafts.
  Use this skill when asked "what PRs do I have open", "which of my PRs are
  approved", "what feedback haven't I addressed", for a PR status report, to
  triage an open-PR backlog, to find PRs ready to merge, or to act on that
  backlog (request first-pass bot reviews, merge approved PRs) across a GitHub
  organization such as mitodl.
license: BSD-3-Clause
metadata:
  category: process
---

# GitHub PR Triage

One command answers "what PRs do I have open, and what does each one need":

```bash
uv run --script skills/process/github-pr-triage/scripts/pr_status.py mitodl
uv run --script skills/process/github-pr-triage/scripts/pr_status.py mitodl/agent-kit   # one repo
uv run --script skills/process/github-pr-triage/scripts/pr_status.py mitodl --author someone-else
uv run --script skills/process/github-pr-triage/scripts/pr_status.py mitodl --json      # per-PR records
```

It prints a markdown report grouped by whose move it is, one row per PR with
a status line, and a one-line count at the end. Show that report to the user
as-is; don't re-derive it or re-bucket it. About 15 seconds for 85 PRs.

Default to report-only. Act (request reviews, merge, fix feedback) only when
the user asked for it, and follow [Acting on the report](#acting-on-the-report).

## Groups

Each PR lands in exactly one group, checked in this order:

| Group | Rule |
|-------|------|
| Drafts | Not marked ready for review |
| Needs you | Any unresolved review thread whose last comment isn't from the PR author; a human's review comment newer than the author's last push, comment, or thread reply; changes requested with no push or re-request since; CI failing; merge conflict |
| Ready to merge | Approved, CI green (or no CI), mergeable, nothing above |
| Waiting on others | Approved (CI still running or mergeability not computed yet), a review request pending, or a human has reviewed and the author has answered |
| No human reviewer yet | No human review and no pending request. Bot-only reviews show as `bot review only (...)` |

The status line carries the detail behind the group, e.g. `approved by Ardiea;
7 open threads awaiting the author (github-advanced-security 3, sentry 4); CI
failing; merge conflict`. An approved PR with unaddressed threads shows up in
"Needs you" with the approval in its status, so nothing is hidden by the
grouping.

## How the signals are read

"Feedback not addressed" comes from GitHub's own review-thread state, not a
timestamp guess. A thread that is unresolved and whose last comment is from
someone other than the PR author is waiting on the author. The status names
who opened each such thread, so bot threads (Copilot, Sentry, CodeQL) are
told apart from a colleague's at a glance. Threads marked outdated (the code
they point at has since changed) are counted separately: often already fixed
by a push and only needing a reply or a resolve.

Feedback left in a review body rather than an inline thread has no resolved
state, so it falls back to timing: a human's non-empty `COMMENTED` review
submitted after the author's latest push, PR comment, or thread reply is
reported as `review comment from <who> newer than the author's last push or
reply`. Bot review bodies are left out because they summarize findings that
also arrive as threads. Read the review itself before treating it as answered.

Approval is read from each reviewer's latest approve/request-changes review,
not only `reviewDecision`. On repos without required reviews (e.g. mit-learn)
`reviewDecision` stays empty even after an approval. Where it is
`REVIEW_REQUIRED` despite an approval and nobody has requested changes, the
status says `more approval required` and the PR is not counted as approved.
"Pushed since" for a change request compares the head commit against that
reviewer's change-request review, so the author's own thread replies don't
reset it.

`mergeability not computed yet` means GitHub hasn't evaluated the PR since its
last push or base change. It is not a conflict; it usually resolves the next
time anything views the PR.

## Limits

- `--author` defaults to `@me`. PRs where the user is a requested _reviewer_
  are a different query (`gh search prs --review-requested @me`); say so
  rather than mixing the two.
- Search returns at most 200 PRs. If the total in the summary line is 200,
  the report is truncated.
- Bot-authored PRs (Renovate, Dependabot) only appear with an explicit
  `--author`. For those, the `renovate-security-triage` and
  `dependency-updates` skills fit better.

## Acting on the report

Every action is visible to others or hard to reverse, so confirm the specific
PRs first. [references/action-playbook.md](references/action-playbook.md) has
the commands and edge cases. In short:

- Needs you: read the open threads and fix, reply, or resolve. The
  [`address-pr-feedback`](../address-pr-feedback/SKILL.md) skill does this for
  one PR at a time. CI failures and conflicts need case-by-case work.
- Ready to merge: `scripts/merge-pr.sh <repo> <number> [merge|squash|rebase]`,
  after the user has seen the list.
- No human reviewer yet: `scripts/request-review.sh <repo> <number>
  [copilot|claude|all]` for a bot first pass, or an in-session review with
  the [`code-review`](../code-review/SKILL.md) skill. Never request a human
  reviewer the user didn't name.
- Waiting on others: nothing by default. Offer a nudge only past a staleness
  threshold the user gives.
- Drafts: report only.
