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

**2. Close-out.** Before `workflow_project_complete`, read the whole file. Any
ADR that took **two or more** addenda from this work, or whose `## Decision`
these entries have made false, gets a superseding ADR via
[`write-adr`](../../write-adr/SKILL.md), taking its *why* from here. See
[the accumulation trigger](../../write-adr/references/revision-protocol.md#when-to-supersede-instead).

Then the file is done. It stays in git as the shipped spec's sibling and is not
maintained further.

## What does not go here

| | Where it goes |
|---|---|
| What is true now | The spec's `Dn.` section, or the ADR |
| Chunk status, PR state | Nowhere — the plan comment renders it from the graph |
| A reusable lesson | `memory_store` |
| Work discovered outside the plan | A follow-up task or issue, not a deviation |
| Ordinary implementation detail — names, file layout, how a function is factored | Nowhere. If it would not have changed the approval, it is not a deviation |
