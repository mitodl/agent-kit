---
name: github-pr-triage
description: >
  Enumerate your open GitHub pull requests across an org (or a single repo) and
  categorize each by the action it actually needs: needs a first-pass bot review
  (Copilot/Claude/Gemini), awaiting a requested reviewer, has review feedback to
  address, formally has changes requested, or is approved and ready to merge.
  Use this skill when asked for a PR status report, to triage an open-PR backlog,
  to find PRs ready to merge, to see which PRs are stuck waiting on the author,
  or to process pull requests across a GitHub organization such as mitodl.
license: BSD-3-Clause
metadata:
  category: process
---

# GitHub PR Triage

Turns a sprawling list of open PRs into a report grouped by required action, and
optionally executes that action. The core pattern is
**enumerate → enrich → classify → report → (optionally) act**, using
`gh search prs` / `gh pr view` for data and deterministic `jq` rules for the
classification — no code archaeology needed, unlike issue triage.

Three deterministic scripts do the mechanical work, so most of this skill is
about reading their output correctly and knowing when to escalate to judgment:

| Script | Purpose |
|--------|---------|
| `scripts/fetch-prs.sh` | Enumerate open PRs by author across an org |
| `scripts/enrich-prs.sh` | Fetch review/check/mergeability detail per PR, in parallel |
| `scripts/classify-prs.sh` | Bucket the enriched PRs by required action |

Action scripts (used only after explicit per-PR confirmation — see Phase 5):

| Script | Purpose |
|--------|---------|
| `scripts/request-review.sh` | Request a first-pass review from Copilot and/or Claude; refuses drafts and PRs that already have review activity |
| `scripts/merge-pr.sh` | Merge an approved, green PR and delete its branch |

---

## Scope: figure out mode before running anything

Two independent choices govern the whole run — resolve both up front, asking
the user if either is ambiguous:

1. **Report or act?** A pure status report never mutates anything. Acting means
   requesting reviews, posting comments, pushing fixes, or merging — all of
   which are visible to others and some of which are hard to reverse. Default
   to **report-only** unless the user clearly asked to take action ("merge the
   ready ones", "kick off reviews", "clear my backlog").
2. **Which org/repo, which author?** Default author is `@me` (the current `gh`
   user) — this skill is built around "my open PRs", matching how the four
   buckets below are framed (things *you*, the author, are waiting on or need
   to act on). If the user wants PRs where they're a *reviewer* instead, that's
   a different query (`gh search prs --review-requested @me`) — say so rather
   than silently reinterpreting the request.

---

## Phase 1 — Enumerate

```bash
./skills/process/github-pr-triage/scripts/fetch-prs.sh mitodl /tmp/prs_fetched.json
# or a non-default author:
./skills/process/github-pr-triage/scripts/fetch-prs.sh mitodl /tmp/prs_fetched.json --author someone-else
```

This is one `gh search prs --owner <org> --author <user> --state open` call, so
it works across every repo in the org in one shot — no per-repo iteration
needed. It captures `number, title, url, repository, isDraft, createdAt,
updatedAt, author`, but **not** review state — GitHub's search JSON doesn't
expose `reviewDecision`, so that requires a per-PR detail call (Phase 2).

---

## Phase 2 — Enrich

```bash
./skills/process/github-pr-triage/scripts/enrich-prs.sh /tmp/prs_fetched.json /tmp/prs_enriched.json
```

Runs `gh pr view` per PR (8-way parallel) to pull `reviewDecision,
latestReviews, reviewRequests, statusCheckRollup, mergeable, mergeStateStatus,
comments`. For a normal backlog (tens of PRs) this takes a few seconds. If a
single PR's detail fetch fails (deleted branch, permissions), it's silently
dropped rather than aborting the whole run — check the count in the output
message against the fetch count if that matters.

---

## Phase 3 — Classify

```bash
./skills/process/github-pr-triage/scripts/classify-prs.sh /tmp/prs_enriched.json > /tmp/prs_classified.json
```

Buckets, in the order the report should present them (most actionable first):

| Bucket | Meaning | Typical action |
|--------|---------|-----------------|
| `approved_ready_to_merge` | `APPROVED`, mergeable, checks green | Merge |
| `approved_blocked` | `APPROVED` but conflicting / checks red / mergeability not yet computed | Resolve conflict, fix CI, or just re-check later |
| `changes_requested` | Formal `CHANGES_REQUESTED` review | Address feedback, push, re-request review |
| `has_review_comments` | A review left in `COMMENTED` state (this is how Copilot, Gemini, and most review bots report findings — they rarely use formal approve/request-changes) but no formal decision | Read the comments, decide if they're addressed |
| `awaiting_review` | A reviewer (human or bot) is requested; nothing submitted yet | Nothing to do — wait, or nudge if stale |
| `needs_first_pass_review` | No reviews, no reviewer requested at all | Kick off a first-pass review |
| `draft` | Not marked ready for review | Report separately; skip review/merge actions |

**`reviewDecision` alone under-counts feedback.** GitHub only sets it for
formal `APPROVED` / `CHANGES_REQUESTED` reviews. Bot reviewers overwhelmingly
leave `COMMENTED`-state reviews with the substance in the review body — those
show up with `reviewDecision: ""` and land in `has_review_comments`. In
practice this is usually the *largest* bucket. Don't skip it just because
`reviewDecision` looks empty.

**`mergeable` can be `"UNKNOWN"`.** GitHub computes mergeability lazily in the
background; a PR that was just pushed to, or hasn't been polled recently, can
show `UNKNOWN` even with no real conflict. `classify-prs.sh` surfaces this as
`blocked_reason: "unknown_pending_recompute"` — re-run `scripts/pr-detail.sh`
on that PR (or just `gh pr view`) after a few seconds before assuming it's
actually blocked.

**`feedback_likely_addressed` is a heuristic, not a verdict.** For
`has_review_comments` and `changes_requested`, the classifier compares the
newest review's timestamp against the newest PR comment's timestamp — if a
comment landed after the review, someone probably already responded. It has
no visibility into new commits pushed without a comment, and it can't tell
whether the response actually resolved the concern. Use it to *prioritize*
which PRs to read first, never to skip reading them.

---

## Phase 4 — Report

Present the classification as grouped markdown tables, ordered per the bucket
table above (most actionable first), each PR as one row:

```markdown
### ✅ Approved & ready to merge (3)
| PR | Title | Repo |
|----|-------|------|
| [#4902](https://github.com/mitodl/ol-infrastructure/pull/4902) | Add Pulumi program for ol-analytics-api K8s deployment | ol-infrastructure |

### 🚧 Approved but blocked (3)
| PR | Reason |
|----|--------|
| [#4693](...) | unknown_pending_recompute — re-check mergeability |

### 🔴 Changes requested (4)
...

### 💬 Has review comments — verify addressed (32, ~2 likely already addressed)
...

### ⏳ Awaiting review (N)
...

### 🆕 Needs first-pass review (1)
...

### 📝 Draft (2)
...
```

Keep rows terse — title, repo, one-line status. Put counts in the section
headers so the user can see backlog shape at a glance. End with a one-line
bottom line: "N ready to merge, M need your response, K just need a first
pass."

If this is report-only, stop here.

---

## Phase 5 — Act (only when the user asked for it)

Confirm before mutating anything **per PR or per clearly-scoped batch** — see
[references/action-playbook.md](references/action-playbook.md) for the exact
commands, confirmation phrasing, and edge cases for each bucket. Summary:

- **`needs_first_pass_review`** — request a bot review with
  `scripts/request-review.sh <repo> <number> [copilot|claude|all]` (default
  `all`; it self-guards against drafts and PRs that already have review
  activity), and/or run this session's built-in `/review <pr-url>` to have
  Claude leave its own first-pass review directly.
- **`approved_ready_to_merge`** — confirm per PR, then
  `scripts/merge-pr.sh <repo> <number> [merge|squash|rebase]`. Never batch-merge
  without the user seeing the specific list first.
- **`changes_requested` / `has_review_comments` (not likely addressed)** —
  read the actual review bodies and comments, check out the branch locally if
  code changes are needed, fix, push, then comment summarizing what changed
  (see the playbook for the multi-repo parallel-fix pattern).
- **`awaiting_review`** — no action by default; offer to post a polite nudge
  comment only for PRs stale beyond a threshold the user specifies.
- **`approved_blocked`** — surface the reason; only `unknown_pending_recompute`
  is safely auto-resolved by re-checking. Conflicts and failing checks need
  case-specific investigation.
- **`draft`** — report only, never act.

See the playbook for command-level detail on each of these.
