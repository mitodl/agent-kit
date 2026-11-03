---
name: implement-plan
description: >
  Execute an approved plan chunk by chunk, honouring the architecture decisions
  that govern the files being changed. Use this skill when asked to "implement
  the plan", "build this out", "work through it", "start on chunk 2", "stack
  these PRs", or when an approach has been approved and the code now has to be
  written. Opens stacked pull requests with the gh stack extension. Reads the
  plan comment on the issue plus any ADRs whose applies_to globs match the
  changed files, validates at every phase boundary, re-measures each chunk
  against the size budget before opening its pull request, and stops to get
  confirmation before deviating — then records the deviation where it belongs.
  Does not choose the approach: that is plan-technical-approach's job, and
  does not close the work out: that is complete-project's.
license: BSD-3-Clause
metadata:
  category: workflow
---

# Implement the Plan

**Build what was agreed, and keep the record true while doing it.** This skill
does not choose approaches. If the plan turns out to be wrong it stops and
sends you back rather than quietly picking something else.

The name follows the input that is always present. A trivial change has **zero**
ADRs and a plan comment; a large one has several ADRs and a plan comment. The
plan comment is the constant.

## Prerequisites

- **An approved plan** — the marker-tagged comment on the issue, written by
  [`plan-technical-approach`](../plan-technical-approach/SKILL.md). No approved
  plan → go there first rather than inventing one and building it.
- The witan `task_*` / `workflow_*` tools, for claiming and closing chunks.
  This skill ships in witan's own bundle and `witan setup` installs both
  together. If they are missing anyway, **say so and stop** — do not proceed
  with the claiming skipped.
  An unclaimed task being actively worked is indistinguishable from an idle
  one, which is the collision the claim exists to prevent, and it is worse
  during implementation than during scoping because there is now code at
  stake.
- The plan-comment scripts, which live in the sibling skill
  `plan-technical-approach`. Both ship in witan's skill bundle and install side
  by side, so the sibling is in the parent of the directory you loaded this
  skill from — resolve it that way rather than assuming an install root, since
  the five supported agents use five different ones. If the sibling really is
  absent, say so rather than hand-rolling a comment update that loses the
  marker and digest.

**On entry, make sure this session is linked.** This is the skill most likely
to start in a session of its own — chunk 3 gets picked up days after chunk 1,
often by someone else — and Step 6 ends a session with
`workflow_session_end(session_slug=...)`. Nothing can end a session this run
never started. So if the injected session context shows no linked session, link
one following [`witan-workflow`](../witan-workflow/SKILL.md) and keep the
`session_slug`; without it this chunk's work is attributed to no project and
the close-out call has nothing to close.

**This skill carries no implementation guidance.** How to write the code
belongs to the language and domain skills — `drf-api-performance`,
`pulumi-modify-existing`, `uv-python-workflow`, and the rest.

---

## Step 1 — Load the plan and the governing decisions

Read the plan comment in full: the chunk order, each chunk's phases and
done-conditions, the estimated sizes, and the tradeoff ledger including any
accepted workaround.

**Where the plan comment links a spec, the spec is the design** — the comment
carries status and links, not the thinking. Read it before the first edit, and
read it as a *current* document rather than a settled one: its `Dn.` sections
carry their own state, and one marked `(open)` is a decision nobody has made
yet. Building past an open `Dn.` is the same mistake as contradicting an ADR,
except the record says outright that it is unresolved. Take it back to
[`plan-technical-approach`](../plan-technical-approach/SKILL.md) instead.

Check its commit anchor too. A spec pins its file and line references to a sha;
if `origin/main` has moved a long way since, the paths it quotes may have moved
with it, and the mismatch is the spec being stale rather than the code being
wrong.

**Then find the ADRs that govern this change.** Zero is normal, not an error.
`adr-scope.sh` ships in the sibling skill `write-adr`; resolve `$SKILLS` as
[Step 3](#step-3--regenerate-the-plan-comment) describes:

```bash
ADR="$SKILLS/write-adr/scripts"

# Before a chunk's FIRST EDIT: match the chunk's declared Touches paths.
bash "$ADR/adr-scope.sh" --dir <adr-dir> --paths "<the chunk's Touches globs>"

# Once there is a diff, and again before opening the PR:
bash "$ADR/adr-scope.sh" --dir <adr-dir> \
  --changed "$(git merge-base origin/main HEAD)"
```

**Run the `--paths` form before the first edit, not after.** An ADR found after
the code is written is a rewrite; found before, it is a constraint. The paths
come from the chunk task's `Touches` line, recorded by
`plan-technical-approach`. If the task carries none — a plan made before this
was recorded — derive them from the approach and say you did, rather than
skipping the check.

The script applies `:(glob)` itself, which is what the inlined version kept
getting wrong: without the prefix `*` crosses `/`, so `src/*.py` silently
matches a whole subtree instead of nothing.

**Read its output in full, not just the `governs this change:` lines.** Two
other things it reports are the ones that matter most:

- **`UNSCOPED`** — `applies_to` is new, and **no witan ADR carries it today**,
  so "no ADR governs this change" is what a correct run routinely prints while
  inherited decisions bind just as hard. Read each unscoped record's decision
  and the paths it names in prose, and judge relevance from those. **Zero ADRs
  govern this change is a conclusion, not a default.**
- **`WARN` / `Note:`** — a glob matching nothing. At the *start* of the work
  you cannot tell a wrong glob from a path this work has not created yet, so an
  ADR carrying an `<!-- applies_to: ... is created by this work -->` note is
  reported separately and taken at its word until close-out.

Exit 2 means a glob governs nothing with nothing saying it should. That is
worth stopping for; it is not a reason to proceed as though no decision
applied.

**Read every governing ADR in full before editing** — its status, its
`## Decision`, its `## Consequences`, **every `## Addendum` or `## Amendment`,
and any record that supersedes it.** A decision you contradict without noticing
is the failure this whole convention exists to prevent.

Decision-plus-consequences is not enough, because many existing records were
written under the old protocol, which appended a dated addendum rather than
rewriting `## Decision`. On those the original wording is routinely the *stale*
half — an addendum can change what implementation must do while `## Decision`
still reads the way it did when it was written. ol-infrastructure ADR-0009 and
witan ADRs 0001, 0003, 0004 and 0011 all carry them.

New revisions no longer work that way: an accepted ADR is immutable and a
changed decision gets a superseding record instead. But that forbids *writing*
addenda, not reading the ones already there. Take the current state from the
whole file:

- `Superseded by ADR-NNNN` → that record governs, not this one. Follow it.
- `Deprecated` → still in force for existing code, not for new.
- `Proposed` → not yet agreed. Build to it, and say it is unaccepted.

**Then read the deviation log, and read it as part of the ADRs.** This is not
optional and it is not background: superseding records are written **only at
close-out**, so for the whole life of the project an ADR this work has already
deviated from still reads the way it did before. The log is where current truth
lives in that window.

So a governing ADR named in the log has been **overruled in that respect** by
an approved deviation, and the entry — not the ADR — is what binds. Chunk 3
building to ADR-0014 as written, three weeks after chunk 1 deviated from it, is
the exact failure this ordering prevents, and it is likelier than contradicting
an ADR outright because the record looks authoritative and current.

```bash
cat <spec-dir>/<slug>-deviations.md    # named in the spec's header block
```

No log, or no entry naming an ADR you are governed by, and the ADR stands as
written.

**Claim the chunk you are starting, not all of them.** Claiming the whole stack
up front hides from everyone else that only one of them is live.

How to claim — which id to pass, why never `assignee`, what a refusal looks
like, and the requirement to read the task's existing comments first — is in
[`witan-task`](../witan-task/SKILL.md). Those comments matter most here: a
previous session's **checkpoint** is how you find out that this chunk's branch
is already pushed, or its PR already open, before you redo either.

Work chunks in the recorded order. A stacked chunk cannot start before the one
below it; a dependency on a *deploy* rather than a merge means waiting for
production, not for the merge button.

---

## Step 2 — Build the chunk

Implement its phases. At **every phase boundary**:

1. Run `validate-before-commit` — not
   only at close-out. A phase that ends with failing hooks has not ended.
2. Update the plan comment (Step 4).

**Leave a checkpoint at each durable step** — a branch created, a commit, a
push, a PR opened — so a run cut off mid-chunk can be resumed instead of
reconstructed. The step names, the one-line format and the fetch-first resume
procedure are in
[`witan-task`](../witan-task/SKILL.md) under "Checkpoint as you go" and
"Resuming from a checkpoint". **Follow the link; do not copy the format or the
git commands here.** Both changed once already during that skill's own review —
a fetch was added and the branch-list pattern corrected — and a copy would
already be wrong.

**An accepted workaround** gets a code comment at the workaround site naming
its follow-up issue, landing **in the same pull request as the workaround** —
not as a tidy-up afterwards. A workaround that merges without it is invisible
to everyone who does not already know it is there.

**Scope creep** goes back to the engineer. Work discovered outside the approved
plan is not silently absorbed; the default disposition is a follow-up
(`task_create`, or an issue via
`create-ol-github-issue`).

---

## Step 3 — Regenerate the plan comment

The comment is **rendered from the work graph, not written**. Regenerate it at
every phase boundary and every chunk boundary (a PR opened, a PR merged), and
when the work is blocked. Not per commit: a record that churns is one nobody
reads.

There is nothing to keep in sync, because nothing in it is maintained
separately:

| For | Authority |
|-----|-----------|
| Whether a chunk is done | The chunk's **witan task** state |
| Whether its PR is open, approved, merged, failing checks | The chunk task's **`external_uri`**, resolved live |
| What is blocking | The **`blocks` links** in the graph |
| The narrative — why, what was traded away | The **spec and the ADRs** |
| How the plan moved | [The deviation log](../plan-technical-approach/references/deviation-log.md) |

So the way to fix a wrong comment is to fix the graph and re-render. Editing
the comment to disagree with the graph just moves the error somewhere nobody
will look again.

The scripts live in the sibling skill `plan-technical-approach`, which installs
beside this one. Resolve its directory as
[that skill's own "Resolving the scripts"](../plan-technical-approach/SKILL.md#resolving-the-scripts)
describes — from the directory you loaded the skill from, with a fixed probe as
the fallback — rather than assuming `~/.claude/skills`, which is wrong for Pi,
Copilot, OpenCode, Codex and every project-scoped install. Never use a path
relative to the working directory: that is the product repo, where neither
skill lives.

**If `$SKILLS` comes back empty, stop and say so.** Every command below starts
`"$SKILLS/..."`, so an empty one quietly becomes an absolute path at the
filesystem root and fails with a "no such file" about a path nobody wrote.
Never run one with an empty prefix.

```bash
PTA="$SKILLS/plan-technical-approach/scripts"

bash "$PTA/upsert-plan-comment.sh" \
  --repo <owner/repo> --issue <N> --require-existing \
  --render --epic <tk-epic-slug> --spec <spec URL> \
  --decisions "<ADR markdown links>" --note "<judgment, or omit>"
```

`--require-existing` on every update after the first, so a transient read
failure cannot open a second comment. No `fetch-plan-comment.sh` read-back and
no `--expect-sha`: those guard hand-written prose against another session, and
a rendered body is rebuilt from the graph every time.

**Two refusals to expect, both deliberate.** The script exits non-zero rather
than posting when the epic has no child tasks, and when the witan read fails —
a comment whose chunk table is empty reads as *this work has no chunks*, and
posting it would overwrite a correct one with that claim.

`--note` is the one authored line, capped at 280 characters. Use it for
judgment the graph cannot imply — *"the bottleneck is review, not work"* — and
omit it when there is none. Anything longer is reasoning: it belongs in the
spec.

Floor: no more than one phase may land without a regeneration, so close-out is
never the first movement since approval.

---

## Step 4 — Deviating

Plans change during implementation. That is normal. What turns it into a
failure is changing one silently, so the record and the code disagree and
nobody knows which is wrong.

**Confirm before you build the deviation — not after.** Tell the engineer what
the plan assumed, what you found that contradicts it, what you propose instead
and what it costs, and whether it changes the chunk boundaries, the size
estimates, or any acceptance criterion. They approve, choose differently, or
decide the original stands. **Do not write the deviating code first and ask
afterwards** — an approval sought after the work exists is not an approval, it
is a request to ratify.

**Every deviation gets one entry in
[the deviation log](../plan-technical-approach/references/deviation-log.md)** —
six fields, 800 characters hard, and `Touches:` naming `chunks` when the entry
moved the delivery shape — **written when the engineer approves, before the
deviating code exists.** That is what makes it a record of what was agreed
rather than a note about what was done, and it is the only artifact the
approval leaves. It is also where a superseding ADR gets its *why*.

**Write nothing to the ADR itself — not now, not at the second deviation.** An
accepted ADR is immutable, and its superseding record is written **only at
close-out** (Step 6), once the decision has stopped moving. Superseding mid-work
chains records: Tuesday's supersession is itself superseded on Thursday, and a
decision that moved four times leaves four ADRs with one live.

Until then the log entry **is** the record, which is why Step 1 reads the log
as part of the ADRs.

Then, depending on what changed:

| What changed | Where it goes |
|---|---|
| A **decision** an ADR records | Nothing on the ADR — it is immutable. The log entry is the record until close-out, which **supersedes** it — see [the revision protocol](../write-adr/references/revision-protocol.md#when-a-superseding-record-gets-written) |
| A **decision a spec records**, where one exists | Rewrite that `Dn.` section and update its state, in the same PR. Specs change in flight; they get no addendum protocol — see [`write-spec`](../write-spec/SKILL.md) |
| Delivery: a chunk split, reordered, added, dropped | Change the **chunk tasks** — the graph is the delivery shape — then re-render (Step 3) |
| Something that should have been an ADR and was not | Write one now via [`write-adr`](../write-adr/SKILL.md) |

Land the log entry and the record change in the **same pull request** as the
deviation, so the reviewer sees the change and its justification together.

**What counts:** a different mechanism, a moved chunk boundary, a new tradeoff
accepted, an acceptance criterion that turns out to be unbuildable as written.
**What does not:** the ordinary detail of writing code — names, file layout,
how a function is factored. If it would not have changed the approval, it is
not a deviation.

### When to replan instead of repairing again

Repairing a plan one deviation at a time is right while the divergence is
small. Past a point it is not: you are patching a plan you no longer have, and
the patches cost more than rewriting while producing something worse. Repair
buys **stability** — it preserves the commitments already attached to the plan,
the open PRs and the claimed tasks — and that value decays as divergence grows.

So stop repairing and go back to
[`plan-technical-approach`](../plan-technical-approach/SKILL.md) — which
rewrites the plan, reading the deviation log to do it — when **any** of:

- the deviation log reaches a **third** entry that changes the chunk graph's
  shape (a chunk split, added, reordered or dropped), **or**
- a decision the plan rests on is reopened, or turns out to be false, **or**
- the mechanism named as the approach is itself what is in question.

The third is the clearest: **a mechanism swap is a new decision, not an
adjustment to an old one.** But the first two get missed, because each
individual change looks small. So do not judge the feel of it — count, with the
script, after every entry you write:

```bash
bash "$PTA/check-deviation-log.sh" <spec-dir>/<slug>-deviations.md \
  --since "$(git merge-base origin/main HEAD)"
```

**Exit 3 is the threshold**, and it is a distinct status so it cannot be read
past: stop repairing and go back to
[`plan-technical-approach`](../plan-technical-approach/SKILL.md). Exit 1 means
the log itself is malformed — a missing field, an entry over the 800-character
cap, or lines removed from a file that is append-only.

The count comes from entries whose `Touches:` names `chunks`, so write that
token when an entry moves the delivery shape. An entry that does not name it
does not count toward the threshold, and a plan can drift three chunk changes
deep while the log says nothing.

Three is where it was calibrated. Reconstructing
[mitodl/hq#14080](https://github.com/mitodl/hq/issues/14080)'s history as
deviation entries gives six, of which three changed the chunk graph's shape;
the third landed five days in, with three further repairs after it and the
plan by then describing a different delivery from the one that was approved.

---

## Step 5 — Re-measure, then open the PR

Planning sizes were estimates. Measure before opening each chunk's PR:

```bash
git diff --shortstat origin/<base>...HEAD -- ':/' \
  ':(top,exclude)*.lock' ':(top,exclude)*-lock.*'
```

A chunk estimated at 400 and passing 900 is a signal to stop and re-chunk on an
axis from
[`chunking-the-work.md`](../plan-technical-approach/references/chunking-the-work.md),
not a number to exceed quietly. Re-chunking is a deviation: Step 4 applies, and
past the hard limit with no axis that works it needs the same bypass approval
planning would have asked for.

Then hand to
`create-ol-pull-request`, with
two things:

- **The chunk's base branch, explicitly.** A stacked chunk targets the previous
  chunk's branch; that skill defaults to the repo's default branch. With
  `gh stack` driving the topology (below), the base is the branch one layer
  down in the stack.
- **Goals from the issue body**, never from the plan comment. That skill's
  independent review takes goals only from sources the authoring session did
  not write, and the plan comment is exactly that session's output. Acceptance
  criteria live in the issue body for this reason.

**As soon as the PR exists, link it to the chunk task:**

```text
task_update(slug="<this chunk's task>", external_uri="<the PR URL>")
```

This is not bookkeeping — it is what makes the PR column render. The comment
resolves each chunk's live state (open, approved, changes requested, checks
failing, merged) from this field, so a chunk whose PR is unlinked shows as
*not started* while it sits in review.

The first chunk's PR also carries any ADRs written for this work — that is how
they land, reviewed alongside the code they describe.

**This skill never pushes without going through that skill**, and there is
deliberately no extra review pass here — that one owns it.

### Stacked chunks: use `gh stack`

When the plan has more than one chunk and they are **stacked** (each builds on
the one below), manage the chain with the `gh stack` extension rather than
hand-maintaining base branches and rebases. Independent chunks need none of
this — they target the default branch and `gh stack` does not apply.

Check it is there first; it is an extension, not part of `gh`:

```bash
gh extension list | grep -q 'github/gh-stack' || gh extension install github/gh-stack
```

If it cannot be installed, fall back to the manual form — one branch per chunk,
each created from the previous one, each PR opened with
`--base <previous-chunk-branch>`, and `git rebase` by hand when a lower chunk
changes. The stack still ships; it is just more work and easier to get wrong.

**Create the chain as chunks begin**, bottom first:

```bash
gh stack init <chunk-1-branch>          # based on the default branch
gh stack add  <chunk-2-branch>          # based on chunk 1
gh stack view --short                   # confirm the shape before pushing
```

`gh stack init` adopts branches that already exist and creates those that do
not, so a chunk branched by hand earlier is absorbed rather than duplicated.

**Do not create the PRs with `gh stack submit`.** Three reasons, all from its
own documented behavior:

- It opens a single-screen interactive editor by default, which an agent cannot
  drive.
- `--auto` skips that editor but uses **auto-generated titles**, discarding the
  org template, the claim audit and the independent review that
  `create-ol-pull-request` exists to apply.
- With `--auto`, new PRs are created **as drafts unless `--open` is passed** — a
  quiet way to open a stack nobody is asked to review.

So: open each chunk's PR through `create-ol-pull-request` as above, then link
them into a stack on GitHub:

```bash
gh stack sync
```

`sync` links the stack's open PRs into a GitHub stack object once two or more
exist, which is what makes this split possible.

**One case where it stops instead.** If the local and remote stacks have
diverged — someone added a PR to the stack on GitHub — `sync` prompts in an
interactive terminal, and **in a non-interactive one it aborts without pushing
branches or updating PRs**. That is a silent no-op from an agent's point of
view, so check the exit status and re-read `gh stack view --short` rather than
assuming the link happened. A clean "remote is ahead" update applies without
prompting; only a true divergence stops.

**`gh stack sync` is also how a lower chunk's changes reach the ones above it.**
It fetches, fast-forwards the trunk, cascade-rebases each branch onto its
updated parent, and pushes every branch atomically with `--force-with-lease`.
Run it after review feedback lands on any chunk but the top one, or the chunks
above are reviewing against a base that no longer exists.

Two cautions, both because that push rewrites remote history:

- **Never run it on a stack whose branches someone else may be committing to.**
  `--force-with-lease` protects against overwriting commits you have not seen,
  but a colleague mid-push still loses time.
- **On a rebase conflict, sync restores every branch to its original state** and
  stops, advising `gh stack rebase` to resolve interactively. That is a human's
  job — surface it rather than attempting the resolution blind.

`gh stack view --json` reports the chain and each PR's state (merged, queued,
open, needs-rebase); use it to tell what has landed rather than inferring from
the plan comment.

The extension is at v0.0.8. Treat its output as authoritative over anything
written here, and if a command's behavior differs from this description, follow
the tool and say so.

---

## Step 6 — Close out

**Per chunk, as it merges:**

1. `task_close(slug="<chunk task>", resolution="<what shipped>")` — this also
   makes anything it blocked eligible in `task_ready`. Then re-render (Step 3);
   the row follows from the task and its PR, so there is nothing to tick.
2. If the chunk carried an accepted workaround, comment its PR URL on the
   follow-up issue — that diff is what whoever fixes it has to undo. Leave that
   issue **open**; it tracks work that has not happened.
3. **Re-check any forward-looking `applies_to` glob — but only once the chunk
   that creates the path has landed.** An ADR written for this work may have
   named a path that did not exist yet, flagged with an
   `<!-- applies_to: ... is created by this work -->` note:

   ```bash
   bash "$ADR/adr-scope.sh" --dir <adr-dir> --sweep
   ```

   A `LIVE:` line → remove the note, the glob is live.

   **A `WRONG GLOB:` line here is not yet a verdict**, and this is the one
   place the script cannot decide for you. `--sweep` assumes the work is over;
   mid-stack it is not. All the ADRs land with the first chunk while the paths
   they govern may be created by the third, so an empty match after chunk 1
   proves nothing. Resolve it against the chunk tasks: if a **later** chunk's
   `Touches` covers the path, the note is still accurate — leave it and
   re-check when that chunk merges. Only when the chunk that was supposed to
   create the path has merged and the glob is *still* empty is it genuinely
   wrong, and then it needs the follow-up PR below, because by now this
   chunk's PR has merged.

   `complete-project` runs the same sweep once every chunk has landed, which
   is the point at which its verdict is one.
4. **Accept the ADRs this PR carried.** Acceptance is an event, not a chore.
   Query each reviewer's latest verdict with bots excluded — the exact command
   and the three ways the obvious version gets it wrong are in
   [the revision protocol](../write-adr/references/revision-protocol.md).
   Do not re-derive it here; this skill had its own copy and the two drifted.

   A human approver → flip the status to `Accepted` and record who approved
   it, **in whichever form that ADR's flavor uses**: an ol-infrastructure ADR
   gets a Review History row and a bumped `**Last Updated:**`; a witan ADR has
   neither, so the approver goes on the `- Status:` line
   (`Accepted 2026-10-14 by @handle`). No approving review → it stays
   `Proposed`, and say so rather than leaving it to be discovered.

5. **Deliver steps 3 and 4 as a follow-up pull request.** Close-out runs
   *after* the merge, so every edit it makes is to a tracked file already on
   the default branch, with nothing carrying it there. Editing the working
   tree does not update it, and neither does pushing to the merged chunk's
   branch — that branch is spent.

   One small PR per close-out, titled for what it does
   (`docs(adr): accept ADR-0014 after #412`). What rides in it, the repo's own
   regeneration obligations, and why it does not regress forever are in
   [the bookkeeping pull request](../complete-project/references/bookkeeping-pr.md).

   Same rule in reverse: do **not** hold a chunk's PR open waiting for its own
   acceptance. The approval that accepts the ADR is the approval that merges
   the PR, so the edit can only ever come afterwards.

**When the last chunk merges, hand off to
[`complete-project`](../complete-project/SKILL.md)** rather than continuing
here. That is a different job in a different mode — it edits the default
branch, carries no feature work, and routinely runs days later in a session
that never built anything.

Give it four things, so it does not re-derive them:

- the **epic slug**, or the single task's;
- the **spec directory**, and the deviation log path inside it;
- the **ADRs this work wrote**, and which chunk's PR carried each;
- whether this epic **is** the project, or one slice of a longer one.

It owns what is left: superseding the ADRs the deviation log touched, deleting
the log, accepting what is still `Proposed`, the bookkeeping pull request,
closing the epic and the session, and sealing the corpus trace. It also covers
work that ends without shipping, which this skill has no path for.

**Do not call `workflow_project_complete` here.** It seals an immutable corpus
trace, and whether this work *is* the whole project is the first question
`complete-project` asks.
