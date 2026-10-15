# Complexity triage

How [`scope-work`](../SKILL.md) decides how much process a piece of work
deserves. Three tiers, assessed **after** grounding in the code and never from
the issue title — a "simple UI tweak" that turns out to touch a shared
component is not simple, and only the grounding tells you which one you have.

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
- It is large enough to need splitting into several pull requests.
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

## Tier 3 — Unknowable

Not "large" — **unsizable**. A load-bearing fact is missing, and without it any
estimate and any design is a guess dressed up as a plan:

- The shape of the real data is unknown and the approach depends on it.
- Whether a third-party API or library supports the thing at all is unverified.
- Where the latency or the failure actually comes from has not been measured.
- Whether the obvious approach performs acceptably at production scale is
  untested.

The tell is that you cannot write down the options without inventing a fact.

**Route:** spike first — see below.

---

## When in doubt, it is not trivial

Running the full process on a simple change costs ten minutes. Running the fast
path on a complex one ships an unexamined design decision, which is the thing
this whole workflow exists to prevent. The asymmetry is deliberate: classify
conservatively.

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
