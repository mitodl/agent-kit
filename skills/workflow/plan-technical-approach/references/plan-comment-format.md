# Plan comment format

The shape of the plan comment created by
[`plan-technical-approach`](../SKILL.md) and maintained by
[`implement-spec`](../../implement-spec/SKILL.md), and the marker contract that
lets it be updated in place instead of re-posted.

**Which form you write depends on the route.** Work with a spec puts the plan
in `TECHNICAL.md` and the comment becomes a pointer — see
[Short form](#short-form-when-there-is-a-spec). Fast-path work has no spec, so
the comment *is* the plan, in the full form below.

## The marker contract

The comment opens with a marker line and closes with a digest line:

```text
<!-- ol-plan:v1 -->
...plan body...
<!-- ol-plan:sha:<sha256 of everything above this line> -->
```

**Both are written by `scripts/upsert-plan-comment.sh`, not by hand.** It
strips any markers in the body it is given before adding its own, so a body
that was round-tripped through a read cannot end up with two.

The marker is how the next run finds this comment instead of posting a second
one. The digest is how a second session discovers it is about to overwrite an
edit it never saw — GitHub's PATCH ignores `If-Match`, so the check has to
travel inside the body.

**The strip-HTML-comments rule does not apply here.**
[`create-ol-github-issue`](../../../process/create-ol-github-issue/SKILL.md)
says to strip `<!-- ... -->` from a finished issue **body**, where those
comments are leftover template instructions. This is a **comment**, and these
two markers are load-bearing: strip them and the next update silently posts a
duplicate instead of editing.

## Writing rules

These are inherited, not local inventions:

- **Never hard-wrap.** Each paragraph and each bullet is one long line; GitHub
  wraps it for the reader. GitHub's comment renderer turns a single newline
  inside a paragraph into a line break, so an 80-column wrap that looks tidy in
  an editor renders as a column of ragged half-lines. Newlines belong between
  blocks, between list items, and inside code fences. See
  `configs/agent-communication-style.md`.
- **Self-contained.** The comment is public on a public repo and is read by
  people with no access to the machine that wrote it. No local paths, no
  internal hostnames, no secrets, no "see the output above". Reference code and
  issues by full GitHub URL.
- **Dates come from `date -u +%Y-%m-%d`,** never from memory. An agent's idea
  of today's date is routinely wrong, and a wrong date in a log someone reads
  in six months is worse than no date.
- **Voice.** Write as the engineer explaining the decision to a teammate. No
  throat-clearing, no inflated adjectives, no closing summary restating the
  body.

## Template

```markdown
## Plan

**Approach:** <one line naming the mechanism>

<One paragraph: what this does and why it won. Name the criterion that decided it.>

**Phases**

- [ ] <Phase 1> — done when <verifiable condition>
- [ ] <Phase 2> — done when <verifiable condition>

**Delivery**

<"Single PR, ~N lines." — or the chunk table, plus whether chunks are stacked or independent.>

| # | Chunk | Axis | Est. LOC | Depends on | PR |
|---|-------|------|----------|------------|-----|
| 1 | <what ships> | <split axis> | ~N | — | |

**Tradeoffs**

| Giving up | To get | Revisit when | Decided by | Follow-up |
|---|---|---|---|---|
| <concrete> | <concrete> | <observable signal> | @<handle> | <issue URL, required when this row is an accepted workaround> |

**Not chosen**

- <Approach B> — <one line: why it lost, and the condition under which it would have won>

**Strongest argument against this plan:** <one line>

**Status:** <current phase, or blocked on what>

**Revisions**

- <YYYY-MM-DD> — <what changed>
```

Omit a section that has nothing in it. An empty **Tradeoffs** table is a
legitimate outcome — it means nothing was given up — and is more honest than a
row invented to fill it. Fast-path work legitimately has no **Tradeoffs**, no
**Not chosen** and no scores; see the trivial-change example below.

## Short form: when there is a spec

For spec'd work the spec is the source of truth and the comment is a pointer.
Approach, tradeoffs and the chunk table live in `TECHNICAL.md` **only** —
recording them in both gives you two copies that drift, and the issue copy is
the one that goes stale, because the spec sits next to the code and moves with
it in the same pull request.

```markdown
## Plan

**Spec:** [`specs/saved-search-notifications/`](https://github.com/mitodl/mit-learn/tree/main/specs/saved-search-notifications) — [PRODUCT.md](https://github.com/mitodl/mit-learn/blob/main/specs/saved-search-notifications/PRODUCT.md) · [TECHNICAL.md](https://github.com/mitodl/mit-learn/blob/main/specs/saved-search-notifications/TECHNICAL.md)

Three chunks, independent PRs. The approach, tradeoffs and delivery plan are in TECHNICAL.md; this comment tracks status only.

**Status:** Chunk 1 merged (#4821), chunk 2 in review (#4834). Chunk 3 waits on chunk 2 reaching production.

**Revisions**

- 2026-10-28 — Chunk 1 merged; opened chunk 2.
- 2026-10-21 — Spec approved, plan recorded.
```

Link the spec by **full GitHub URL**, not a repo-relative path: the comment may
be on a tracker issue in a different repo, where a relative path resolves to
nothing. Until the first PR merges those links 404 — that is expected, since
the spec lands with the first implementation PR. Say so in the comment if the
work will sit unmerged for a while.

A deviation approved during implementation is recorded in `TECHNICAL.md`'s
Deviations table, not here. The comment's Revisions log notes that a deviation
happened and points at the PR; the reasoning lives with the design.

## Worked example — competing approaches

```markdown
## Plan

**Approach:** Move retry handling into the shared client wrapper.

Both copies of the retry logic are reachable from the scheduler, and they have already drifted — one honours `max_attempts` and the other ignores it, which is the bug in this issue. Consolidating into the wrapper in `clients/` fixes both call paths at once and puts the behavior where every other cross-service read already looks for it. Architectural fitness decided it: the alternative works, but leaves this the only read path in the repo that does its own retrying.

**Phases**

- [ ] Add retry policy to the client wrapper — done when the wrapper's tests cover exhaustion, backoff and the non-retryable 4xx case
- [ ] Migrate the scheduler path — done when `code_callers` shows no direct `requests.get` left in `scheduler/`
- [ ] Delete the second copy — done when the module is gone and the suite is green

**Delivery**

Three chunks, independent PRs. Chunk 3 depends on chunk 2 being **deployed**, not merged: the old copy is the rollback target until the new path has run in production, so deleting it at merge time would leave nothing to revert to.

| # | Chunk | Axis | Est. LOC | Depends on | PR |
|---|-------|------|----------|------------|-----|
| 1 | Retry policy in the client wrapper | Functional layer — domain logic | ~280 | — | #4821 (merged) |
| 2 | Migrate the scheduler path | Functional layer — consumers | ~190 | 1 merged | #4834 (open) |
| 3 | Delete the second copy | Risk isolation — pure removal | ~120 | 2 deployed | |

**Tradeoffs**

| Giving up | To get | Revisit when | Decided by | Follow-up |
|---|---|---|---|---|
| Per-call-site retry tuning, which two callers use today | One retry policy that cannot drift again | A caller needs a genuinely different policy — at which point make it a wrapper argument, not a second implementation | @rhysyngsun | — |

**Not chosen**

- Fix the drifted copy in place — would have won if the second copy were being deleted for other reasons this quarter, which it is not; it leaves two implementations and the same drift recurs.
- Retry at the scheduler level — would have won if the failures were scheduler-specific, but the traces show them on the direct API path too, which the scheduler never touches.

**Strongest argument against this plan:** it touches a wrapper used by 14 call sites to fix a bug that reaches 2, so a mistake in the policy has a much larger radius than the bug does.

**Status:** Chunk 1 merged, chunk 2 in review. Chunk 3 waits on chunk 2 reaching production.

**Revisions**

- 2026-10-21 — Chunk 1 merged (#4821); opened chunk 2 (#4834).
- 2026-10-14 — Initial plan.
```

## Worked example — sole candidate

When planning found no genuine alternative, the comment must not look like the
output of a comparison. Drop **Not chosen**, keep everything else, and say so
in one line:

```markdown
## Plan

**Approach:** Add the missing `None` guard in `parse_window`.

No alternative mechanism found — the function has one caller, the input that crashes it is well defined, and every other shape of fix reduces to this same guard in a different place. Scored for its tradeoffs rather than ranked.

**Phases**

- [ ] Guard and regression test — done when the crashing input from the issue is covered and the suite is green

**Delivery**

Single PR, ~30 lines.

**Tradeoffs**

_None._ The guard is local to one function and reverts cleanly.

**Strongest argument against this plan:** it treats the symptom; the `None` arrives from an upstream parser that arguably should not emit it. Filed as a follow-up rather than widened into this fix.

**Status:** Phase 1 in progress.

**Revisions**

- 2026-10-14 — Initial plan.
```

## Worked example — trivial change (fast path)

A change that scoping classified as trivial skips the alternatives and the ledger
entirely. The comment is four lines plus a revision log, and padding it out
would add nothing a reader wants.

```markdown
## Plan

**Approach:** Bump the empty-state copy to match the Figma, in `SavedSearchEmpty.tsx`.

Fast path — one obvious change, one component, no shared contract, reverts cleanly.

**Phases**

- [ ] Copy updated — done when the string matches the Figma and the snapshot test is regenerated

**Delivery**

Single PR, ~15 lines.

**Status:** In progress.

**Revisions**

- 2026-10-14 — Initial plan.
```

Note what is absent and should stay absent: no **Tradeoffs** table with a row
invented to fill it, no **Not chosen** section listing approaches nobody
considered, no **Strongest argument against** for a copy change. A reader
scanning this issue in six months gets what they need in one screen.

## Worked example — accepted workaround

The case the **Follow-up** column exists for. Note that the row is not a
complete record on its own: it points at an issue that carries the detail, and
the code carries a comment pointing at the same issue.

```markdown
## Plan

**Approach:** Cap the export at 10,000 rows and return a truncation notice.

The timeout is caused by building the whole export in memory before streaming it, and the real fix is a cursor-paginated streaming export — roughly a week, touching the serializer, the task runner and the download UI. The quarter-end reporting window opens Monday and the current failure blocks it entirely. Capping is a day, unblocks the window, and does not foreclose the streaming fix. Taken as a deliberate workaround, not as the design.

**Phases**

- [ ] Cap and truncation notice — done when an export over the cap returns the first 10,000 rows plus a visible notice, and the timeout no longer reproduces

**Delivery**

Single PR, ~120 lines.

**Tradeoffs**

| Giving up | To get | Revisit when | Decided by | Follow-up |
|---|---|---|---|---|
| Complete exports for the 4% of accounts over 10,000 rows, who now silently get partial data unless they read the notice | A working export before the quarter-end window opens Monday | Any account hits the cap twice in a month, or the reporting window closes — whichever is first | @rhysyngsun | mitodl/mit-learn#5120 |

**Strongest argument against this plan:** a truncation notice is easy to miss, so the failure mode changes from an obvious timeout to silently incomplete data — which is worse for anyone who does not read it. Accepted because the alternative is no export at all this quarter, and the follow-up is scheduled rather than aspirational.

**Status:** Phase 1 in progress.

**Revisions**

- 2026-10-14 — Initial plan. Opened mitodl/mit-learn#5120 for the streaming export.
```

The follow-up issue carries what this row cannot: why streaming was deferred,
what the real fix involves, and — once the workaround merges — the PR URL whose
diff has to be undone. The code gets `# Capped export: see mitodl/mit-learn#5120`
at the cap, which is how someone reading the serializer in six months finds any
of this.

## Keeping the Revisions log bounded

GitHub rejects a comment body over 65,536 characters, and
`upsert-plan-comment.sh` fails before the call rather than letting it surface
as an opaque 422. On long-running work the Revisions log is what grows. Trim it
to the most recent entries plus a count of what was dropped:

```markdown
**Revisions**

- 2026-11-02 — Phase 3 done; added the backfill step after the row count came in 4x higher than estimated.
- 2026-10-28 — Phase 2 done.
- _(11 earlier revisions elided)_
```

Never solve it by deleting the comment and posting a fresh one: the log is the
record of how the plan moved, and the comment's own edit history is the only
other copy.
