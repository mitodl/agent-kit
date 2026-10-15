# TECHNICAL.md template

The engineering half of a spec. [`write-spec`](../SKILL.md) creates the file
and owns its shape;
[`plan-technical-approach`](../../plan-technical-approach/SKILL.md) fills it,
because the content is the output of that skill's approach process — the
alternatives, the blinded scoring, the tradeoff ledger, the delivery chunks.

For spec'd work **this file is the source of truth**. The issue comment is a
pointer to it. Anything recorded in both will drift, and the issue copy is the
one that goes stale, because this one sits next to the code and moves with it.

## Template

```markdown
# <Title> — technical design

**Issue:** <full GitHub issue URL>
**Product spec:** [PRODUCT.md](PRODUCT.md)
**Status:** draft | approved | in progress | shipped
**Last updated:** <YYYY-MM-DD>

## Current state

<How the system behaves today in the area being changed, with the measured baseline from scoping: where the behavior lives, what calls it, caller counts. Facts, not plans. This is what makes the blast-radius claims below checkable.>

## Approach

<The chosen mechanism, and the criterion that decided it. One or two paragraphs.>

## Alternatives considered

| Approach | Why it lost | Would have won if |
|---|---|---|
| <mechanism> | <the deciding criterion> | <the condition that would flip it> |

<Omit this section when there was genuinely one candidate, and say so in Approach rather than leaving an empty table.>

## Changes

<Only the subsections that apply.>

### Data model
<Migrations, new or changed columns, constraints, backfills. Name the reversibility: does a revert restore the old behavior, or has data moved?>

### API
<New or changed endpoints, serializers, permissions. Breaking or additive — and if breaking, who consumes it.>

### Integration points
<Services, queues, external APIs, feature flags. Anything whose deploy order matters.>

## Delivery

<"Single PR, ~N lines", or the chunk table. Axes and the size budget are in the planning skill's chunking reference.>

| # | Chunk | Axis | Est. LOC | Depends on | PR |
|---|-------|------|----------|------------|-----|
| 1 | <what ships> | <split axis> | ~N | — | |

## Tradeoffs

| Giving up | To get | Revisit when | Decided by | Follow-up |
|---|---|---|---|---|
| <concrete> | <concrete> | <observable signal> | @<handle> | <issue URL, required for an accepted workaround> |

## Risks

- <What could go wrong, and what makes it detectable or recoverable. Not every hypothetical — the ones that change how this is built or rolled out.>

## Deviations

<Empty until the plan changes during implementation. Append, never rewrite — see below.>

| Date | What the plan said | What we did | Why | Approved by | PR |
|------|--------------------|-------------|-----|-------------|-----|
```

## Deviations: the rule

Plans change during implementation. That is normal and not a failure — what
turns it into one is changing the plan silently, so the document and the code
disagree and nobody knows which is wrong.

So when implementation diverges from this file:

1. **Stop and confirm with the engineer first.** Before writing the code that
   deviates, not after. Say what the spec assumed, what you have found, what
   you propose instead, and what it costs. They approve, choose differently, or
   decide the original plan still stands.
2. **Update the body of this file** so the Approach, Changes and Delivery
   sections describe what is actually being built. A reader looking for current
   truth should find it at the top, not have to reconstruct it from a log.
3. **Append a Deviations row.** This is the part that is easy to skip and the
   part that matters in six months: the body says *what*, the row says *why it
   changed*, which is the question someone asks when the design looks arbitrary.
4. **Land all of it in the pull request that deviates** — same PR, so the
   reviewer sees the change and its justification together.

Append to the table; never edit or delete an existing row. A deviation that
was later reversed gets a second row saying so. Rewriting history here defeats
the only purpose the section has.

**What counts as a deviation** — a different mechanism than the one chosen, a
chunk boundary moved, a chunk added or dropped, a new tradeoff accepted, an
acceptance criterion that turns out to be unbuildable as written. **What does
not** — the ordinary detail of writing the code: names, file layout, how a
function is factored. If it would not have changed the approval, it is not a
deviation.

### Worked example

```markdown
## Deviations

| Date | What the plan said | What we did | Why | Approved by | PR |
|------|--------------------|-------------|-----|-------------|-----|
| 2026-10-28 | One chunk: model, service and API together, ~420 lines | Split into two chunks: model + service (~350), then API (~250) | The migration needed a backfill nobody had sized, which made the combined diff ~600 lines and put a reviewable migration behind an unrelated serializer change | @rhysyngsun | #4834 |
| 2026-11-04 | Daily digest assembled by a cron task | Assembled by a Celery beat task on the existing schedule | The repo has no cron infrastructure in this service; adding one for a single job was a larger change than the feature | @rhysyngsun | #4851 |
```

Both rows pass the test: each would have changed the original approval, and
each names the fact that was not known when the plan was written.
