---
name: complete-project
description: >
  Close out a piece of work once its last pull request has merged: supersede
  the ADRs its deviations changed, consume and delete the deviation log, accept
  the records still Proposed, close the epic and the session, and seal the
  corpus trace. Use this skill when asked to "close this out", "wrap it up",
  "the last PR merged", "finish the project", or when implementation is done
  and the record has to be made true. Also covers work that ends without
  shipping. Requires the witan MCP server. Does not write implementation code:
  that is implement-plan's job, and it has already finished.
license: BSD-3-Clause
metadata:
  category: workflow
---

# Complete the Project

**One output: a record that matches the code, and a graph that says the work
is over.** Everything here runs *after* a merge, against the default branch,
and carries no feature work.

This is not bookkeeping. Accepted ADRs are immutable and a changed decision is
superseded **only at close-out** — so this is the single moment the decision
record is made to match what was built. Skip it and every ADR the work
deviated from stays confidently wrong on `main`, while the deviation log that
recorded the truth is deleted or orphaned.

It runs standalone. Work that `implement-plan` never drove still ends, and
still needs this.

| File | Purpose |
|------|---------|
| [`references/bookkeeping-pr.md`](references/bookkeeping-pr.md) | Why post-merge edits need their own pull request, and what rides in it |

## Prerequisites

- The witan `task_*` / `workflow_*` tools. This skill ships in witan's bundle
  and `witan setup` installs both together. Missing → **say so and stop**:
  closing a task graph by hand leaves it inconsistent in ways nobody notices.
- **The last pull request merged.** Not approved, not queued — merged. Close-out
  edits files on the default branch; running it early produces a bookkeeping PR
  against code that has not landed.
- **A linked session.** This skill starts cold more often than any other — the
  work finished days ago, possibly in someone else's session — and Step 5 ends
  one. Nothing can end a session this run never started. No linked session in
  the injected context → link one following
  [`witan-workflow`](../witan-workflow/SKILL.md) first.

**Everything here is re-runnable.** Close-out spans a merge, so it is routinely
interrupted: the bookkeeping PR is open when the session dies. Each step below
checks state before acting, and the trace call is idempotent by construction.
Re-running after a crash is the expected path, not a recovery.

---

## Step 1 — Establish what is ending

Three different things get confused here, and the wrong answer seals an
immutable record early.

```text
task_get(slug="<the epic or task>")      # children, state, external_uri
workflow_project_get(slug="<project>")   # phase, linked sessions
```

- **A chunk** merged → not this skill. That is `implement-plan` Step 6's
  per-chunk pass.
- **The epic** is done — every child task closed — but the project has more
  epics coming → close the epic and the session. **Do not seal the trace.**
- **The project** is done → everything below, including the trace.

**When the epic is the only epic, these are the same event** and the
distinction is invisible from the task graph alone. Ask rather than infer: a
`WorkflowTrace` is immutable and sealing it early permanently understates the
project.

Confirm what shipped against live state rather than the plan — the plan is what
was intended:

```bash
gh pr list --repo <owner/repo> --search "<epic reference>" --state merged
```

## Asking the user

Step 1 asks the user to choose. Ask the same way on every agent:

- **If a structured question tool is actually available to you** (e.g. Claude
  Code's `AskUserQuestion`, or a question tool a Pi extension registers), use
  it with the header, question and options given. Where free text is also
  needed, use whatever free-text entry that tool offers; if it offers none, ask
  in a plain message.
- **Otherwise**, ask the same question in a normal message — number the
  options, say what free-text answer is accepted — and **end your turn and
  wait** for the reply. Do not guess an answer or pick a default on the user's
  behalf.

Never add an option the step does not list just to get a free-text box. Nothing
that changes the graph happens until the user has answered.

---

## Step 2 — Make the decision record true

**Read the deviation log end to end**, at
`docs/specs/<slug>-deviations.md` — named in the spec's header block. No log
means no deviations, which is a normal outcome and not a reason to go looking.

For **every ADR the entries touched**, write **one** superseding record via
[`write-adr`](../write-adr/SKILL.md), covering everything that moved:

- One record per *decision*, not per deviation. A decision the work hit four
  times gets a single ADR stating what it finally became, not a chain of four.
- One deviation is enough. There is no threshold here, because there is no
  later trigger to wait for — see
  [when a superseding record gets written](../write-adr/references/revision-protocol.md#when-a-superseding-record-gets-written).
- Take the *why* from the entries. That is the part a reader comes back for,
  and it exists nowhere else.

**Then delete the log**, in the same pull request, with its sha cited in the
record first:

```bash
git rev-parse --short HEAD        # goes in the ADR, BEFORE the removal
git rm docs/specs/<slug>-deviations.md
```

**Delete it even when nothing superseded.** Work whose deviations changed no
decision is the common case and exactly the case that would otherwise
accumulate a file per project forever. Full reasoning in
[the deviation log](../plan-technical-approach/references/deviation-log.md#then-delete-it).

Already deleted, from an interrupted run? Then this step is done — check the
superseding records exist and move on.

---

## Step 3 — Sweep what the chunks left behind

Three things the per-chunk pass routinely cannot finish, because each needs
work that had not landed yet:

**Forward-looking `applies_to` globs.** An ADR may name a path created by a
later chunk, flagged with an
`<!-- applies_to: ... is created by this work -->` note. Every chunk has now
merged, so an empty match is finally a verdict:

```bash
git ls-files -- ":(glob)$entry" | grep -q . \
  && echo "live -- drop the note" \
  || echo "WRONG GLOB -- the work is over and it governs nothing"
```

**ADRs still `Proposed`.** Each gets the acceptance pass — the reviewer query
and the three ways the obvious version gets it wrong are in
[the revision protocol](../write-adr/references/revision-protocol.md). Do not
re-derive it; this instruction had its own copy once and the two drifted. No
approving human review → it stays `Proposed` and **say so out loud** rather
than leaving it to be found.

**Accepted workarounds.** Each should have a follow-up issue carrying the PR
URL whose diff has to be undone. Leave those issues **open** — they track work
that has not happened — and report any that is missing one.

---

## Step 4 — The bookkeeping pull request

Every edit above changes a tracked file on the default branch with nothing
carrying it there. One PR carries them all:
[`references/bookkeeping-pr.md`](references/bookkeeping-pr.md) — what rides in
it, the repo's own obligations, and why the final acceptance flip terminates
rather than regressing forever.

---

## Step 5 — Close the graph

In this order, because each depends on the one before:

1. `task_close` every child task still open, then the epic.
2. `workflow_session_end` — this session, with what it changed.
3. **The project, only if Step 1 said so** — `workflow_project_complete`.

**The calls themselves belong to
[`witan-project-tracker`](../witan-project-tracker/SKILL.md)**, which documents
their arguments and what the trace aggregates. Follow the link rather than
reconstructing them here; a second copy of those rules is how this bundle has
drifted before.

What this skill adds is **when** the completion call is legitimate: every chunk
merged, the record true, the log gone. The outcome narrative is the primary
content of a permanent corpus record, so write it as one — what shipped, what
was traded away, what is still open.

---

## Step 6 — Report

One short message, not a document:

- What shipped, with the merged PR URLs.
- **Any ADR left `Proposed`**, and why.
- **Any ADR `Proposed` for more than 90 days** while the paths it governs have
  shipped code. These claim to be undecided and are in production.
- Anything deferred: open follow-up issues, workarounds still in place.

---

## When the work ends without shipping

Cancelled, superseded, or overtaken. The mess is the same as a successful
close-out and nobody comes back for it, so run it anyway:

| | Abandoned work |
|---|---|
| The deviation log | **Delete it.** It records changes to a plan that is not happening |
| ADRs still `Proposed` | **Withdraw, do not supersede.** Set `Superseded`/`Rejected` per the repo's flavor with a line saying the work was abandoned. Superseding implies a replacement decision; there is none |
| ADRs already `Accepted` whose work was abandoned | Leave them. They were agreed; a superseding record is only needed if the abandonment *changes* the decision |
| Chunk tasks and the epic | `task_close` with a resolution saying abandoned and why — not deleted. The graph should show the work was considered |
| Follow-up issues for workarounds | Close them **only if** the workaround was also reverted. A workaround still in production still needs its fix |
| The session | `workflow_session_end` as normal |
| The project | `workflow_project_update(status="abandoned")` — **not** `workflow_project_complete`, which is the only route to a corpus trace and would record delivery that did not happen |

Say plainly in the report that the work was abandoned and what, if anything,
of it is still running in production.
