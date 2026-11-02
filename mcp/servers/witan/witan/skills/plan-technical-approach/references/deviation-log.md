# The deviation log

Where a plan's **movement** is recorded: what was assumed, what was found that
contradicted it, and what was agreed instead.

[`implement-plan`](../../implement-plan/SKILL.md) Step 4 requires the engineer
to approve a deviation **before** the deviating code is written. Today that
approval leaves no artifact. The spec rewrite shows what is true *now*; the
commit message is written afterwards and says what was *done*. Neither records
what was agreed, when, or by whom — and a superseding ADR needs exactly *"why
the decision changed, not just what the new one is"*
([revision protocol](../../write-adr/references/revision-protocol.md)). This
file is that record.

**It is not a second copy of the spec.** The spec says what is true. This says
what changed and on whose say-so. Different facts, so they do not drift against
each other.

## Where it lives

`<spec-dir>/<slug>-deviations.md`, one file per body of work, newest entry
first.

Resolve `<spec-dir>` with
[`write-spec`'s repo-locations](../../write-spec/references/repo-locations.md)
— the same lookup the spec itself used. `docs/plans/` is a flat directory whose
documents are distinguished by suffix (`-spec.md`, `-analysis.md`,
`-spike-results.md`), so `-deviations.md` follows a convention that is already
there. **Do not invent a directory per piece of work**: that is exactly the
imposition `write-spec`'s detect-don't-impose rule exists to prevent.

## Which work gets one

| Tier | When the file is created |
|---|---|
| **Spec** | At spec time, empty but for its header. Named in the spec's header block beside `Project:` — a log nobody can find is a log nobody reads |
| **One or two PRs with an ADR** | On the first deviation, not before |
| **Trivial** | Never. A change that reverts cleanly in tens of lines does not deviate; if it does, it was not trivial and the triage was wrong |

## The entry

Six fields, one line each, **800 characters hard**. The cap is the point: an
entry that does not fit is carrying reasoning, and reasoning belongs in the
`Dn.` section or the ADR it changed.

```markdown
### 2026-10-26 — chunk 4 splits into 4a/4b

- **Assumed:** one CSP policy for the whole site, enforced in one chunk.
- **Found:** Learn ships its own embed feature, so `frame-ancestors` has to be `*` on the embed routes and `'self'` everywhere else.
- **Changed to:** two policies from chunk 3; chunk 4 splits so a regression stays attributable to one policy.
- **Touches:** D7, D8 (spec) · ADR-0014
- **Approved by:** @rhysyngsun
- **PR:** https://github.com/mitodl/mit-learn/pull/4183
```

**There is deliberately no "what we learned" field.** A lesson that outlives
this work — *"this RFC has now been wrong twice about browser behaviour, both
times stated as fact with no citation beside it"* — goes to `memory_store`,
where the team can search it and where it is not scoped to one spec. Put it
here and it dies with the work that happened to surface it.

Other rules, each closing a way this file turns back into a changelog:

- **Dates come from `date -u +%Y-%m-%d`,** never from memory. An agent's idea
  of today's date is routinely wrong.
- **Written at approval, before the deviating code.** An entry written
  afterwards is a note, not a record of what was agreed.
- **Lands in the pull request that carries the deviation,** so a reviewer sees
  the change and its justification together.
- **Never edit an entry.** A deviation later reversed gets a second entry
  saying so. This file is append-only — which is safe precisely because it is
  bounded, consumed, and dies with the work.

## It is read twice, not archived once

A log consumed only "once the work is complete" is a log that rots, because
work often has no clean end. This one has a trigger that fires mid-flight.

**1. The replan threshold.** `implement-plan` counts the entries that change
the chunk graph's shape — a chunk split, added, reordered or dropped. At the
**third**, repair stops and the work goes back to
[`plan-technical-approach`](../SKILL.md), which rewrites the plan from these
entries rather than patching it again.

The threshold is not arbitrary. Repair buys *stability* — it preserves the
commitments already attached to the plan, the open PRs and claimed tasks — and
that value decays as divergence grows, to the point where repairing costs more
than replanning and yields a worse plan. Past a few shape changes you no longer
have the plan you are repairing.

**2. Close-out.** Before `workflow_project_complete`, read the whole file.
Every ADR these entries touched gets **one** superseding record via
[`write-adr`](../../write-adr/SKILL.md), covering everything that moved and
taking its *why* from here. This is the only moment an ADR changes: accepted
records are immutable, and nothing is written to one mid-work. See
[when a superseding record gets written](../../write-adr/references/revision-protocol.md#when-a-superseding-record-gets-written).

## Then delete it

**This file is consumed, not archived.** Once close-out has read it, `git rm`
it — in the **same pull request** as the superseding ADR, so one diff shows the
record appearing and the log being consumed together and a reviewer can see
that nothing was dropped quietly.

The record that outlives it cites where it came from, which is what makes the
deletion recoverable rather than lossy:

```markdown
Derived from the deviation log at `a3f91c2`.
```

Get the sha with `git rev-parse --short HEAD` **before** removing the file.
Same convention as a spec's commit anchor, and the same reason: a reference
with no sha rots.

Three reasons it goes rather than stays:

- **It poisons the convention detection.** `write-spec` learns a repo's house
  style by reading the **newest** documents in the spec directory. A deviation
  log is written during implementation, so it is *by construction* newer than
  the spec it describes and sorts above it every time. Leave a few lying about
  and the next spec is modelled on deviation logs.
- **Nothing reads it after close-out.** Both of its triggers have fired by
  then. A file in a working tree that no process and no person opens again is
  the definition of the noise this whole change set exists to remove.
- **Git still has it.** Deleting moves it out of the working tree and into
  history, which is the right place for a record of how something *used to* be
  planned. `git show <sha>:<path>` brings it back.

**Delete it whether or not anything superseded.** Most work will produce a log
that changed no decision badly enough to need a new record — that is the common
case, and it is exactly the case that would otherwise accumulate forever.

**Only at project completion, never per chunk.** The replan threshold reads
this mid-work; removing it early takes away the one trigger that fires while
the plan can still be fixed.

What this does not carry forward: an entry that changed only the delivery shape
touches no decision, so no ADR takes it. That is accepted. The merged pull
requests already record what shipped and in what order, and six months on
nobody asks why chunk 1 was stacked rather than opened independently.

## What does not go here

| | Where it goes |
|---|---|
| What is true now | The spec's `Dn.` section, or the ADR |
| Chunk status, PR state | Nowhere — the plan comment renders it from the graph |
| A reusable lesson | `memory_store` |
| Work discovered outside the plan | A follow-up task or issue, not a deviation |
| Ordinary implementation detail — names, file layout, how a function is factored | Nowhere. If it would not have changed the approval, it is not a deviation |
