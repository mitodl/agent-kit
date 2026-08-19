# Coordinating work

Once more than one agent can act at the same time, they need a shared answer to
"what is being worked on". This page is about how witan provides that answer,
and — more importantly — how strong the answer actually is.

## Ready work is computed

A task carries `blocked_by`: the slugs of tasks that must close before it can
start. "Ready" is derived from that, not stored:

> A task is ready when every task blocking it has closed **and** its status
> makes it claimable.

Claimable is broader than "open". `readiness.status_pickable` treats `open` and
`blocked` alike — a `blocked` task whose blockers have all closed is exactly
the case that should become pickable — and it also returns an `in_progress`
task once its **lease has lapsed**, on the assumption the holder crashed
without releasing it. Only `closed` is never pickable.

Closing a blocker unblocks its dependents automatically, so the ready list stays
correct without anyone maintaining it. `task_ready` and `witan tasks --ready`
both answer the same computed question.

The dependency edges are worth using properly:

| Edge | Meaning |
| --- | --- |
| `blocks` | This must close before that can start |
| `parent_of` | Epic → sub-issue |
| `discovered_from` | This was found while working on that |
| `addresses` | This task acts on that memory |
| `task_belongs_to` | This rolls up to that workflow project |

`discovered_from` is the one people skip and later wish they had not. Follow-up
work found mid-task is the easiest thing to lose, and the edge preserves the
reason the task exists at all.

## What a claim guarantees

Here is the honest version, because a confident wrong answer here causes real
double-work.

**A claim is an advisory lease with a best-effort compare-and-swap. It is not a
mutex.**

### Why it cannot be more

On a local `.omni` store, `OmnigraphClient` serialises writes with a per-store
advisory `flock`, and claiming is effectively safe.

A shared deployment removes that. A `flock` is a local-filesystem lock; it
cannot coordinate across pods. And omnigraph — this is the crux — offers **no
conditional-write primitive**. There is no `--if-version`, no
`--expected-commit`, and the query engine cannot express a
`... WHERE status = 'open'` guard inside the mutation. You cannot ask the store
to "set this claim only if it is still unclaimed" and have the store reject the
loser.

So a naive read-check-write lets two agents both read a task as `open`, both
write their own claim, and the last write silently clobbers the first.

### What witan does instead

Three mechanisms, reconstructing as much of a CAS as the storage layer permits:

1. **Conflict-surfacing writes.** `task_claim` opts into raising a typed
   `OmnigraphConflict` on a Lance optimistic-concurrency conflict, rather than
   retrying. Every other write keeps transparent retry, which idempotent upserts
   rely on — but for a claim, a blind retry re-reads the updated state and
   re-applies the claim *over* whoever won, turning a should-fail claim into a
   clobbering success.

2. **Conflict-aware retry.** On conflict, `task_claim` re-reads rather than
   re-applying. If someone else now holds a live claim, it returns
   `{"claimed": false, "reason": "lost_race", "held_by": …}`. If the conflicting
   write was unrelated, or the rival's lease has lapsed, it retries — in a
   bounded loop that keeps surfacing conflicts, so a consecutive conflict never
   falls back to the clobbering path.

3. **Post-write verification.** Because the last writer still wins, `task_claim`
   re-reads after writing and confirms it is still the assignee. A claim that
   was overwritten by a rival landing later is reported as `lost_race`, not as a
   false success.

Together these collapse the common double-claim race to at most one
`claimed: true`, and never a silent clobber.

### The residual window

A small window remains: if both callers run their verification read after both
writes commit and before either observes the other, both can see the last writer
and one of them is wrong.

This is not merely theoretical — it has been observed under concurrent write
load. Treat `claimed: true` as **"you almost certainly hold this"**, not as a
hard mutex, and design work so that a brief overlap between two agents is
wasteful rather than destructive.

A true single-round-trip guarantee needs omnigraph to accept a manifest-version
or commit-id precondition on `mutate`. Until that exists upstream, this is the
ceiling. [ADR 0003](decisions/0003-atomic-task-claims-cas.md) has the full
analysis.

### Leases

Claims carry `claimed_at` and expire. A task held by a session that crashed does
not stay held forever, and the lease is also the backstop that recovers any task
that ends up mis-owned through the window above. `task_release` hands one back
deliberately.

## Projects and sessions

A `WorkflowProject` tracks an objective across many sessions, through four
phases: `discovery` → `spec` → `implementation` → `delivery`. A project may span
several repos, or none.

The mechanism that makes this useful is the **session**. `workflow_session_start`
registers a session against a project; `workflow_session_end` records a summary.
That summary is what a *different* session — possibly a different agent, on a
different machine, days later — reads to pick the thread up.

This is why witan needs no hand-off document. The hand-off is a graph edge.

`workflow_session_start` is **re-entrant**: calling it again for a still-open
`(project, session_id)` returns the same handle rather than minting a second
node, so a hook retry or a transport reconnect cannot silently duplicate a
session. Two genuinely simultaneous starts can still both insert, and are
de-duplicated immediately afterwards rather than left for a migration to find.

When a project completes, `workflow_project_complete` assembles a
`WorkflowTrace` — a corpus record built from every contributing session, kept so
that how work actually got done can be mined later.

## Branch tracking

`task_claim` and `workflow_session_start` both upsert a `CodeBranch` node linking
the current repo and branch to the task or project in flight. Best-effort, no
command, silent no-op outside a checkout.

The payoff is that "which branch carries task X" is a one-hop query, and the
session-start hook can tell you the branch you just checked out already has work
in progress against it.
