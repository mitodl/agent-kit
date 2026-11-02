---
name: plan-technical-approach
description: >
  Decide how a piece of work will be built, and get it approved, before any
  code is written. Use this skill when asked "how should we approach this",
  "what's the best way to do X", "plan this out", or after scoping has
  established what needs doing. Elicits the engineer's own approach before
  proposing any alternative, generates alternatives independently, scores every
  candidate against a fixed rubric — tech-debt delta, complexity,
  architectural fitness, blast radius, effort, reversibility — in a context
  blind to who proposed what, documents the tradeoffs of any short-term
  workaround and opens a follow-up issue for the real fix, then decides whether
  the work ships as one pull request or several along service and functional
  lines under a 500-line soft and 1,000-line hard budget. Planning only: it
  writes no code.
license: BSD-3-Clause
metadata:
  category: workflow
---

# Plan the Technical Approach

**Planning only. This skill writes no code** — it ends when an approach is
approved and recorded, which is what
[`implement-plan`](../implement-plan/SKILL.md) then reads.

| File | Purpose |
|------|---------|
| [`references/ranking-rubric.md`](references/ranking-rubric.md) | Six criteria, the tradeoff ledger, the workaround follow-up issue |
| [`references/analyst-brief.md`](references/analyst-brief.md) | The verbatim brief handed to the scoring context |
| [`references/chunking-the-work.md`](references/chunking-the-work.md) | One PR or several: size budget, split axes, stacked vs independent |
| [`references/plan-comment-format.md`](references/plan-comment-format.md) | The issue comment, which is the work record, and its marker contract |
| `scripts/fetch-plan-comment.sh` | Read-only: find the plan comment on an issue |
| `scripts/upsert-plan-comment.sh` | Create or update it in place |

## Prerequisites

- The `task_*` and `workflow_*` tools, from the witan MCP server (`witan
  setup`). Missing → say so and stop.
- A scoped issue and a measured baseline from
  [`scope-work`](../scope-work/SKILL.md). Without them, go there first:
  alternatives invented without reading the code are fiction, and the rubric
  has nothing to score against.

**On entry, make sure this session is linked.** Do not assume `scope-work` ran
in it: planning often starts in a fresh session the morning after scoping, and
this skill's Step 6 writes to the graph. If the injected session context shows
no linked session, link one now following
[`witan-workflow`](../witan-workflow/SKILL.md), and **keep the returned
`session_slug`** — it is the thread tying this session's later `memory_store`
calls and `workflow_session_end` together. Already linked, nothing to do.

## Entry modes

| Entry | Behavior |
|---|---|
| **Fast path** — scoping called the work trivial | Skip Steps 1–3. One obvious approach, short plan comment, no ledger, no ADR. Go to Step 4. |
| **Full path** — scoping found a decision worth recording | All steps. The chosen approach and its alternatives feed the ADR(s) written by [`write-adr`](../write-adr/SKILL.md); delivery stays in the plan comment. |
| **Spec path** — scoping found work spanning several PRs, sessions or repos | All steps, and the plan lands as a **file in the repo** rather than only a comment. See [Step 6](#step-6--record-the-approved-plan). |

Scoring comes first, then the record: run Steps 1–4, then hand the chosen
approach and the rejected alternatives to [`write-adr`](../write-adr/SKILL.md),
which turns them into `## Decision` and `## Options Considered`. An ADR written
before the alternatives were scored records a preference, not a decision.

## Asking the user

Steps 1 and 5 ask the user to choose or type something, and Step 5's answer is
the approval the whole skill waits on. Ask the same way on every agent — the
same contract `witan-task` and `witan-workflow` use, because `witan setup`
installs all of them into agents that have no such tool:

- **If a structured question tool is actually available to you** (e.g. Claude
  Code's `AskUserQuestion`, or a question tool a Pi extension registers), use
  it with the question and options given. Where a step also needs free text,
  use whatever free-text entry that tool offers; if it offers none, ask for the
  text in a plain message.
- **Otherwise**, ask the same question in a normal message — number the
  options, say what free-text answer is accepted — and **end your turn and
  wait** for the reply. Do not guess an answer or pick a default on the user's
  behalf.

Never add an option the step does not list just to get a free-text box. Nothing
is recorded, no issue is opened and no code is written until the user has
answered: on this skill the cost of improvising is an approach approved by
nobody.

---

## Step 1 — Ask the engineer first

Ask how **they** would solve it, before proposing anything. The order is the
whole point: an agent that leads with its own design turns the engineer into a
reviewer of that design instead of the source of a second one.

You may state **facts** about the code ("this lives in X; Y already does half
of it") and must not state a **direction** ("so we should…"). A code tour that
ends in a recommendation anchors exactly as hard as a proposal.

Sort the answer before it reaches Step 3 — a constraint fed in as a candidate
produces a nonsense ledger:

| What they said | Where it goes |
|---|---|
| An approach — "replace the cache layer" | Becomes a candidate |
| A constraint — "don't touch the serializer" | A filter applied to every candidate before scoring, and a named ledger row |
| A requirement — "must work on the 2019 data" | Goes to the issue's acceptance criteria, not the ledger |

Ask once. **"No view" is a valid answer** — record "no approach offered" and
move on. Never invent one and attribute it to them: Step 3 scores it as theirs,
which launders your own idea into their column.

Ask in one message, the way [Asking the user](#asking-the-user) describes.

---

## Step 2 — Generate 0–3 alternatives

Independent of the engineer's answer, not variations on it. Normalize every
candidate — theirs included — into one template, which is also what makes Step
3's relabeling work:

```text
Mechanism:        <one line>
Touches:          <files and symbols, from the baseline>
Explicitly not:   <what it leaves alone>
Failure mode:     <how it breaks, concretely>
Reversal path:    <how you back it out>
```

**A falsifiable test for "differs in mechanism".** Two candidates differ only
if they touch a disjoint-enough set of files or layers, **or** one's failure
mode is impossible under the other. State, per pair, one thing true under A and
false under B. If you cannot, they are one approach wearing two names.

A short-term hack is a legitimate candidate **when one genuinely exists**,
labeled as such. Never manufacture a strawman to flatter the recommendation,
and never pad to three.

**Coming up empty is a first-class outcome.** If you cannot find a genuine
alternative, say so — "no alternative mechanism found; here is what I
considered and why each collapsed into yours" — and proceed with theirs as the
only option. A fabricated alternative makes Step 3 look like a choice when it
was not.

- **They offered an approach, you found no alternative** → proceed with theirs
  as the sole candidate. Step 3 still runs: scoring one approach surfaces its
  tradeoffs and its reversal path. The output is a ledger, not a ranking.
- **They offered no approach and you found none** → a dead end, not a plan. Say
  so, report the baseline, and ask for direction.

---

## Step 3 — Score the approaches independently

Score every candidate against
[`references/ranking-rubric.md`](references/ranking-rubric.md) in a context
that did not generate them. With a subagent facility available (Claude Code's
delegation tool, or an installed Pi extension such as `pi-subagents`), run one
analyst against [`references/analyst-brief.md`](references/analyst-brief.md)
and nothing else — not the conversation, not which candidate came from whom.
Otherwise run the same brief against yourself inline, writing the ledger out in
full before reading any of it back; the rubric, the evidence bar and the ledger
schema are identical either way.

**Make the blinding mechanical, not ceremonial.** Stripping a name does nothing
when one candidate is the only one in first person, or the only one labeled
"hack". So: normalize into Step 2's template, *then* relabel A/B/C by a stated
rule (alphabetical on the mechanism line), and pass nothing else. With a single
candidate there is nothing to blind — score it directly.

Then add the control blinding cannot give. The ledger must name, for the
**recommended** option, the strongest argument against it; and for each
**rejected** option, the condition under which it would have won. Both survive
a reader who knows whose idea was whose.

**Bound the work:**

| Change | Tool-call budget |
|--------|------------------|
| One or two files, mechanism obvious | ~10 |
| A subsystem, several call sites | ~25 |
| Cross-cutting or cross-repo | ~40 |

Every accepted tradeoff gets a ledger row. **A hack with no ledger row is not
an approved hack** — and where the recommendation *is* a workaround, the row
also needs a follow-up issue tracking the real fix. Draft it now; it ships with
the plan in Step 5 and opens in Step 6.

---

## Step 4 — Decompose and chunk the delivery

Two outputs: **phases** are units of work with done-conditions; **chunks** are
units of delivery, one per pull request. A chunk contains one or more phases.

**On the fast path this is short, not skipped.** One phase with a real
done-condition, "single PR, ~N lines", and move on.

**Phases.** Each with a condition that can be checked rather than asserted —
"the wrapper's tests cover exhaustion, backoff and the non-retryable 4xx case",
not "retries work".

**Chunks.** One PR or several, where the boundaries fall, in what order. The
procedure is in
[`references/chunking-the-work.md`](references/chunking-the-work.md). The four
things that decide it:

- **One PR is the default.** Splitting costs rebase churn and review latency,
  so it has to be bought with a reason.
- **Size sets the budget, not the boundary.** 500 changed lines soft, 1,000
  hard, excluding lockfiles and generated paths. Crossing a threshold forces
  the question; it never answers it. Cut on a functional or service line, never
  at the line where the counter crossed 500.
- **Over the hard limit with no axis that works is a bypass request, not your
  call.** Bring the axes you tried, the size and how much is mechanical, and
  what makes it reviewable anyway. Never assume it.
- **Every chunk is independently reviewable, correct** (trunk deployable after
  it merges) **and revertable** — or explicitly declared not to be. Say whether
  chunks are stacked or independent, and flag any dependency on a *deploy*
  rather than a merge; a reviewer cannot infer that from the diff.

**Record the paths each chunk will touch**, as globs or exact files, in the
chunk table's `Touches` column. `implement-plan` matches those against each
ADR's `applies_to` before the chunk's first edit — without them a governing
decision is only discovered once the code already contradicts it. Estimates
are fine; the point is knowing which decisions apply.

Sizes here are estimates off the baseline. Label them as estimates;
implementation re-measures.

---

## Step 5 — Present and approve

Show the recommendation with its scores, the runner-up and why it lost, the
tradeoff ledger, each rejected approach in one line, and the delivery plan —
chunks with axis, estimated size, and order.

**On the fast path, present in a few lines:** what you are going to do, why it
is the obvious call, the one phase, the size estimate, and the line from
scoping saying it qualified as trivial. No scores, no runner-up, no ledger.

**The runner-up and rejected list are conditional.** With a sole candidate,
omit both rather than fabricating them and say no alternative was found.

**Ask for a hard-limit bypass here**, if one is needed, as part of the same
approval. Do not bury it in the chunk table.

**If the recommendation is a workaround, show the drafted follow-up issue too**
— title, target repo, body — and say that approving the plan means opening it.
**Declining the follow-up declines the workaround.** Go back and recommend
something else; the approved option is then whatever wins without it. There is
no third door where the workaround ships and the debt goes untracked: the
rubric makes the Follow-up URL mandatory for exactly this row, and Step 6 opens
the issue before the plan is recorded, so an "accepted untracked" workaround
would be approved against a ledger it cannot satisfy.

Then confirm, with the options approve / revise / pick another / cancel, asked
the way [Asking the user](#asking-the-user) describes.

**Nothing is recorded and no code is written until the engineer approves.**
"Revise" goes back to Step 2, 3 or 4 — or, on the fast path, to Step 1 if they
want options after all. Never forward to implementation.

---

## Step 6 — Record the approved plan

**Approved a workaround? Open its follow-up issue first**, so the ledger row
carries a URL instead of a promise. Create it with
`create-ol-github-issue` using
the body approved in Step 5 — do not re-draft it. Then link it the three ways
[`references/ranking-rubric.md`](references/ranking-rubric.md) requires: the
ledger row, the code comment (added during implementation), and a witan task —
`task_create(title=..., external_uri="<follow-up issue URL>",
discovered_from=["<this work's task slug>"])`.

**Where the plan lands depends on the route:**

- **The plan comment is the work record** — chunks, phases, status, the
  tradeoff ledger. Always, on every path.
- **Decisions worth recording go to [`write-adr`](../write-adr/SKILL.md)**, one
  ADR per decision, zero on the fast path. The comment links to any that were
  written; it does not restate them.
- **On the spec path, the design goes in a file in the repo**, and the comment
  shrinks to what it is good at: chunk status and links. See below.

### The spec path

Work spanning several pull requests, sessions or repos produces a **spec**: a
reviewed file in the repo, not an issue comment. At that size a comment kept
consistent by hand with a digest is rebuilding a versioned file the hard way,
and the thing readers need — what was decided, in what order, and what is still
open — is exactly what a diff and a PR review handle well.

Hand off to [`write-spec`](../write-spec/SKILL.md), which detects where this
repo already keeps them and matches that convention. Pass what this skill
produced: the chosen approach, the rejected ones with the condition that would
flip each, the chunk boundaries and their `Touches` paths, the measured
baseline from scoping, and **which decisions are still open** — those become
its numbered `Dn. (open)` sections, and knowing them up front is what keeps the
spec honest about what it has settled.

The plan comment still gets written, and shrinks to what it is good at: chunk
status, PR links, and a link to the spec. The design does not live in both.

**Extract each decision into its own ADR as it settles, not up front.** A
decision still moving is a spec section; an ADR written over it would be
amended before the work shipped. `write-spec` owns that handoff too.

Write the comment body to the draft path with your file-writing tool — never a
heredoc, which drops and splices lines mid-sentence. The path is derived from
the repo and issue, not fixed, so ask the script for it rather than guessing:

<a id="resolving-the-scripts"></a>

**Resolving the scripts.** They ship *inside this skill*, so the one reliable
anchor is the directory you loaded this `SKILL.md` from — `scripts/` is beside
it, and sibling skills are in its parent. Use that first.

`~/.claude/skills` is **not** a safe default: skills install to
`.claude/skills`, `~/.pi/agent/skills`, `.agents/skills`, `.github/skills` and
`.opencode/skill` depending on the agent, in project scope as well as global,
and this skill also ships inside the witan package. Four of those roots have no
`~/.claude` in them at all.

If you genuinely cannot tell where you were loaded from, probe once and reuse
the answer for the whole session — project scope first, since a project-scoped
install overrides the global one:

```bash
for root in .claude/skills .agents/skills .pi/skills .github/skills \
            .opencode/skill "$HOME/.claude/skills" "$HOME/.agents/skills" \
            "$HOME/.pi/agent/skills" mcp/servers/witan/witan/skills; do
  [ -f "$root/plan-technical-approach/scripts/upsert-plan-comment.sh" ] \
    && SKILLS="$root" && break
done
[ -n "${SKILLS:-}" ] || echo "plan-comment scripts not found -- locate the installed skill root before continuing" >&2
```

The list is fixed and in a fixed order on purpose. The resolved command string
is then the same on every run on a given machine, so a permission-allowlist
entry approved once keeps matching — the same reason `scripts/paths.sh` derives
the draft path itself instead of taking it as an argument.

```bash
PTA="$SKILLS/plan-technical-approach/scripts"

bash "$PTA/upsert-plan-comment.sh" \
  --repo <owner/repo> --issue <N> --print-draft-path
# -> ~/.cache/ol-plan/<owner>-<repo>-<N>.md

bash "$PTA/upsert-plan-comment.sh" \
  --repo <owner/repo> --issue <N> --dry-run
```

Invoke with `bash <path>` rather than as a bare executable. The mode bits do
survive the wheel — `witan setup` copies with `shutil.copy2` and hatchling
preserves them — but `bash <path>` works whether or not they did, and the
failure it avoids (`permission denied` from a copy that dropped the bit) is
one an agent tends to work around by rewriting the command rather than fixing
the install.

Check what it reports, then re-run without `--dry-run`. The script computes the
draft path itself, prepends the marker and appends a digest; do not pass paths
it can derive, and do not hand-write the marker.

**Updating a comment you did not just write?** Read it first, and pass the
digest back:

```bash
bash "$PTA/fetch-plan-comment.sh" \
  --repo <owner/repo> --issue <N>          # prints the live body and its sha
bash "$PTA/upsert-plan-comment.sh" \
  --repo <owner/repo> --issue <N> --expect-sha <that sha>
```

The `sha` that `fetch` prints is **computed from the body it fetched**, not
copied out of the footer that body carries, so an edit made in the GitHub UI
changes it. `--expect-sha` is still not a lock: GitHub's PATCH ignores
`If-Match`, so it catches an edit made *before* your read and nothing landing
after it.

Without `--expect-sha` an upsert is a full-body overwrite, so a session working
from a stale or empty draft cache replaces the Revisions log, the merged chunk
PR URLs and the tradeoff ledger wholesale — and the comment's own edit history
is the only other copy. `--require-existing` prevents duplicates, not
clobbering. Format per
[`references/plan-comment-format.md`](references/plan-comment-format.md).

**More than one chunk?** Mirror the delivery shape in the graph: an epic with
one child task per chunk, and `blocks` links for a stack, so the order is
visible to anyone looking for ready work.

**Promote the task `scope-work` already claimed — do not create a second one.**
One issue is one task; it becomes an epic when the work turns out to need
chunking:

```text
task_update(slug="<the task scope-work claimed>", type="epic")
task_create(title="<chunk 1>", parent="<that slug>", type="feature",
            project_slug="<project>")
task_create(title="<chunk 2>", parent="<that slug>", type="feature",
            project_slug="<project>")
task_link(from_slug="<chunk-1 slug>", to_slug="<chunk-2 slug>", kind="blocks")
```

Two things that bit here before:

- Calling `task_create(type="epic", external_uri=<the same issue>)` left **two**
  tasks pointing at one issue. `implement-plan` closes "the epic, or the single
  task", so the claimed scoping task stayed `in_progress` forever — holding a
  lease on work that had shipped.
- **Pass `project_slug` on every chunk task.** Without it the chunks do not roll
  up to the `WorkflowProject`, and the project's own status reads as though
  nothing is happening while every chunk is in flight.

Note that `task_update` cannot set `blocked_by` after creation — `task_link` is
the only way to add the dependency later. A single chunk needs no epic and no
promotion: the task stays a task.

Advance the project to `implementation` with `workflow_project_advance`, then
hand off to [`implement-plan`](../implement-plan/SKILL.md).
