---
name: implement-spec
description: >
  Execute an approved plan or spec, chunk by chunk, keeping the design record
  true as the work evolves. Use this skill when asked to "implement the spec",
  "build this out", "work through the plan", "implement the approved
  approach", "start on chunk 2", or when a plan has been approved and the code
  now has to be written. Validates at every phase boundary, re-measures each
  chunk against the size budget before opening its pull request, and stops to
  get confirmation before deviating from the plan — then records the deviation
  and why in the spec. Closes each chunk's task as it merges. Does not choose
  the approach: that is plan-technical-approach's job.
license: BSD-3-Clause
metadata:
  category: workflow
---

# Implement the Spec

**Build what was agreed, and keep the record true while doing it.** This skill
does not choose approaches — if the plan turns out to be wrong, it stops and
sends you back rather than quietly picking something else.

## Prerequisites

- An approved plan: a spec's `TECHNICAL.md` from
  [`write-spec`](../write-spec/SKILL.md), or the approved plan comment for
  fast-path work. **No approved plan → go to
  [`plan-technical-approach`](../plan-technical-approach/SKILL.md)** rather
  than inventing one and building it.
- The witan `task_*` / `workflow_*` tools, for claiming and closing chunks.
- The plan-comment scripts, which live in the sibling skill
  (`../plan-technical-approach/scripts/`). Both ship in the `universal`
  profile and install side by side; if the sibling is absent, say so rather
  than hand-rolling a comment update that loses the marker and digest.

**This skill carries no implementation guidance.** How to write the code
belongs to the language and domain skills — `drf-api-performance`,
`pulumi-modify-existing`, `uv-python-workflow`, and the rest.

---

## Step 1 — Load the plan and claim the chunk

Read the whole record before writing anything: `TECHNICAL.md` where there is a
spec, the plan comment otherwise. Take from it the chunk order, each chunk's
phases and done-conditions, the estimated sizes, and the tradeoff ledger —
including any accepted workaround, which has obligations in Step 3.

Claim the chunk you are starting, not all of them:
`task_claim(slug="<chunk task>", session_id="<$CLAUDE_SESSION_ID or
$PI_SESSION_ID>")`. Pass `session_id`, never `assignee` — `assignee` replaces
the holder outright, recording a session with no person behind it. A refusal
comes back as `{"claimed": false, "held_by": ...}`: surface it and pick
different work rather than forcing past a live lease.

Work chunks in the recorded order. A stacked chunk cannot start before the one
below it; an independent chunk can, and a dependency on a *deploy* rather than
a merge means waiting for production, not for the merge button.

---

## Step 2 — Build the chunk

Implement its phases. At **every phase boundary**:

1. Run [`validate-before-commit`](../validate-before-commit/SKILL.md) — not
   only at close-out. A phase that ends with failing hooks is not a phase that
   ended.
2. Update the record (Step 4).

---

## Step 3 — Obligations carried from the plan

**An accepted workaround** gets a code comment at the workaround site naming
its follow-up issue, landing **in the same pull request as the workaround** —
not as a tidy-up afterwards. A workaround that merges without it is invisible
to everyone who does not already know it is there. The follow-up issue itself
was opened during planning; this is the link from the code back to it.

**Scope creep** goes back to the engineer. Work discovered outside the approved
plan is not silently absorbed — the default disposition is a follow-up
(`task_create`, or an issue via
[`create-ol-github-issue`](../../process/create-ol-github-issue/SKILL.md)).
Widening a chunk quietly is how a 300-line estimate becomes a 900-line review.

---

## Step 4 — Keep the record current

Update at **every phase boundary** and **every chunk boundary** (a PR opened, a
PR merged — record the URL against its chunk). Also when the work gets blocked.
Not per commit: a record that churns is one nobody reads.

Where to write depends on the route. **Spec'd work: `TECHNICAL.md` is the
source of truth**, and the issue comment stays a pointer plus status.
**Fast-path work: the comment is the plan.** Never maintain both in full —
they drift, and the issue copy is the one that goes stale.

For comment updates, pass `--require-existing` on every update after the first,
so a transient read failure cannot open a second comment:

```bash
../plan-technical-approach/scripts/upsert-plan-comment.sh \
  --repo <owner/repo> --issue <N> --require-existing
```

Floor: no more than one phase may land without an update, so close-out is never
the first movement since approval.

---

## Step 5 — Deviating from the plan

Plans change during implementation. That is normal. What turns it into a
failure is changing the plan silently, so the document and the code disagree
and nobody knows which is wrong.

**Confirm before you build the deviation — not after.** Stop and tell the
engineer:

- what the plan assumed,
- what you have found that contradicts it,
- what you propose instead, and what it costs on the rubric's terms (blast
  radius, reversibility, effort),
- whether it changes the chunk boundaries, the size estimates, or any
  acceptance criterion.

They approve it, choose differently, or decide the original plan still stands.
**Do not write the deviating code first and ask afterwards.** An approval
sought after the work exists is not an approval; it is a request to ratify.

Once approved:

1. **Update the body** of `TECHNICAL.md` so Approach, Changes and Delivery
   describe what is actually being built. Current truth belongs at the top.
2. **Append a Deviations row** — date, what the plan said, what you did, why,
   who approved, which PR. Append; never edit or delete an existing row. A
   deviation later reversed gets a second row saying so.
3. **Land both in the pull request that deviates**, so the reviewer sees the
   change and its justification together.

The body says *what*; the row says *why it changed*, which is the question
someone asks in six months when the design looks arbitrary. The full rule and a
worked example are in
[`write-spec`'s technical template](../write-spec/references/technical-template.md).

**What counts:** a different mechanism, a chunk boundary moved, a chunk added
or dropped, a new tradeoff accepted, an acceptance criterion that turns out to
be unbuildable as written. **What does not:** the ordinary detail of writing
code — names, file layout, how a function is factored. If it would not have
changed the approval, it is not a deviation.

**If the whole approach is wrong**, this is not a deviation — stop and go back
to [`plan-technical-approach`](../plan-technical-approach/SKILL.md) with the
new evidence. What ships has to be what was agreed, and a mechanism swap is a
new decision, not an adjustment to an old one.

---

## Step 6 — Re-measure, then open the PR

Planning sizes were estimates. Measure the real thing before opening each
chunk's pull request:

```bash
git diff --shortstat origin/<base>...HEAD -- ':/' \
  ':(top,exclude)*.lock' ':(top,exclude)*-lock.*'
```

A chunk estimated at 400 and passing 900 is a signal to stop and re-chunk on an
axis from
[`chunking-the-work.md`](../plan-technical-approach/references/chunking-the-work.md),
not a number to exceed quietly. Re-chunking is a deviation: Step 5 applies, and
past the hard limit with no axis that works it needs the same bypass approval
planning would have asked for.

Then hand to
[`create-ol-pull-request`](../../process/create-ol-pull-request/SKILL.md). Two
things to give it:

- **The chunk's base branch, explicitly.** A stacked chunk targets the previous
  chunk's branch; that skill defaults to the repo's default branch.
- **Goals from the issue and `PRODUCT.md`** — not from the plan comment or
  `TECHNICAL.md`. Those were written by the authoring session, which is exactly
  what that skill's independent review excludes.

The first chunk's PR also carries the spec, which is how `specs/` lands:
reviewed alongside the code it describes, not as a separate gating PR.

**This skill never pushes without going through that skill**, and there is
deliberately no extra review pass here — that one owns it.

---

## Step 7 — Close out

**Per chunk, as it merges:**

1. Update the record: tick the chunk, record its PR URL.
2. `task_close(slug="<chunk task>", resolution="<what shipped>")` — this also
   makes anything it blocked eligible in `task_ready`.
3. If the chunk carried an accepted workaround, comment its PR URL on the
   follow-up issue: that diff is what whoever fixes it has to undo. Leave the
   follow-up issue **open** — it tracks work that has not happened.

**When the last chunk merges:**

1. Final record update: outcome, every PR link, `Status: shipped` in the spec.
2. `task_close` on the epic, or on the single task.
3. `workflow_session_end(session_slug=..., summary=..., files_changed=[...])` —
   what was done, what remains, decisions taken.

**Do not call `workflow_project_complete`** unless this was the whole project.
It writes the immutable corpus trace and belongs at the end of the work, not
the end of a session.

The spec stays in the repo. It is a record of why, which outlives the work —
not scaffolding to delete once the code is merged.
