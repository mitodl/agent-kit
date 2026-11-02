# Complexity triage

How [`scope-work`](../SKILL.md) decides how much process a piece of work
deserves. Four tiers, assessed **after** grounding in the code and never from
the issue title — a "simple UI tweak" that turns out to touch a shared
component is not simple, and only the grounding tells you which one you have.

**Two questions, not one.** *How much deciding does this need* and *how much
of it will outlive the work* are different, and the artifacts answer different
ones:

| Artifact | Lifetime | Holds |
|---|---|---|
| Plan comment | The work | Chunks, phases, status, the tradeoff ledger |
| **Spec** | The work | Several interlocking decisions, open questions, a sequence across PRs, sessions or repos. Changes in flight; becomes history when done |
| **ADR** | As long as the code | **One** decision, scoped with `applies_to`, amended rather than rewritten, read by someone two years later asking why |
| RFC | Until it is decided | A question that needs input from outside the team before anyone can choose |

A spec is not a long ADR and an ADR is not a small spec. Decisions inside a
spec that will outlive the project get **extracted into their own ADRs** once
they settle, and the spec links to them.

## Tier 1 — Trivial

**All of these must hold.** One fails and it is not trivial:

- One mechanism is obvious and you cannot name a second worth weighing.
- Contained: a handful of call sites, no shared contract (schema, env var,
  endpoint, package export), no data migration, no deployable boundary crossed.
- Tens of lines, not hundreds.
- A plain `git revert` undoes it.
- Not in auth, permissions, payments, PII, or anywhere failure is silent.
- No user-facing behavior change anyone would want to agree on first. Changing
  a label's wording is trivial; changing what the button does is not.
- The engineer has not asked for options.

Typical: a copy or color fix, an off-by-one, a missing null guard, a version
bump with no API change, adding a log line.

**Route:** [`plan-technical-approach`](../../plan-technical-approach/SKILL.md),
fast path. No ADR.

## Tier 2 — Has a decision worth recording

Any one of these is enough:

- More than one mechanism is worth weighing.
- It changes a contract two sides agree on — schema, API shape, env var, a
  published package export.
- It needs a data migration or a backfill.
- It crosses a deployable boundary (backend and frontend, service and infra,
  two repos).
- It changes user-facing behavior someone should agree on before it is built.
- It touches auth, permissions, payments or PII at all.
- It involves pulling in new dependencies

**Route:** [`plan-technical-approach`](../../plan-technical-approach/SKILL.md),
full path — alternatives generated and scored first, then
[`write-adr`](../../write-adr/SKILL.md) records what was decided. The order
matters: an ADR's `## Options Considered` is the *output* of scoring, so a
record written before the scoring documents a preference rather than a
decision.

This tier means the work contains at least one decision a future reader will
ask *why* about. It may produce one ADR, or several — one per decision.

**"Needs several pull requests" is deliberately not on that list.** How work is
split is a delivery question, owned by
[`chunking-the-work.md`](../../plan-technical-approach/references/chunking-the-work.md),
and a chunk boundary is not a decision anyone will come back to ask about. Size
alone routes nowhere; what it does is make Tier 3 worth checking.

## Tier 3 — Needs a spec

The work is one *body* of work rather than one decision. Any of:

- It spans **several pull requests across more than one session**, so the plan
  has to survive being picked up by someone who was not there.
- It spans **more than one repo or deployable**, where the sequence between
  them is itself the hard part.
- It carries **open questions that block the design** — not unknowns that need
  a spike, but questions whose answers the team has to converge on while the
  work is already moving.
- It contains **several decisions that interlock**, where choosing one
  constrains the next, and recording them separately would lose the ordering.

**Route:** [`plan-technical-approach`](../../plan-technical-approach/SKILL.md),
full path, then [`write-spec`](../../write-spec/SKILL.md) writes the spec into
the repo's existing location — `docs/plans/`, `docs/specs/`, `docs/design/` or
`docs/internals/design/`, whichever that repo already uses. It is a reviewed
file in a pull request, not an issue comment: at the size these reach, a comment
kept consistent by hand with a digest is rebuilding a versioned file the hard
way.

Each decision in it that will outlive the project is extracted into its own
[ADR](../../write-adr/SKILL.md) **once it settles**, and linked from the spec.
Not up front: a decision still moving is a spec section, and an ADR written
over it would be amended before the work shipped.

This is the tier the team already works in — there are 11 specs in
ol-infrastructure's `docs/plans/`, 8 across ol-data-platform's `docs/specs/`
and `docs/design/`, and 14 in agent-kit's `docs/internals/design/`. They share a
shape: problem, numbered decisions, measured facts, phases, sequence, open
questions.

## Tier 4 — Unknowable

Not "large" — **unsizable**. A load-bearing fact is missing, and without it any
estimate and any design is a guess dressed up as a plan:

- The shape of the real data is unknown and the approach depends on it.
- Whether a third-party API or library supports the thing at all is unverified.
- Where the latency or the failure actually comes from has not been measured.
- Whether the obvious approach performs acceptably at production scale is
  untested.

The tell is that you cannot write down the options without inventing a fact.

**Route:** spike first — see below.

## Off to the side — needs a decision from outside the team

Not a tier, because it is not about size. When the choice needs agreement from
people who are not going to read an ADR — another team owns the contract, or
the call is as much product as engineering — the artifact is an **RFC**, via
`create-ol-rfc-discussion`,
and an ADR records the outcome once it is settled. Routing that to an ADR
first records a decision the team was not entitled to make alone.

---

## When in doubt, it is not trivial

Running the fast path on a complex change ships an unexamined design decision,
which is the thing this whole workflow exists to prevent. Running the full
process on a simple one costs a scoring pass and a short plan comment. The
asymmetry is deliberate: classify conservatively.

**The full path is not free, and its cost has not been measured.** A scoring
pass, an ADR and a plan comment is real work, and on the full path it runs
before any code is written. Treat "it is cheaper than being wrong" as the
argument — it does not need a number, and an invented one ("ten minutes")
invites being quoted back when the true figure turns out to be larger.

Two specific traps:

- **"This looks easy"** is the feeling that precedes most of the changes that
  were not. It is a prompt to check the call sites, not a verdict.
- **A workaround is never trivial.** Accepting one means there is a real fix
  you chose not to do, which is a second mechanism by definition — so it fails
  the first criterion, and it needs the tradeoff ledger and follow-up issue
  that only the full path produces.

Classification is revocable. If work routed as trivial turns out to have a
second mechanism, a shared contract, or a size estimate that has doubled, stop
and re-route. Re-routing late is cheap; an unexamined design decision is not.

---

## The spike protocol

A spike is a time-boxed investigation that answers **one specific question**.
It is not "look into it" — a spike with no question has no finish line and
becomes an open-ended research project.

Write it down as a task, matching the `tk-spike-*` convention already in the
backlog:

```text
task_create(
    type="task",
    title="Spike: <the question, as a question>",
    description="<why the answer is load-bearing, what is blocked without it,
                 the time box, and the artifact that ends it>",
    priority="<inherit from the work it unblocks>",
    external_uri="<the originating issue URL>",
)
```

Four things the task must carry:

1. **The question**, specific enough to be answered yes or no, or with a
   number. "Can omnigraph's `load(merge)` handle a 40k-node batch in under 10s?"
   — not "investigate omnigraph performance".
2. **Why it is load-bearing** — what decision is blocked until it is answered.
3. **A time box.** Hours or days, stated up front. A spike that overruns its
   box is itself a finding: report it rather than continuing silently.
4. **The artifact** that ends it — a measurement, a working throwaway script, a
   paragraph with evidence. Spike code is evidence, not a deliverable; it is
   not the start of the implementation.

**After the spike, re-enter [`scope-work`](../SKILL.md) at Step 3**, not at the
tier you assumed before. The findings change the grounding the triage rests on,
and all three outcomes are live:

- The answer made it straightforward → **trivial**, take the fast path, and do
  not write an ADR nobody needs.
- The answer settled the unknown but the work is still substantial → **needs a
  decision worth recording**, now with a fact in it instead of a hole.
- The answer revealed the premise was wrong → the off-ramp. Report it and stop;
  a spike that kills a bad plan has paid for itself.
