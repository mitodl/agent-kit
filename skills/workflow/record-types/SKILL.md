---
name: record-types
description: >
  Decide which kind of design record you are looking at, and what that means
  you may do to it. Use this skill when the answer turns on whether something
  is an ADR, a spec, an RFC, a plan comment or a deviation log — "what is the
  difference between an ADR and a spec", "where does this decision get
  recorded", "should this be an ADR or an RFC", "can I edit this RFC", "may I
  change this ADR", "which record governs this file", "this document says
  Status: spec but it is in docs/rfcs" — and before acting on any rule that is
  keyed on one of those words. Also covers a decision recorded somewhere none
  of the conventions put it.
---

# Record types

Four artifacts, one word each, and the words collide. A rule keyed on the
word inherits every ambiguity in it, so resolve the artifact first.

**Two questions, not one.** *How much deciding does this need* and *how much
of it will outlive the work* are different, and the artifacts answer
different ones:

| Artifact | Lifetime | Holds |
|---|---|---|
| Plan comment | The work | Chunks, phases, status, the tradeoff ledger |
| **Spec** | The work | Several interlocking decisions, open questions, a sequence across PRs, sessions or repos. Changes in flight; becomes history when done |
| **ADR** | As long as the code | **One** decision, scoped with `applies_to`, immutable once accepted — a changed decision is superseded, never edited — read by someone two years later asking why |
| RFC | Until it is decided | A question that needs input from outside the team before anyone can choose |
| Deviation log | Transient | How the plan moved and on whose say-so. Consumed and deleted at close-out |

A spec is not a long ADR and an ADR is not a small spec. Decisions inside a
spec that will outlive the project get **extracted into their own ADRs** once
they settle, and the spec links to them.

An RFC is a **question**, not a record. It produces an ADR once it is
answered. A document that already records answers is doing ADR's job
whatever it is called or wherever it lives.

## Read the artifact, not the label

The word in the filename, the directory, and any `Status:` line inside are
three independent claims and they disagree in real repositories. Rank them:

1. **What the document does.** Answers recorded = a record. A question
   awaiting an answer = an RFC. A sequence of chunks and open questions for
   one body of work = a spec.
2. **Where it lives.** `docs/adr/` and `docs/specs/` are the conventions this
   bundle writes to. Anything else is a *foreign record* — see below.
3. **Any `Status:` field.** Least reliable. A status value naming a tier
   (`spec`, `adr`, `rfc`) is not evidence of the tier: agents have written
   those into templates that have no status field at all, after which the
   string reads back as though it were repo vocabulary. Never resolve the
   artifact from it alone.

## Foreign records

A decision recorded outside `docs/adr/` and `docs/specs/` — an RFC tree used
for decisions, a design doc, a wiki page — is a **foreign record**. Several
repositories keep decisions in `docs/rfcs/`, and the word there means the
record, not the question.

Treat a foreign record as an ADR: read it, cross-reference it, **change
nothing**. New decisions go to `docs/adr/`. The rules that follow from this —
when a record may be edited and from which branch, and where the movement
goes instead — are in `write-adr`'s revision protocol.

## `spec` has a fourth meaning

In witan's project graph, `spec` is one of four **phases** a `WorkflowProject`
moves through: `discovery` → `spec` → `implementation` → `delivery`. It is a
schema enum about where a project is, unrelated to the artifact in the table
above. A project in its `spec` phase need not produce a spec.

## Related

- `write-adr` — recording one decision, and the revision protocol governing
  when an existing record may change
- `write-spec` — the design document for multi-PR work, and where it goes
- `create-ol-rfc-discussion` — posting the question when the call is not the
  team's to make alone
