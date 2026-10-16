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

**A claim is a compare-and-swap with a lease. Once granted, it changes only by
a write meant to change it.**

### How it works

On a local `.omni` store, `OmnigraphClient` serialises writes with a per-store
advisory `flock`. A shared deployment cannot: a `flock` is a local-filesystem
lock and cannot coordinate across pods. What makes a claim safe there is the
store itself.

omnigraph accepts a precondition on a write: `/mutate/if-graph-commit` over
HTTP, `--if-commit` on the CLI. The write applies only while the branch head is
still the `graph_commit_id` the caller read, and otherwise is refused with a
412, having written nothing. `task_claim` reads the task, decides on that row
whether it may take it (not closed, not blocked, not held by someone else's
live lease), and writes its claim fenced on the commit of that same read. A
claim that succeeds therefore proves nobody claimed, closed or blocked the task
in between. A rival whose claim lands later either read yours and is refused,
or fenced on an older head and is refused by the store.

Every other task write is fenced the same way. A task write replaces the whole
row, so an unfenced edit that read the task before your claim would put back
the old status and assignee and leave the task open for a second claimer.
Instead, `task_update`, `task_close`, `task_release`, `task_link`,
`task_unlink` and the unblock sweep each state the commit of the read they
merged from, and a refused write re-reads and merges again.

Two details make the loop work:

1. **Conflict-surfacing writes.** A task write raises a typed
   `OmnigraphConflict` on a refused precondition or a write conflict, rather
   than retrying the same merge. A blind retry would re-apply a row built from
   a read that is already stale.

2. **Bounded retry.** The precondition covers the whole branch head, not this
   task, so any concurrent write to the graph refuses it. A refused write backs
   off, re-reads and tries again, up to five times. For a claim, the next
   attempt's read decides whether a rival won (`{"claimed": false, "reason":
   "lost_race", "held_by": …}`) or the conflict was unrelated, and a budget that
   runs out reports `"reason": "contention"` once a last read shows the task is
   still claimable. Any other task write that runs out raises `WriteContention`,
   having written nothing.

### What it does not cover

- Writes meant to change the holder. `force` steals on purpose, `task_update`
  can set `assignee` or `status` directly, `task_release --force` clears
  someone else's claim, and a claim taken after the lease lapses is a
  legitimate takeover.
- Two sessions that claim under the same holder string. Pass `session_id`, or
  they cannot be told apart (the claim reports `"qualified": false`).
- `store_merge` (`witan migrate merge`), which loads reconciled rows rather
  than writing through the fenced path. A claim that lands while a merge into a
  served store runs can be overwritten by a newer row from the source.
- A write whose response is lost. The server finishes an admitted write after
  its caller disconnects, so a timeout or a dropped connection can leave a claim
  that landed. witan does not re-send it. Re-read with `task_get` before
  retrying.

[ADR 0003](decisions/0003-atomic-task-claims-cas.md) has the history, including
the best-effort design this replaced.

### Leases

Claims carry `claimed_at` and expire. A task held by a session that crashed does
not stay held forever, and the lease is also the backstop that recovers any task
that ends up mis-owned through the cases above. `task_release` hands one back
deliberately.

## Saying something about a task

A claim answers "who is on this?". It does not answer "is this task even
right?" — and until `task_comment` existed there was nowhere to put that answer.
Every write primitive either mutated the task (`task_update`, which overwrites
another author's description) or created a node (`task_create`, `memory_store`),
so an agent that found a problem with someone else's in-flight work had to
choose between clobbering their text and inflating the work list.

What that cost, concretely: a one-paragraph correction — the task's stated
mechanism could not fire on the pipelines it named — became a whole `p0` task
whose real content was "your premise is wrong", parented under the original so
its holder would encounter it. The ready-work list gained an item that was not
work; it had to be filed `p0` to sit next to its parent, so it competed with
real `p0`s; a `parent` edge was written onto someone else's claimed row; and
closing it would have meant "I read this", which is not what closing a task
means anywhere else.

`task_comment(slug, text)` is the primitive that was missing. A comment is
attributed, timestamped, and append-only — and it is **flat**: no threading, no
editing, no deleting, no resolution state. Those are all decisions to make when
something actually needs them.

Two details are load-bearing:

- **It does not touch the task's row, `updated_at` included.** That field
  doubles as the advisory-lease start for an `in_progress` task with no
  `claimed_at`, so bumping it would silently renew a stranger's claim every
  time somebody commented on their task.
- **It is surfaced where an executing agent already looks**, not where a
  curious one might. `task_get` returns comments alongside the description, and
  the context-injection hook puts unread comments on a task you hold *first* —
  ahead of projects and ready work. A correction to the task in front of you is
  the only thing in that block addressed to you specifically. Unread is a local
  watermark rather than a read receipt in the graph, because "was rendered into
  this machine's prompt" is a fact about a client, not about shared work.

A comment is read once, by whoever executes that task. Reusable knowledge about
the repo still belongs in a `Memory` — the two compose: store the fact, then
comment with the correction and a pointer to it.

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
