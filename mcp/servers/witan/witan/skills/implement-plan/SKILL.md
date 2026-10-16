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
  Does not choose the approach: that is plan-technical-approach's job.
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

**Then find the ADRs that govern this change.** Zero is normal, not an error:

```bash
base=$(git merge-base origin/main HEAD)
git diff --name-only "$base"...HEAD > /tmp/changed_files

# For each ADR's applies_to entries (`:(glob)` semantics are mandatory --
# without the prefix, `*` crosses `/` and a narrow-looking glob matches a whole
# subtree):
git diff --name-only "$base"...HEAD -- ":(glob)${entry}" | grep -q . \
  && echo "governs this change: ${adr}"
```

**`applies_to` is not the whole set.** That match only sees ADRs carrying the
metadata, which is new — every record written before this convention has none,
including witan's own. Those are skipped in silence, and the empty-glob warning
below cannot catch a *missing* entry the way it catches a wrong one. So list
them explicitly and judge them by hand:

```bash
# Every ADR with no applies_to frontmatter at all.
for adr in <adr-dir>/[0-9]*.md; do
  grep -q '^applies_to:' "$adr" || echo "unscoped: $adr"
done
```

Read each unscoped record's decision and the paths it names in prose, and
decide relevance from those. **Zero ADRs govern this change is a conclusion,
not a default** — it may only be reached once the unscoped ones have been read,
because an inherited decision binds just as hard as a scoped one.

Before a chunk's first edit, run the same match against that chunk's
**Touches** entry in the plan's chunk table — the paths it is expected to
change. An ADR found after the code is written is a rewrite; found before, it
is a constraint. If the chunk table has no `Touches` column (a plan written
before this was recorded), derive the paths from the approach and say you did,
rather than skipping the check.

**Warn on a glob that matches nothing in the repo at all** —
`git ls-files -- ':(glob)<entry>'` empty means the ADR is governing nothing.
Say which ADR, and do not treat its absence from the match as meaningful.

Two causes, and at the *start* of the work you cannot tell them apart: the
glob is wrong, or it names a path this work has not created yet. An ADR
carrying an `<!-- applies_to: ... is created by this work -->` note is
declaring the second, so take it at its word for now.

**Read every governing ADR in full before editing** — its status, its
`## Decision`, its `## Consequences`, **every `## Addendum` or `## Amendment`,
and any record that supersedes it.** A decision you contradict without noticing
is the failure this whole convention exists to prevent.

Decision-plus-consequences is not enough, and this workflow is the reason: the
[revision protocol](../write-adr/references/revision-protocol.md) appends a
dated addendum rather than rewriting `## Decision`, precisely so the original
wording survives. So the original wording is routinely the *stale* half — an
addendum can change what implementation must do while `## Decision` still reads
the way it did when it was written. Take the current state from the whole file:

- `Superseded by ADR-NNNN` → that record governs, not this one. Follow it.
- `Deprecated` → still in force for existing code, not for new.
- `Proposed` → not yet agreed. Build to it, and say it is unaccepted.

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

## Step 3 — Keep the plan comment current

The comment is the work record: chunks, status, PR links, revisions. Update it
at **every phase boundary** and **every chunk boundary** (a PR opened, a PR
merged — record the URL against its chunk), and when the work is blocked. Not
per commit: a record that churns is one nobody reads.

**Which record wins.** Chunk progress lives in three places, so say once which
is authoritative and keep it that way:

| For | Authority |
|-----|-----------|
| Whether a chunk is done | The chunk's **witan task** state |
| Whether its PR is open, queued, merged, needs rebase | **`gh stack view --json`** |
| The narrative — why, in what order, what was traded away | The **plan comment** |

The comment is **derived from** the first two, not maintained in parallel with
them. It exists so a reader without witan can follow the work; the moment it is
treated as a second status record, it is a third thing to keep in sync and the
first to go stale. If it disagrees with task state or `gh stack`, the comment
is wrong — fix the comment.

Pass `--require-existing` on every update after the first, so a transient read
failure cannot open a second comment:

The scripts live in the sibling skill `plan-technical-approach`, which installs
beside this one. Resolve its directory as
[that skill's own "Resolving the scripts"](../plan-technical-approach/SKILL.md#resolving-the-scripts)
describes — from the directory you loaded the skill from, with a fixed probe as
the fallback — rather than assuming `~/.claude/skills`, which is wrong for Pi,
Copilot, OpenCode, Codex and every project-scoped install. Never use a path
relative to the working directory: that is the product repo, where neither
skill lives.

```bash
PTA="$SKILLS/plan-technical-approach/scripts"

# Not just the first update: read before overwriting a comment this session
# did not write, or the Revisions log and merged PR URLs are replaced wholesale.
bash "$PTA/fetch-plan-comment.sh" --repo <owner/repo> --issue <N>

bash "$PTA/upsert-plan-comment.sh" \
  --repo <owner/repo> --issue <N> --require-existing --expect-sha <sha>
```

Floor: no more than one phase may land without an update, so close-out is never
the first movement since approval.

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

Where it gets recorded depends on what changed:

| What changed | Where it goes |
|---|---|
| A **decision** an ADR records | A dated `## Addendum` on that ADR — see [the revision protocol](../write-adr/references/revision-protocol.md) |
| Delivery: a chunk split, reordered, added, dropped | The plan comment's **Revisions** log |
| Something that should have been an ADR and was not | Write one now via [`write-adr`](../write-adr/SKILL.md) |

Land the record change in the **same pull request** as the deviation, so the
reviewer sees the change and its justification together.

**What counts:** a different mechanism, a moved chunk boundary, a new tradeoff
accepted, an acceptance criterion that turns out to be unbuildable as written.
**What does not:** the ordinary detail of writing code — names, file layout,
how a function is factored. If it would not have changed the approval, it is
not a deviation.

**If the whole approach is wrong**, this is not a deviation. Stop and go back
to [`plan-technical-approach`](../plan-technical-approach/SKILL.md) with the
new evidence. A mechanism swap is a new decision, not an adjustment to an old
one.

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

1. Update the comment: tick the chunk, record its PR URL.
2. `task_close(slug="<chunk task>", resolution="<what shipped>")` — this also
   makes anything it blocked eligible in `task_ready`.
3. If the chunk carried an accepted workaround, comment its PR URL on the
   follow-up issue — that diff is what whoever fixes it has to undo. Leave that
   issue **open**; it tracks work that has not happened.
4. **Re-check any forward-looking `applies_to` glob — but only once the chunk
   that creates the path has landed.** An ADR written for this work may have
   named a path that did not exist yet, flagged with an
   `<!-- applies_to: ... is created by this work -->` note:

   ```bash
   status=0
   git ls-files -- ":(glob)$entry" | grep -q . || status=$?
   if [[ "$status" -eq 0 ]]; then
     echo "now governs real files -- drop the note"
   else
     echo "still empty: wrong glob, or a path a LATER chunk creates"
   fi
   ```

   Matching → remove the note, the glob is live.

   **Still empty is not yet a verdict.** All the ADRs land with the first
   chunk, while the paths they govern may be created by the third, so an empty
   match after chunk 1 proves nothing. Resolve it against the plan's chunk
   table: if a **later** chunk's `Touches` covers the path, the note is still
   accurate — leave it and re-check when that chunk merges. Only when the
   chunk that was supposed to create the path has merged and the glob is
   *still* empty is it genuinely wrong, and then it needs the follow-up PR
   below, because by now this chunk's PR has merged.
5. **Accept the ADRs this PR carried.** Acceptance is an event, not a chore.
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

6. **Deliver steps 4 and 5 as a follow-up pull request.** Close-out runs
   *after* the merge, so every edit it makes — a status flipped to `Accepted`,
   a note dropped, a corrected glob, a regenerated index or docs mirror — is a
   change to a tracked file on the default branch with nothing carrying it
   there. Editing the working tree, or pushing to the merged chunk's branch,
   does not update the merged record: the ADR stays `Proposed` on `main` while
   the task and the session close around it.

   So open one small PR per close-out, titled for what it does
   (`docs(adr): accept ADR-0014 after #412`), carrying only the acceptance
   edits. It needs no plan comment and no chunk task — it is bookkeeping the
   merge triggered, not new work. Where the repo has further obligations (this
   one regenerates the docs mirror and a nav entry), they ride along in it.

   Same rule in reverse: do **not** hold a chunk's PR open waiting for its own
   acceptance. The approval that accepts the ADR is the approval that merges
   the PR, so the edit can only ever come afterwards.

**When the last chunk merges:**

1. Final comment update: outcome and every PR link.
2. `task_close` on the epic, or on the single task.
3. `workflow_session_end(session_slug=..., summary=..., files_changed=[...])`.

**Do not call `workflow_project_complete`** unless this was the whole project —
it writes the immutable corpus trace and belongs at the end of the work.

Before finishing, report any ADR that has been `Proposed` for more than 90 days
while the paths it governs have shipped code. One line is enough: these claim
to be undecided and are in production.
