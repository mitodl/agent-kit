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
- The plan-comment scripts, which live in the sibling skill
  `plan-technical-approach`. Both ship in the `universal` profile and install
  side by side — under `~/.claude/skills/` once installed, under
  `skills/workflow/` in this repo. If the sibling is absent, say so rather than
  hand-rolling a comment update that loses the marker and digest.

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

Before a chunk's first edit, run the same match against the files the chunk
*will* touch, from the plan. An ADR found after the code is written is a
rewrite; found before, it is a constraint.

**Warn on a glob that matches nothing in the repo at all** —
`git ls-files -- ':(glob)<entry>'` empty means the ADR has gone stale and is
silently governing nothing. Say which ADR, and do not treat its absence from
the match as meaningful.

Read every governing ADR's `## Decision` and `## Consequences` before editing.
A decision you contradict without noticing is the failure this whole convention
exists to prevent.

Claim the chunk you are starting, not all of them:
`task_claim(slug="<chunk task>", session_id="<$CLAUDE_SESSION_ID or
$PI_SESSION_ID>")`. Pass `session_id`, never `assignee` — `assignee` replaces
the holder outright, recording a session with no person behind it. A refusal
comes back as `{"claimed": false, "held_by": ...}`: surface it and pick
different work rather than forcing past a live lease.

Work chunks in the recorded order. A stacked chunk cannot start before the one
below it; a dependency on a *deploy* rather than a merge means waiting for
production, not for the merge button.

---

## Step 2 — Build the chunk

Implement its phases. At **every phase boundary**:

1. Run [`validate-before-commit`](../validate-before-commit/SKILL.md) — not
   only at close-out. A phase that ends with failing hooks has not ended.
2. Update the plan comment (Step 4).

**An accepted workaround** gets a code comment at the workaround site naming
its follow-up issue, landing **in the same pull request as the workaround** —
not as a tidy-up afterwards. A workaround that merges without it is invisible
to everyone who does not already know it is there.

**Scope creep** goes back to the engineer. Work discovered outside the approved
plan is not silently absorbed; the default disposition is a follow-up
(`task_create`, or an issue via
[`create-ol-github-issue`](../../process/create-ol-github-issue/SKILL.md)).

---

## Step 3 — Keep the plan comment current

The comment is the work record: chunks, status, PR links, revisions. Update it
at **every phase boundary** and **every chunk boundary** (a PR opened, a PR
merged — record the URL against its chunk), and when the work is blocked. Not
per commit: a record that churns is one nobody reads.

Pass `--require-existing` on every update after the first, so a transient read
failure cannot open a second comment:

The scripts live in the sibling skill, and the two skills install side by side
— in this repo under `skills/workflow/`, once installed under
`~/.claude/skills/`. Anchor on whichever root applies rather than using a
relative path, which resolves under neither when the working directory is the
product repo:

```bash
SKILLS=~/.claude/skills    # in-repo development: ./skills/workflow
PTA="$SKILLS/plan-technical-approach/scripts"

# Not just the first update: read before overwriting a comment this session
# did not write, or the Revisions log and merged PR URLs are replaced wholesale.
"$PTA"/fetch-plan-comment.sh --repo <owner/repo> --issue <N>

"$PTA"/upsert-plan-comment.sh \
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
[`create-ol-pull-request`](../../process/create-ol-pull-request/SKILL.md), with
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
4. **Accept the ADRs this PR carried.** Acceptance is an event, not a chore:

   ```bash
   gh pr view <N> --repo <owner/repo> --json reviews \
     --jq '[.reviews[] | select(.state == "APPROVED") | .author.login] | unique | join(", ")'
   ```

   A human approver → flip the status to `Accepted` and record who approved
   it, **in whichever form that ADR's flavor uses**: an ol-infrastructure ADR
   gets a Review History row and a bumped `**Last Updated:**`; a witan ADR has
   neither, so the approver goes on the `- Status:` line
   (`Accepted 2026-10-14 by @handle`). No approving review → it stays
   `Proposed`, and say so rather than leaving it to be discovered. Full rule in
   [the revision protocol](../write-adr/references/revision-protocol.md).

**When the last chunk merges:**

1. Final comment update: outcome and every PR link.
2. `task_close` on the epic, or on the single task.
3. `workflow_session_end(session_slug=..., summary=..., files_changed=[...])`.

**Do not call `workflow_project_complete`** unless this was the whole project —
it writes the immutable corpus trace and belongs at the end of the work.

Before finishing, report any ADR that has been `Proposed` for more than 90 days
while the paths it governs have shipped code. One line is enough: these claim
to be undecided and are in production.
