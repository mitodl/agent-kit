---
name: write-spec
description: >
  Write the design document for a body of work that spans several pull
  requests, sessions, or repositories, in the location and shape that
  repository already uses. Use this skill when scoping has routed work to the
  spec tier — interlocking decisions, open questions blocking the design, a
  sequence across PRs or repos — or when asked to "write a spec", "write the
  design doc", "write this up as a plan", or "turn the discovery into a spec".
  Detects the repo's existing spec directory rather than imposing one, pins
  source references to a commit, numbers the decisions so they can be argued
  with individually, links each phase to its witan task, and extracts settled
  decisions into ADRs as they stop moving. Planning only: it writes no code.
license: BSD-3-Clause
metadata:
  category: workflow
---

# Write a Spec

A spec is the plan for **one body of work**: several interlocking decisions, a
sequence across pull requests, and the open questions still blocking the
design. It changes while the work is in flight and becomes history when the
work ships.

**It is not a long ADR.** The difference is lifetime, and it decides which one
you are writing:

| | Spec | ADR |
|---|---|---|
| Records | One body of work | One decision |
| Lives | Until the work ships, then as history | As long as the code it governs |
| Changes | Continuously, while in flight | By dated addendum, rarely |
| Answers | "What are we building, in what order, and what is still open?" | "Why is it like this?" |
| Read by | The person building it, now | Someone two years from now |

Work on the spec route produces **both**: the spec first, then an ADR per
decision that settles. Writing the ADRs up front gets it backwards — a decision
still moving is a spec section, and an ADR over it would be amended before the
work shipped.

| File | Purpose |
|------|---------|
| [`references/spec-template.md`](references/spec-template.md) | The header block, the section shape, and what each section owes the reader |
| [`references/repo-locations.md`](references/repo-locations.md) | Finding the repo's spec directory and matching the convention it already uses |

## Entry conditions

Entered from [`plan-technical-approach`](../plan-technical-approach/SKILL.md),
**after** its alternatives have been generated and scored, on work
[`scope-work`](../scope-work/SKILL.md) routed to the spec tier. That skill
supplies the chosen approach, the rejected ones with the condition that would
flip each, the chunk boundaries, and the measured baseline.

Invoked without those, go there first. A spec written before the scoring is a
preference with headings, and one written before the code has been read is
fiction with headings.

**Check the tier was right.** A spec is for work that spans several pull
requests *and* more than one session or repo, or that carries open questions
blocking the design. One decision over one or two PRs is a plan comment plus an
ADR — writing a spec for it produces a document nobody needs to read and a
directory that teaches the next person specs here are ceremonial.

## Asking the user

Steps 1 and 5 ask the user to choose or confirm something. Ask the same way on
every agent — the same contract the other skills in this bundle use, because
`witan setup` installs all of them into agents that have no such tool:

- **If a structured question tool is actually available to you** (e.g. Claude
  Code's `AskUserQuestion`, or a question tool a Pi extension registers), use
  it with the question and options given. Where a step also needs free text,
  use whatever free-text entry that tool offers; if it offers none, ask for the
  text in a plain message.
- **Otherwise**, ask the same question in a normal message — number the
  options, say what free-text answer is accepted — and **end your turn and
  wait** for the reply. Do not guess an answer or pick a default on the user's
  behalf.

Never add an option the step does not list just to get a free-text box.

---

## Step 1 — Find the directory and match the convention

Full procedure in
[`references/repo-locations.md`](references/repo-locations.md). In short:

```bash
git ls-files \
  | grep -iE '(^|/)docs/(plans|specs|design)/|(^|/)docs/internals/design/' \
  | sed 's|/[^/]*$||' | sort -u
```

`docs/plans/`, `docs/specs/`, `docs/design/` and `docs/internals/design/` are
all in use across the org's repos. **Follow the one you find**, and read two or
three of its newest files before writing — the conventions below vary by
directory, and the directory wins over anything here.

Nothing matches → ask where it should go rather than creating a fifth
convention. If the answer is "there is none", `docs/design/` in a repo that
keeps documentation under `docs/`, otherwise `docs/design/` at the root.

---

## Step 2 — Write the header block

Every spec opens with a short prose block before the first section. It is the
load-bearing part for anyone arriving later:

```markdown
# <subject> — Spec

Status: **spec** (design/track only — nothing here is implemented yet)
Project: `wp-<the WorkflowProject slug>`
Basis: [`<discovery-or-spike-doc>.md`](./<doc>.md), and the issue it came from

All source anchors below are against `origin/main` @ `<short sha>`.
```

Three of those earn their place:

- **`Project:`** is the `wp-` slug. It is what connects the document to the
  tasks, sessions and memories doing the work, and it is how someone reading
  the graph finds the design.
- **`Status:`** says whether this is still being argued with or is
  implementation-ready. A spec read as settled when it is not gets built.
- **The commit anchor** is the one most often skipped and most often missed. A
  spec quotes file paths and line numbers; without a sha they rot silently, and
  an implementer six weeks later cannot tell whether the spec is wrong or the
  code moved. Pin it, and say so.

---

## Step 3 — Number the decisions

**Each decision gets its own number and its own heading** — `D1`, `D2`, … —
with its state in the heading:

```markdown
## D4. Deploy-time apply: entrypoint direct apply only (decided)

<The decision. Then the alternative that lost, and the condition that would
have flipped it.>
```

This is the single most useful convention in the corpus, because it makes a
decision **addressable**. A reviewer disagrees with D4, not with "the spec". A
witan task can be "Decide D1–D7". An ADR extracted later says which D it came
from. A spec whose decisions are buried in prose gets argued with as a whole,
which means it gets argued with slowly.

Mark state explicitly — `(decided)`, `(open)`, `(deferred to P2)`. An
undecided decision presented in the same voice as a settled one is how a spec
ships a guess.

**Record the condition that would flip each rejected alternative**, as
`write-adr` does: "rejected because X" is a verdict, "would have won if Y" is
reviewable, and on a spec Y often *does* arrive mid-flight.

---

## Step 4 — Sequence it, and link the tasks

A spec that does not say what order the work happens in is a design document,
not a plan. Give the phases, their done-conditions, and **the witan task for
each** — 30 of the 43 specs across these repos reference `tk-` slugs inline,
and that is what makes the document navigable from the backlog and the backlog
navigable from the document.

```markdown
## Sequencing

1. **Shared read layer** — `tk-witan-ui-shared-read-layer-...`
   Done when: every view reads through it and no view queries the store.
2. **Gantt view** — `tk-witan-ui-gantt-...` (blocked by 1)
```

Tasks already created by `plan-technical-approach` are referenced, not
recreated: the spec ratifies that DAG rather than forking a second one. Where
the spec changes the shape of the work, update the tasks and say so here.

Also carry, each in its own section:

- **Verification** — how anyone knows it worked, per phase. Checkable
  conditions, not "tests pass".
- **Non-goals / out of scope** — the things a reader will otherwise assume are
  coming. This section prevents more scope creep than any other.
- **Open questions** — with who can answer each. An open question with no owner
  is a wish.
- **Risks** and what is done about them, where the work can fail in a way the
  sequencing cannot absorb.

---

## Step 5 — Review it as a file, not a comment

The spec is a **reviewed file in a pull request**. That is the point of it
being a file: at this size a comment kept consistent by hand is rebuilding a
versioned document the hard way, and what readers need — what changed, when,
and why — is what a diff and a review already do well.

So: it lands in its own pull request, ahead of the implementation, and the
chunk PRs reference it. Say in the PR description which decisions are still
open, so a reviewer knows which parts they are being asked to settle rather
than check.

Confirm with the engineer before opening it, the way
[Asking the user](#asking-the-user) describes — a spec that reaches review with
D3 silently decided is worse than one that asks.

Then advance the project to `spec`, which on this route means exactly **the
spec exists and is under review**.

---

## Keeping it true, and retiring it

**While the work is in flight**, the spec is the thing that changes — that is
the difference from an ADR. A decision that moves gets its section rewritten
and its state updated, in the same pull request as the code that moved it, the
way [`implement-plan`](../implement-plan/SKILL.md) records any other deviation.
There is no addendum protocol here; append-only is an ADR rule and applying it
to a spec produces a document that reads as a changelog.

**As each decision settles**, extract it into an ADR via
[`write-adr`](../write-adr/SKILL.md) — but only the ones that will **outlive
the project**. Most will not: a chunk boundary, a phase order, a migration
sequence all stop mattering once the work ships. The test is `write-adr`'s own:
would someone two years from now, reading the code, ask why? Link the ADR from
the D-section it came from, and leave that section in place.

**When the work ships**, the spec becomes history. Some repos move it — this
one keeps shipped specs in `docs/internals/design/`. Do not delete it: the ADRs
extracted from it carry the decisions, but only the spec carries the sequence
and the things that were considered and dropped.
