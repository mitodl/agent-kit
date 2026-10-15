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

## Entry modes

| Entry | Behavior |
|---|---|
| **Fast path** — scoping called the work trivial | Skip Steps 1–3. One obvious approach, short plan comment, no spec, no ledger. Go to Step 4. |
| **Full path** — scoping found a decision worth recording | All steps. The chosen approach and its alternatives feed the ADR(s) written by [`write-adr`](../write-adr/SKILL.md); delivery stays in the plan comment. |

Scoring comes first, then the record: run Steps 1–4, then hand the chosen
approach and the rejected alternatives to [`write-adr`](../write-adr/SKILL.md),
which turns them into `## Decision` and `## Options Considered`. An ADR written
before the alternatives were scored records a preference, not a decision.

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
| A requirement — "must work on the 2019 data" | Goes to the spec, not the ledger |

Ask once. **"No view" is a valid answer** — record "no approach offered" and
move on. Never invent one and attribute it to them: Step 3 scores it as theirs,
which launders your own idea into their column.

Ask in one message. If a structured question tool is available (Claude Code's
`AskUserQuestion`, or a Pi extension that registers one), use it; otherwise ask
in plain prose and end your turn and wait for the reply.

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
If the engineer declines the follow-up, the workaround is no longer approved:
recommend something else, or get them to say on the record that the debt is
accepted untracked.

Then confirm. With a structured question tool, use the options approve /
revise / pick another / cancel; otherwise ask the same in plain prose, number
the options, and end your turn and wait.

**Nothing is recorded and no code is written until the engineer approves.**
"Revise" goes back to Step 2, 3 or 4 — or, on the fast path, to Step 1 if they
want options after all. Never forward to implementation.

---

## Step 6 — Record the approved plan

**Approved a workaround? Open its follow-up issue first**, so the ledger row
carries a URL instead of a promise. Create it with
[`create-ol-github-issue`](../../process/create-ol-github-issue/SKILL.md) using
the body approved in Step 5 — do not re-draft it. Then link it the three ways
[`references/ranking-rubric.md`](references/ranking-rubric.md) requires: the
ledger row, the code comment (added during implementation), and a witan task —
`task_create(title=..., external_uri="<follow-up issue URL>",
discovered_from=["<this work's task slug>"])`.

**Where the plan lands depends on the route:**

- **The plan comment is the work record** — chunks, phases, status, the
  tradeoff ledger. Always, on both paths.
- **Decisions worth recording go to [`write-adr`](../write-adr/SKILL.md)**, one
  ADR per decision, zero on the fast path. The comment links to any that were
  written; it does not restate them.

Write the comment body to the draft path with your file-writing tool — never a
heredoc, which drops and splices lines mid-sentence. The path is derived from
the repo and issue, not fixed, so ask the script for it rather than guessing:

```bash
SKILLS=./skills/workflow   # installed: ~/.claude/skills
"$SKILLS"/plan-technical-approach/scripts/upsert-plan-comment.sh \
  --repo <owner/repo> --issue <N> --print-draft-path
# -> ~/.cache/ol-plan/<owner>-<repo>-<N>.md

"$SKILLS"/plan-technical-approach/scripts/upsert-plan-comment.sh \
  --repo <owner/repo> --issue <N> --dry-run
```

Check what it reports, then re-run without `--dry-run`. The script computes the
draft path itself, prepends the marker and appends a digest; do not pass paths
it can derive, and do not hand-write the marker.

**Updating a comment you did not just write?** Read it first, and pass the
digest back:

```bash
"$SKILLS"/plan-technical-approach/scripts/fetch-plan-comment.sh \
  --repo <owner/repo> --issue <N>          # prints the live body and its sha
"$SKILLS"/plan-technical-approach/scripts/upsert-plan-comment.sh \
  --repo <owner/repo> --issue <N> --expect-sha <that sha>
```

Without `--expect-sha` an upsert is a full-body overwrite, so a session working
from a stale or empty draft cache replaces the Revisions log, the merged chunk
PR URLs and the tradeoff ledger wholesale — and the comment's own edit history
is the only other copy. `--require-existing` prevents duplicates, not
clobbering. Format per
[`references/plan-comment-format.md`](references/plan-comment-format.md).

**More than one chunk?** Mirror the delivery shape in the graph: an epic with
one child task per chunk, and `blocks` links for a stack, so the order is
visible to anyone looking for ready work:

```text
task_create(type="epic", title="<the whole piece>", external_uri="<issue url>")
task_create(title="<chunk 1>", parent="<epic slug>", type="feature")
task_link(from_slug="<chunk-1 slug>", to_slug="<chunk-2 slug>", kind="blocks")
```

Note that `task_update` cannot set `blocked_by` after creation — `task_link` is
the only way to add the dependency later. A single chunk needs no epic.

Advance the project to `implementation` with `workflow_project_advance`, then
hand off to [`implement-plan`](../implement-plan/SKILL.md).
