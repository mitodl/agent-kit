---
name: write-spec
description: >
  Write a durable design record for work that warrants one, as PRODUCT.md and
  TECHNICAL.md under specs/<slug>/ in the repo where the code lives. Use this
  skill when scoping has shown the work is not trivial — multiple approaches
  worth weighing, a schema or API contract change, a migration, a
  frontend/backend split, or user-facing behavior someone should agree on
  before it is built — or when asked to "write a spec", "spec this out",
  "document the design", or "what's the plan for <feature>". Writes the
  product half itself and hands the technical half to plan-technical-approach.
  The spec supersedes the issue comment as the source of truth and is committed
  with the first implementation pull request.
license: BSD-3-Clause
metadata:
  category: workflow
---

# Write a Spec

A spec exists so that a decision made once is still legible a year later, to
someone who was not in the room. Two files, because they answer different
questions for different readers:

| File | Question | Reader |
|------|----------|--------|
| `PRODUCT.md` | What should happen, for whom, and how do we know it worked? | Anyone — product, design, support, a future engineer |
| `TECHNICAL.md` | How are we building it, and what did we trade away? | Engineers, now and later |

| File | Purpose |
|------|---------|
| [`references/product-template.md`](references/product-template.md) | `PRODUCT.md` template with a worked example |
| [`references/technical-template.md`](references/technical-template.md) | `TECHNICAL.md` template — filled by the planning skill |

## Entry conditions

This skill expects a scoped issue and a measured baseline from
[`scope-work`](../scope-work/SKILL.md). Invoked without them, go there first —
a spec written before the code has been read is a guess with headings.

Invoked on work that scoping called **trivial**, stop and say so. A spec for a
copy change is overhead nobody reads, and writing one teaches the next reader
that specs here are ceremonial.

---

## Step 1 — Place the spec

```text
specs/<short-kebab-slug>/
├── PRODUCT.md
└── TECHNICAL.md
```

- **Which repo:** the one where the code lives, never the tracker. For
  cross-repo work, the repo owning the majority of the change; chunks elsewhere
  link to it rather than copying it, so there is one canonical copy to update.
- **The slug** describes the work in a few kebab-case words —
  `saved-search-notifications`, not `12184` or `feature-a`. Issue numbers
  collide across tracker and product repos, and a number tells a browsing
  reader nothing.
- **Both files open with the issue URL** on a header line. The slug will not
  tell a future reader which ticket this was, and the issue is where the
  discussion lives.

**If `specs/` does not exist in this repo yet**, create it with a short
`README.md` saying what the directory is for and linking this skill. The first
spec in a repo otherwise reads as a stray directory someone forgot to clean up.

In this repository specifically, note that `docs/internals/design/*-spec.md`
already exists. `specs/` is the forward convention; leave those where they are.

---

## Step 2 — Write `PRODUCT.md`

Write it **before** any technical design. It is what the technical approach is
then answerable to — reversing the order produces a product document
rationalizing a design that was already chosen.

Template and worked example in
[`references/product-template.md`](references/product-template.md). The
sections: problem, who it affects, user stories, acceptance criteria,
explicitly out of scope, and open questions.

Two rules that carry the weight:

- **Acceptance criteria are checkable by someone who did not build it.** "Users
  can filter by date" is a feature name. "A learner selecting a date range sees
  only courses starting in it, and an empty range shows the zero state rather
  than all courses" is a criterion.
- **Out of scope is not optional.** Most spec disputes are about what someone
  assumed was included. Writing down three things this does *not* do costs a
  line each and prevents the argument.

**Get it approved before Step 3.** This is the point where a product
disagreement is cheap. After the technical design exists, changing the product
answer invalidates work.

---

## Step 3 — Hand the technical half to the planning skill

Create `TECHNICAL.md` from
[`references/technical-template.md`](references/technical-template.md), then
hand off to
[`plan-technical-approach`](../plan-technical-approach/SKILL.md), which runs
the approach process and fills it in: current state, the approaches considered
and why one won, the tradeoff ledger, the delivery chunks, and the risks.

The division is deliberate. This skill owns the document — where it lives, what
shape it has, that it stays current. That skill owns the *decision* — eliciting
the engineer's approach first, generating alternatives, scoring them blind to
authorship. Keeping those apart is what stops a spec from being a write-up of
the first idea anyone had.

---

## Step 4 — Point the issue at it

The spec is now the source of truth. The plan comment on the issue shrinks to a
pointer, current status, and the Revisions log — the approach, the tradeoffs
and the chunk table live in `TECHNICAL.md` only.

Duplicating them means two copies that drift, and the issue copy is the one
that will be stale, because the spec sits next to the code and gets updated in
the same pull request. The short form is documented in
[`plan-technical-approach`'s comment format](../plan-technical-approach/references/plan-comment-format.md).

---

## Step 5 — Commit it with the first implementation PR

The spec is not its own gating pull request. It lands in the first PR that also
carries code, where it is reviewed alongside the thing it describes.

That is a deliberate trade: the spec gets no independent review round, in
exchange for not blocking all implementation behind a documentation review. If
a particular spec genuinely warrants review before any code is written — a
contentious product decision, a migration with no safe rollback — say so and
open it on its own. That is a judgment call, not the default.

---

## Keeping it true

A spec that describes what was *planned* while the code does something else is
worse than no spec: it is confidently wrong, and it is believed.

So `TECHNICAL.md` is updated in the same pull request that diverges from it —
not afterwards, not in a cleanup pass.
[`implement-spec`](../implement-spec/SKILL.md) owns that discipline during
implementation. When the work ships, the spec stays: it is a record of why,
which outlives the work, not a scaffold to delete.
