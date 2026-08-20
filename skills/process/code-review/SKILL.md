---
name: code-review
description: >
  Review a diff, branch, path, or PR for correctness bugs and
  reuse/simplification/efficiency cleanups, using a verify-before-reporting
  pass so findings are checked against the actual code rather than
  pattern-matched. Portable across agent platforms (Claude Code, pi,
  Copilot, OpenCode) — needs only `git diff` and a rubric, no MCP or witan
  dependency. Use this skill when asked to "review this diff", "review my
  branch", "code review PR #N", "review the changes", "check this for
  bugs", or to review staged/unstaged changes before opening a PR. Report-only
  by default; only edits code when the request explicitly says to fix the
  findings too.
license: BSD-3-Clause
metadata:
  category: process
---

# Code Review

Reviews a diff against four dimensions — correctness, simplification,
efficiency, reuse — and reports findings as a severity-ordered table. Every
finding is re-checked against the actual code before it ships, so the
report doesn't carry a pattern-matched guess dressed up as a bug.

See [references/dimensions.md](references/dimensions.md) for the four
dimensions with worked examples of a real finding vs. a non-finding. See
[references/findings-format.md](references/findings-format.md) for the
table schema and a full worked example.

## Scope input

Resolve what to review from the request, in this order:

1. **No target given** — `git diff` (unstaged) plus `git diff --staged`,
   combined. If both are empty, fall back to `git diff HEAD~1` and say
   explicitly that's what's being reviewed instead of silently reporting
   nothing.
2. **A branch name** — diff against where it forked from the default
   branch, not a plain two-dot diff: detect the default branch (`git
   symbolic-ref refs/remotes/origin/HEAD`), find the merge base (`git
   merge-base <default> <branch>`), then `git diff <merge-base>...<branch>`.
3. **A path** — `git diff -- <path>` (or the branch-scoped equivalent above
   if a branch was also named), same default-branch detection.
4. **A PR number** — when `gh` is available and the repo has a GitHub
   remote, `gh pr diff <number>`.

State which of these applied before reporting findings — "reviewing the
diff between `main` and `feature-x`" — so the reader isn't guessing what
was actually in scope.

## Depth

Default to high-confidence findings only — the kind you'd stake your name
on, not a maybe. If the user asks for a deeper pass ("be thorough", "don't
hold back", "look harder"), widen to include findings you're less certain
about, and label those explicitly as lower-confidence in the report rather
than presenting them with the same weight as a confirmed bug. There's no
flag or parameter for this — some platforms this skill runs on have no
argument-passing mechanism, so the depth signal has to come from reading
the request, not from a tier number.

## Dimensions

Four dimensions, most severe first when findings are reported:

1. **Correctness** — a bug: wrong output, a crash, or a concrete input that
   fails.
2. **Simplification** — unneeded complexity: premature abstraction, dead
   branches, a helper that exists for one caller.
3. **Efficiency** — avoidable extra work: N+1 queries, redundant
   recomputation, an unnecessary full scan where an indexed lookup exists.
4. **Reuse** — logic in this diff that duplicates something already in the
   repo, that should call the existing implementation instead.

Full rubric with worked examples: [references/dimensions.md](references/dimensions.md).

## Verification pass

Before a finding goes in the final report, re-read the exact lines it
claims are broken. If the second read doesn't reproduce the failure
scenario as written, drop the finding — don't soften it into a maybe and
ship it anyway. The diff itself is the evidence here, already in context,
so this is a re-read, not a separate fetch-and-check pass the way it is
when reviewing a PR discussion.

For a **reuse** finding specifically, verifying means actually locating the
existing implementation (file:line) — "this probably exists elsewhere" is
not verified; grep for it and cite where.

## Findings format

Flat, severity-ordered markdown table:

| # | Severity | File:Line | Summary | Failure scenario |
|---|----------|-----------|---------|-------------------|

`Failure scenario` is mandatory and concrete — concrete inputs or state
that produce a wrong output or crash. A row that can't state one is a
suspicion, not a finding, and gets dropped in the verification pass above.
For simplification/efficiency/reuse findings, `Failure scenario` becomes
"what it costs" (the maintenance burden, the extra query, the duplicated
logic's drift risk) rather than a crash.

No inline fixes in the table — those belong to fix mode only, below, so a
plain review never mutates anything by accident. Full schema and a worked
example: [references/findings-format.md](references/findings-format.md).

If nothing survives the verification pass, say so plainly ("no
high-confidence findings across the four dimensions") rather than padding
the report with low-confidence guesses to have something to show.

## Fix mode

Report-only by default. If the request already says to fix the findings
too ("review and fix", "review this and clean it up"), apply fixes for the
confirmed findings *after* the review is complete and reported — never
mid-review, before the list is final. Findings dropped in the verification
pass are never applied.

## Environment

Needs `git` (always) and, for PR-number targets, `gh` authenticated against
the target repo. No MCP server, no witan dependency, no repo-specific
tooling beyond that — the scope-resolution rules above work from any
checkout.
