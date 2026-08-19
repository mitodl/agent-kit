# Tasks and projects

Two things live at this layer, and they answer different questions:

- **Tasks** — *what needs doing.* Discrete units of work, with dependencies,
  priorities, and an owner while someone is on them.
- **Workflow projects** — *what we are trying to achieve.* An objective that
  spans many sessions and possibly several repos, moving through phases.

You will use tasks constantly and projects occasionally.

## File a task

```bash
witan task create "Retry logic drops the last attempt's error" \
  --type bug --priority p1 \
  --description "The final exception is swallowed, so a permanent failure looks like a timeout."
```

```
Created task: tk-retry-logic-drops-the-last-attempt-s-e-4f9c21
  status: open
  repo: mitodl/agent-kit
```

The `repo` is auto-detected. Useful flags:

| Flag | Effect |
| --- | --- |
| `--type` | `bug`, `feature`, `task`, `chore`, `epic` |
| `--priority` | `p0` (highest) through `p3` |
| `--parent` | Roll this up under an epic |
| `--blocked-by` | `tk-` slugs that must close before this is ready |
| `--discovered-from` | The task you were on when you found this |
| `--project` | The `wp-` project this belongs to |
| `--symbol-refs` | Code-graph symbols (`repo#path::Name`) this concerns |

`--discovered-from` is worth the habit. Follow-up work found mid-task is the
easiest thing to lose, and the edge records *why* the task exists.

## Find work

```bash
witan tasks              # open tasks in this repo, by priority
witan tasks --ready      # only those with no open blockers
witan tasks --all-repos  # across every repo in the store
```

"Ready" is computed, not stored: a task is ready when every task it is
`blocked_by` has closed *and* its status still makes it claimable. Closing a
blocker automatically unblocks its dependents, so the ready list stays correct
without anyone maintaining it.

Claimable covers more than `open`: a `blocked` task counts once its blockers
close, and an `in_progress` task comes back when its claim lease lapses — that
is how work abandoned by a crashed session returns to the list rather than
being held forever.

## Claim, work, close

```bash
witan run tk-retry-logic-drops-the-last-attempt-s-e-4f9c21
```

This claims the task under your author name and launches your configured agent
with a prompt seeded from the task's title, description, and symbol refs. To see
that prompt without doing anything: `--dry-run`. To launch without claiming:
`--claim=false`.

From inside an agent session, use the tools directly — `task_claim`, then
`task_close` with a resolution:

```bash
witan task close tk-retry-logic-... --resolution "Re-raise the final exception; test added"
```

### What a claim actually guarantees

This is worth being precise about, because the answer is "less than you might
assume".

A claim is an **advisory lease with a best-effort compare-and-swap**, not a
lock. On a local store, writes are serialised by a file lock and a claim is
effectively safe. Against a **shared, deployed** store there is no such lock,
and omnigraph offers no conditional-write primitive — you cannot ask the store
to "set this claim only if it is still unclaimed" and have it reject the loser.

witan reconstructs as much of that as it can: it detects the lost-race conflict
and surfaces it rather than retrying over the winner. But under genuine
concurrent write load, mutual exclusion has been observed to fail. Treat a claim
as *coordination* — a strong signal that someone is on this — rather than as a
correctness guarantee. Design work so that two agents briefly overlapping is
wasteful, not destructive.

[ADR 0003](../explanation/decisions/0003-atomic-task-claims-cas.md) records the
full reasoning and the exact limits.

Claims also carry a **lease expiry**, so a task held by a session that died does
not stay held forever. `witan task release` hands one back deliberately.

## Multi-session projects

When work will not finish in one sitting, create a project instead of holding
the thread in your head:

```bash
witan project create "Migrate auth to OAuth2" --phase discovery
```

Projects move through four phases — `discovery` → `spec` → `implementation` →
`delivery` — via `witan project advance`.

The part that makes them worth using is **session linking**. At the top of each
session, `workflow_session_start` registers that session against the project; at
the end, `workflow_session_end` records a summary. That summary is what a
*different* session, or a different agent, reads to pick up the thread — which
is why the loop works without an explicit hand-off document.

```bash
witan projects                    # active projects for this repo
witan project status wp-...       # phase, sessions, last summary
witan project tasks wp-...        # the tasks rolling up to it
```

`witan project complete` closes a project out and assembles a `WorkflowTrace` —
a corpus record built from every session that contributed, kept for later
pattern mining.

!!! tip "Let the skills drive this"

    `/witan-task` and `/witan-workflow` automate the picking and linking. The
    CLI shown here is what they call underneath, and what you want for triage.

## Branch tracking, for free

`task_claim` and `workflow_session_start` both quietly upsert a `CodeBranch`
node linking your current repo and branch to the task or project in flight. No
command, no configuration — it just means "which branch carries task X" is a
one-hop query later. It no-ops silently outside a git checkout.

---

**Next:** [Indexing a repository →](code-graph.md)
