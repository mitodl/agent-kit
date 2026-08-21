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

1. **No target given** — combine three sources: `git diff` (unstaged,
   tracked changes), `git diff --staged`, and untracked files. Plain `git
   diff`/`git diff --staged` never show untracked files — a working tree
   containing only a brand-new file looks empty to both — so check `git
   status --porcelain` for `??` entries and include them (`git add -N
   <file>` first makes each show up as an addition in the plain `git diff`
   without staging its content). Only fall back to `git diff HEAD~1` when
   all three are empty, and say explicitly that's what's being reviewed
   instead of silently reporting nothing.
2. **A branch name** — diff against where it forked from the default
   branch, not a plain two-dot diff: detect the default branch via `git
   symbolic-ref refs/remotes/origin/HEAD`. That ref only exists once a
   remote's HEAD has been set (`git clone` usually does this, but a
   fresh/local-only repo or an unset remote won't have it — verified: a
   bare `git init` with no remote raises "not a symbolic ref"); when it's
   missing, fall back to `gh repo view --json defaultBranchRef --jq
   .defaultBranchRef.name` if `gh` and a GitHub remote are available,
   otherwise ask the user which branch to diff against rather than
   guessing `main` or `master`. However the default branch was found, find
   the merge base (`git merge-base <default> <branch>`), then `git diff
   <merge-base>...<branch>`.
3. **A path** — `git diff HEAD -- <path>` (covers staged and unstaged
   changes to the path in one call — plain `git diff -- <path>` shows only
   unstaged, so a fully-staged change at that path would otherwise look
   like an empty diff), plus the same untracked-file handling as rule 1,
   scoped to that path. Same default-branch detection as rule 2 if a
   branch was also named.
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

Widening depth changes what the [verification pass](#verification-pass)'s
drop rule means. At the default depth, a finding that doesn't reproduce
gets dropped, full stop. On a widened pass, a finding that doesn't fully
reproduce is *kept*, not dropped — as long as it's explicitly labeled
lower-confidence and its `Failure scenario` states plainly what's
unconfirmed and why (see the lower-confidence row in
[references/findings-format.md](references/findings-format.md#worked-example)).
The drop-if-unreproduced rule is a default-depth rule, not a universal one.

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
claims are broken — and don't stop at the diff when the finding's
correctness turns on something outside it. A guard may already exist in an
unchanged caller, a changed API may violate a contract defined elsewhere in
the repo, or reproducing the scenario may need a definition the diff
doesn't include. The diff is the starting point, not the whole universe of
evidence — this matches [references/dimensions.md](references/dimensions.md)'s
own examples, which check every existing call site for a correctness
non-finding and require citing a real file:line for a reuse finding, not
just what changed. If a claim depends on code outside the diff's scope
(a different file, a different repo, a library's actual behavior), read
that code before the finding ships; if the code needed to verify a claim
is genuinely out of reach in the time available, that's grounds to drop
the finding at default depth (see [Depth](#depth) for the widened-pass
exception) — not to ship it as confirmed anyway.

At default depth, if the second read doesn't reproduce the failure
scenario as written, drop the finding — don't soften it into a maybe and
ship it anyway.

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
