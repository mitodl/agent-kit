---
name: address-pr-feedback
description: >
  Fetch, categorize, and address GitHub pull request review feedback --
  inline review comments, review threads, and top-level discussion comments
  -- including paginating through large or long-running PRs with many
  rounds of review. Applies fixes, replies to reviewers with evidence
  (never a generic "noted"), and marks resolved review threads via the
  GraphQL API. Use this skill when asked to "address PR feedback", "address
  the comments on <PR>", "resolve the review comments", "mark addressed
  comments as resolved", or to triage feedback from bots (Copilot, Gemini,
  CodeQL, Sentry) or human reviewers on a specific pull request.
license: BSD-3-Clause
metadata:
  category: process
---

# Address PR Feedback

Turns a PR's review activity into fixes plus a clean, fully-resolved thread
list. The core pattern is **fetch → categorize → address → reply & resolve →
verify**, using three scripts for the mechanical parts so the interesting
work is reading feedback correctly and knowing when a decision belongs to a
human instead of you.

| Script | Purpose |
|--------|---------|
| `scripts/fetch-feedback.sh` | Fetch review threads (paginated), discussion comments, and reviews in one JSON payload |
| `scripts/resolve-thread.sh` | Reply to (optional) and resolve one review thread by GraphQL node ID |
| `scripts/resolve-threads.sh` | Batch version: reads `[{"thread_id": "...", "comment": "..."}, ...]` from stdin |
| `scripts/reply-comment.sh` | Post a top-level PR comment — for discussion-comment replies or a final summary |

See [references/graphql-reference.md](references/graphql-reference.md) for
the underlying queries/mutations, why plain `first: N` GraphQL calls silently
truncate on long-running PRs, and why replying and resolving are separate
mutations.

## Recognizing the request

The trigger is almost always a short imperative naming a PR, sometimes
bundled with other git operations:

- "Address the PR feedback [on #N / \<url\>]"
- "For any comment that is addressed, mark it as resolved"
- "Address PR feedback, resolving comments that have been handled"
- "Rebase, resolve the conflicts, and then address the PR feedback"
- "Address feedback across the set of open PRs" / "walk through the PR chain,
  addressing feedback on the PRs first, then merge" (a stacked-PR batch)
- "Review the latest human-contributed feedback" / "there's more bot feedback
  to evaluate" (a re-check after previously addressing a first round)

Resolve two things before running anything, asking if either is unclear:

1. **Which PR(s)?** A URL or `#N` in the message is unambiguous — use it.
   When none is given, use this session as context before asking: if you
   opened or pushed to a PR earlier in this same conversation, that's almost
   always the one meant (dogfooding a skill you just built, continuing work
   you just pushed) — don't re-ask for something you already know. Otherwise
   fall back to `gh pr view --json url,number` for the current branch's
   tracked PR, and only ask the user if that's still ambiguous (no tracked
   PR, or reason to think the branch points at the wrong one). A request
   spanning multiple PRs (a stack, or "all my open PRs") means working
   through each one, one at a time — don't parallelize commits across PRs
   that depend on each other.
2. **Report or act?** This skill is almost always invoked to *act* (fix code,
   reply, resolve) — that's what "address" means here, unlike
   [`github-pr-triage`](../github-pr-triage/SKILL.md) which defaults to
   report-only. If the user says "review the feedback" or "give me a summary"
   without "address"/"fix"/"resolve", treat it as read-only: categorize and
   report, but don't change code or touch thread state.

## Phase 1 — Fetch

```bash
./skills/process/address-pr-feedback/scripts/fetch-feedback.sh mitodl/agent-kit 116 /tmp/pr116-feedback.json
```

This is one call regardless of PR size — it follows GraphQL cursor pagination
for review threads internally, so a PR with 200 threads across a dozen
review rounds comes back complete, not truncated at the first page. By
default it excludes already-resolved threads; add `--include-resolved` when
you need the full history (e.g. re-checking a PR you partially addressed in
an earlier session, or producing a final "here's everything, resolved and
not" summary).

Output shape: `{repo, pr, review_threads: [...], discussion_comments: [...],
reviews: [...]}`. Each `review_threads[]` entry carries the GraphQL `id`
(pass this straight to `resolve-thread.sh`), `isResolved`, `isOutdated`,
`path`/`line`, and its `comments`.

## Phase 2 — Categorize

Group by reviewer and tag each item with a priority, inheriting the
reviewer's own label when it supplies one (bot reviewers like
`gemini-code-assist` and `copilot-pull-request-reviewer` usually embed a
severity in the comment body or as a badge) — otherwise infer from
substance:

```
1. **gemini-code-assist** (medium): transport type mismatch in agent-config.toml
2. **copilot-pull-request-reviewer** (nitpick): variable naming in fetch loop
3. **human — tmacey** (substantive): reconsider the retry backoff strategy
```

`isOutdated: true` does **not** mean "safe to skip" — the line moved but the
underlying concern may still apply. Read the current code at that path
before deciding a stale-looking thread is moot.

Bot review-state fields (`APPROVED`/`COMMENTED`/`CHANGES_REQUESTED` in
`reviews[]`) tell you formality, not substance — most bots (Copilot, Gemini)
leave everything in `COMMENTED` state with the real content in the thread
comments, same as noted in
[`github-pr-triage`](../github-pr-triage/SKILL.md#phase-3--classify).

## Phase 3 — Address

For each actionable item: make the code change, run the relevant
tests/lint/build for that change before moving to the next one. Commit with
a message that names what was addressed, not just "address PR feedback".

**Disagreeing with a reviewer is a legitimate outcome — but it must be
verified, not assumed.** Before skipping a suggestion because it "looks
unnecessary": read the actual code path the reviewer is pointing at and
confirm your reasoning holds (e.g. a suggested defensive check is redundant
only if you've confirmed the caller genuinely always guarantees the
precondition — check every call site, not just the obvious one). Once
verified, say so explicitly in the reply (with the evidence) — never resolve
a thread by silently ignoring it, and never reply with a content-free "noted"
or "will consider". A good decline reads like: *"Verified — `write_bindings`
always calls `ensure_bridge_store` first at every call site, so this
fallback path is unreachable. Not adding it; would reintroduce the redundant
read this PR removes."*

## Phase 4 — Reply & resolve

For each thread you touched, reply with what changed (cite the commit) and
resolve in one call:

```bash
./skills/process/address-pr-feedback/scripts/resolve-thread.sh \
  --thread-id PRRT_kwDORsi4Ac6PsuPQ \
  --comment "Fixed in a1b2c3d: switched transport to streamable-http per the schema."
```

For a batch, build the JSON and pipe it once:

```bash
jq -n '[
  {thread_id: "PRRT_...", comment: "Fixed in a1b2c3d: ..."},
  {thread_id: "PRRT_...", comment: "Verified — false positive, see reply. No change needed."}
]' | ./skills/process/address-pr-feedback/scripts/resolve-threads.sh
```

Top-level discussion comments have no resolution state — reply to those (or
post one consolidated summary of the whole pass) with `reply-comment.sh`:

```bash
./skills/process/address-pr-feedback/scripts/reply-comment.sh mitodl/agent-kit 116 \
  "Addressed all review feedback: 4 threads fixed and resolved, 1 declined (see reply) with reasoning."
```

Always dry-run (`--dry-run` on both resolve scripts) first when acting on a
batch you haven't manually reviewed thread-by-thread, and check the printed
list before re-running for real.

## Phase 5 — Verify

Re-run `fetch-feedback.sh` (without `--include-resolved`) and report the
remaining unresolved count. If anything is still open, say specifically why
(deferred pending a human decision, blocked on another PR, a design choice
you're waiting on) — never let a comment silently drop off the report.

## When to stop and ask instead of deciding

These are not edge cases to handle gracefully and move on from — they're
signals to pause the batch and get the user's input before continuing:

- **Security/compliance-flagged findings** (CodeQL alerts, Sentry issues,
  anything touching auth/secrets). Never auto-dismiss one as a false
  positive, no matter how confident the verification looks — surface it with
  your evidence and ask whether to dismiss or leave it open. If the tooling
  itself blocks the action (some environments gate CodeQL alert dismissal
  behind explicit confirmation), that's a signal to use `AskUserQuestion`,
  not to route around it.
- **A design decision with more than one reasonable fix** — e.g. a
  reviewer flags a race condition and a cheap guard vs. a more thorough
  lock-based rewrite are both defensible. Present the options with
  tradeoffs; don't silently pick the one that looks safer or less invasive.
- **Feedback that conflicts** — with another reviewer, or with a decision
  already made elsewhere in this PR or PR stack. Surface the conflict rather
  than picking a side unprompted.
- **Scope expansion** — a suggestion that implies work outside this PR (split
  into a separate PR, touch a different service/repo, merge in a dependency
  PR first). Ask before doing it, even if it seems like the "right" call.
  ("Address feedback and consider whether it makes sense to merge PR #3565
  into this work" is the user opting into that judgment call explicitly —
  don't assume it by default elsewhere.)
- **Genuinely ambiguous feedback** — unclear what change is being requested,
  or from whom, or what "this" refers to in a short comment. Ask rather than
  guess at intent; a wrong guess costs more than the question.
- **Anything hard-to-reverse or externally visible beyond replying/resolving**
  — merging, force-pushing, closing the PR — stays out of scope for this
  skill unless explicitly requested in this same ask.

If the user has already said something like *"for any case where a decision
is necessary, ask me instead of guessing"*, treat that as standing for the
rest of the batch, not just the first ambiguous item you hit.

## Environment

Requires `gh` (authenticated, with write access on the target repo — see the
[permission-errors note](references/graphql-reference.md#permission-errors))
and `jq`. No dependency on any repo-specific tooling; the scripts work from
any checkout via `owner/repo` + PR number.
