# 3. Best-effort compare-and-swap for multi-user task claims

- Status: Accepted
- Date: 2026-07-08
- Deciders: witan platform owners
- Tracking: task `tk-atomic-task-claims-113c26`, project `wp-witan-multi-user-service-deployment-dcf6ee`
- Supersedes: —
- Related: `docs/adr/0002-witan-cedar-authorization-bundle.md`; `tk-spike-validate-omnigraph-server-remote-write-ser-1a8058` (remote-write serialization spike); `pf-witan-multi-user-service-deployment-rfc-split-da-c48753`

## Context

`task_claim` is the coordination primitive that lets parallel agents and
multiple humans share one work-coordination graph without double-working a
task. The shipped implementation (Option A) is a read-check-write: read the
task, reject if held, else write `status=in_progress, assignee, claimed_at`.

On a **local** `.omni` store this is safe enough — `OmnigraphClient` serializes
writes with a per-store advisory `flock` (`graph.py:_acquire_write_lock`). But
the multi-user deployment (the parent project) replaces the local store with a
shared `omnigraph-server` over `http(s)`/`s3`, and there the flock is skipped
(it is a local-filesystem lock and cannot coordinate across pods). Two agents
that both read a task as `open` will both write their own claim; last write
wins and silently clobbers the first claimant. This ADR records what atomicity
is actually achievable and what we shipped.

### Forces — omnigraph's concurrency surface (0.8.0, verified against the binary)

- **An optimistic-concurrency token exists.** `omnigraph commit list --json`
  returns per-commit `graph_commit_id` (a ULID) and a monotonic
  `manifest_version`; `omnigraph snapshot --json` returns the branch
  `manifest_version` and per-table `table_version`. Each write advances these.
- **But there is no conditional-write primitive.** `omnigraph mutate` accepts
  no `--if-version` / `--expected-commit` / precondition flag, and the query
  engine cannot express a compound `... WHERE status = 'open'` guard inside the
  mutation itself. So a **single-statement store-level CAS is impossible**
  through the 0.8.0 client surface — you cannot ask the store to "set the claim
  only if it is still unclaimed" and have the store reject a lost race.
- **Lance OCC conflicts do surface, but were being masked.** When two writers
  race the same manifest version with no serializing flock (the shared case),
  one commit fails with `stale view` / `manifest table version`.
  `OmnigraphClient._execute` treated those as transient and **blindly retried
  the same mutation** — correct for an idempotent upsert, but for a claim the
  retry re-reads the now-updated state and re-applies the claim *over* whoever
  won. The masking turned a should-fail claim into a clobbering success.

## Decision

Ship a **best-effort CAS** claim — the strongest guarantee available without an
upstream conditional-write feature — built from three parts:

1. **Conflict-surfacing writes.** `OmnigraphClient.change(...,
   surface_conflict=True)` raises a typed `OmnigraphConflict` on a Lance OCC
   conflict instead of retrying it. Only `task_claim` opts in; every other
   write keeps the transparent-retry behaviour idempotent upserts rely on.

2. **Conflict-aware claim.** On `OmnigraphConflict`, `task_claim` re-reads the
   task rather than re-applying its write. If a different actor now holds a
   live (non-lease-expired) claim, it returns
   `{"claimed": false, "reason": "lost_race", "held_by": ...}`. If the
   conflicting write was unrelated (or the rival's lease has since lapsed), it
   retries the claim — in a bounded loop that keeps `surface_conflict=True` for
   every attempt, so a *consecutive* conflict is handled the same way and never
   falls back to the blind-retry path that would clobber a new winner.

3. **Post-write ownership verification.** Because the last writer still wins
   with no store CAS, after writing the claim `task_claim` re-reads and confirms
   `assignee == holder` before reporting success. A claim that was overwritten
   by a rival landing last is reported as `lost_race`, not a false success.

Together these make the common double-claim race resolve to **at most one
`claimed: true`**, and never a silent clobber. The lease (`claimed_at`,
`_CLAIM_LEASE_SECONDS`) remains the backstop for the residual window where two
callers both read *after* all racing writes have settled.

## Consequences

- **Not truly atomic.** A vanishingly small window remains: if both callers run
  their post-write verification read after both writes commit and before either
  observes the other, both could see the last writer and one is wrong. In
  practice the write ordering + verification collapses this to near-zero, and
  the lease recovers any task that ends up mis-owned. Callers must still treat
  `claimed: true` as "you almost certainly hold it", not a hard mutex — the tool
  docstring says so.
- **Depends on the server serializing writes.** The conflict-surfacing path
  assumes `omnigraph-server` either serializes branch writes or lets Lance OCC
  reject the loser. That assumption is validated by
  `tk-spike-validate-omnigraph-server-remote-write-ser-1a8058`; if the server
  instead silently accepts both writes with no conflict, only the post-write
  verification (part 3) protects us — which it still does.
- **True atomic CAS is an upstream ask.** A single-round-trip guarantee needs
  omnigraph to accept a manifest-version / commit-id precondition on `mutate`
  (compare-and-swap) or a conditional mutation guard. Tracked as a follow-up
  task against the omnigraph project; until then this ADR is the ceiling.
- **No schema or API change.** `task_claim`'s return shape gains a `lost_race`
  reason; existing `claimed`/`held`/`blocked`/`closed` paths are unchanged.

## Addendum (2026-08-27) — mutual exclusion is off for a caller that sends no `session_id`, and now says so

Everything above is about excluding two *people*. A separate hole excludes two
of one person's *sessions*, and it is open for any MCP client that calls a
deployed witan directly. `_claim_holder` qualifies the holder as
`<identity>#<session>`, but the id has to arrive as an argument: a pod has no
`$CLAUDE_SESSION_ID` and MCP 2026-07-28 carries no session state, so the server
cannot supply it. `witan_core.remote.proxy._map_args` injects it for the CLI;
nothing injects it for an agent. Without it both sessions claim under the bare
`preferred_username`, `current_holder != holder` is False, and the second
session renews the first's lease and is told `claimed: true`.

**Measured before deciding.** Sampled the deployed graph via `witan tasks
--all-repos --output-format json` across all four statuses (the listing is
capped at 50 rows per status, so this is the recent slice, not the whole
graph): 183 distinct tasks, 60 carrying an assignee. 35 holders are qualified.
Of the 25 that are not, 11 are `Tobias Macey` — the local `cfg.author` shape,
a hand-run CLI outside an agent session, not this hole. 13 are
identity-shaped — four distinct login/email-form holders, counts 5/4/2/2 —
which is exactly this: a deployed claim with no `session_id`. (Names omitted:
this repo is public, and witan's own write-path scanner flags them as PII, which
is how the omission got noticed.)
That is 13 of 48 deployed claims, across three different people. The remaining
one, a bare `session_01E2dsMxDihnRVJgcLoStQCv`, is the `witan-task` skill's own
old instruction to pass the session id as `assignee` — which replaces the
holder rather than qualifying it, recording a session and no person. Fixed in
the skill as part of this change.

Caveat on the number: this counts *current* assignee values on task nodes, not
claim events. A task claimed unqualified and later re-claimed with a session id
shows only the qualified holder. 13 is a floor on the incidence, not a rate.

**Decision: (d) + (a) — make it observable, and tell callers.** `task_claim`'s
success return gains `"qualified"`, plus a `"warning"` when a deployed call
defaulted the holder and got no session id. An explicit `assignee` is exempt: a
worker name is a deliberate choice with nothing to fix. The `witan-task` skill
now tells callers to pass `session_id` and specifically not to put it in
`assignee`.

**Not refused, deliberately.** Refusing an unqualified deployed claim would
break every caller that has no id to send, to prevent a collision that only
occurs when that same person runs concurrent sessions. Silence was the harm;
the claim still succeeds and now reports its own weakness.

**(b) rejected on evidence, not assumption.** FastMCP does expose
`get_http_headers`, so a server-visible per-connection value is reachable in
principle. It does not help: no MCP client sends the agent's session id as a
header, so (b) needs the same client cooperation (a) does, plus header plumbing
through APISIX. (c) — a server-side registry — reintroduces the state ADR-0009
removed.

Re-run the measurement above to decide whether (b) ever becomes worth it. The
signal is an assignee with no `#` that is identity-shaped rather than
`cfg.author`-shaped.

## Addendum (2026-10-16): the claim is a store-level compare-and-swap

The premise above no longer holds. omnigraph #470 added
`/mutate/if-graph-commit` (the CLI's `--if-commit`): the write applies only
while the branch head still equals the `graph_commit_id` the caller read, and
otherwise answers 412 having written nothing. Upstream compares against the
same pinned view the write executes on and re-checks under the branch gate
before effects (`mutate_as_with_expected_head`,
`crates/omnigraph/src/exec/mutation.rs`, v0.13.0). Against a local 0.13.0
server, a write fenced on the current head returned 200 with
`commit.parent_commit_id` equal to the fence, and the same write fenced on the
previous head returned 412 with `precondition_failure {expected, actual}`.

`task_claim` has stated that precondition since it landed, but it still decided
claimability on an earlier, unfenced read, and it kept part 3 plus a loop that
retried the verification read until its commit caught up with the claim's own.
That left a gap: a rival that claimed between the first read and the fenced one
was overwritten by a write whose precondition held, and both callers could be
told `claimed: true`. The verification read could not see it, because our claim
was by then the last write.

The claim now decides claimability on the row its conditional write is fenced
on (`_update_task`'s `require`), so a successful write proves nobody claimed,
closed or blocked the task between that read and the commit. Part 3 and the
catch-up loop are gone. A rival whose claim lands later either read ours and is
refused, or fenced on an older head and gets a 412.

That alone did not protect a granted claim. `update_task` sets the whole row,
and every other caller of `_update_task` (`task_update`, `task_close`,
`task_release`, `task_link`, `task_unlink`, the unblock sweep and
`migrate_repo_keys`) wrote it unconditionally from its own earlier read. A
description edit that read the row before a claim landed put back
`status=open`, `assignee=None` and `claimed_at=None`, and a second claimer was
then granted the task too; review reproduced exactly that over the CLI. So
every `_update_task` write is now fenced on the read it merged from, including
the composed row-plus-edge batch (`change_many(if_commit=...)`, witan-core
0.42.0). A refused write re-reads and merges again, up to five attempts with
jittered backoff, and callers whose change depends on the row (`blocked_by`
appends, the release holder check, the unblock sweep) recompute it on each
attempt's read. A read with no `graph_commit_id` raises rather than falling
back to an unconditional write, since both transports report one on 0.13.

The trade is availability. The fence is the whole branch head, so every task
write, not only a claim, can be refused by an unrelated write and can run out
of attempts with `WriteContention` (nothing written; retrying is safe).
Measured on production (`witan_tool_calls_total` and
`witan_tool_duration_milliseconds`, the 7 days to 2026-10-16): council-graph
write tools peaked at about 9 calls a minute as a 5-minute rate and averaged
0.15, and p95 tool duration was about 5s for `task_update` and `task_comment`
and at the 10s bucket cap for `task_close` and `task_claim`. At that peak one
attempt has roughly an even chance of refusal and exhausting five a few
percent; off-peak it is negligible. We judged that better than a claim that
reverts silently. A row-scoped guard (a mutation matching on `slug` and
`updated_at`) would not be refused by unrelated writes; it is unexplored.

Two tools make more than one write, and a later one can run out after an
earlier one committed. `task_close` (and `task_update` to `closed`) commits the
close, then reopens each dependent separately; a dependent that runs out is
skipped, the rest are still swept, and the closed task comes back with a
`warning` naming it (it is still ready, since every blocker is closed, and the
next sweep retries it). A retried close keeps the original `closed_at`.
`task_unlink(kind="parent")` deletes the edge, then clears `parent_slug`
whenever the row still names that parent, so re-running it repairs a sync that
ran out.

`store_merge` (`witan migrate merge`) was the last task writer outside the
fence. It exports the target, reconciles newest-record-wins, then writes, and
on a served store its write lock is a no-op, so a claim landing between the
export and the write was overwritten by a winning source row. `omnigraph load`
takes no precondition in 0.13, so a winning Task row the target already holds
is no longer loaded. It is written by the `merge_task` mutation (every field,
`author` and `created_at` included), in batches of up to 32 tasks and 96 KiB,
each fenced on a re-read of its tasks through the same retry as
`_update_task`. On
that read a task whose `status`, `assignee` or `claimed_at` changed since the
export keeps the target's row (its decision becomes `kept-target` with
`fenced: claim_changed`), and any other task has newest-record-wins re-run
against it (`fenced: target_newer` when the target now wins). A Task the target
did not hold at the export, and every other row type, is still loaded
unfenced. Nobody can hold a task the target does not have, and slugs carry a
random suffix, so only another merge of the same row can write that slug in
the window. The cost is a commit per batch of collisions, and a batch can run
out of attempts like any fenced write (`WriteContention`; re-running the merge
is safe). Those bounds come from measurements on 0.13.0 (`witan/merge_bounds.py`
has them): a local omnigraph-server took 0.6s for a fenced commit of 32
statements and 2.7s for 128, and refused a mutate body over about 1 MiB. The
CLI could not pass 128 at all at first, because the composed source outgrew
Linux's 128 KiB cap on one argv element, and a single task with a 200 KiB
description failed the same way as `--params`. witan-core 0.44.1 hands the CLI
its source and params as files (`--query <path>`, `--params-file`), so argv no
longer bounds a batch and the caps rest on commit time and the server's body
limit. `store_merge` runs inside one request the gateway cuts
off at 120s, so one call writes at most one such batch: the client splits its
batches to fit, and the server refuses a batch that does not, before writing
anything and on a dry run too (`MergeBatchTooLarge`, a refusal rather than an
error). A client older than witan-council 0.43.0 sends 2 MiB batches, so it is
refused whenever a batch would overwrite more than 32 tasks or more than
96 KiB of them, and the refusal
tells it to upgrade. A call is then a load and a fenced write, two commits, and one that
fails after its load leaves the load applied.

Outside the guarantee:

- Writes meant to change the holder: `force`, a `task_update` that sets
  `assignee` or `status`, `task_release --force`, and a claim taken once the
  lease lapses. Each is decided on the current row; it overrides the claim
  deliberately rather than by accident.
- Holders that compare equal (the missing `session_id` case above).
- Writes that bypass `_update_task`. The partial `set_task_author` update sets
  only `author` and `updated_at`, so it cannot revert a claim.
- A claim whose response never arrives. From 0.12 the server keeps running an
  admitted write after its caller disconnects. Against the local 0.13.0
  server, a mutate whose connection was reset 2 ms after sending still landed,
  and one reset before the server routed it did not, so a client timeout or a
  lost response leaves the outcome unknown. The transport does not re-send a
  write that failed mid-flight, and upstream has no durable request-result
  lookup in 0.13, so the caller re-reads before retrying.
- An in-doubt publish. The 0.12 publisher reads a commit back when its
  acknowledgement is lost and returns success if it landed. Only a read-back
  that cannot complete surfaces, as a 500 `internal` whose message names the
  commit ("manifest publish outcome is in doubt: graph commit … may be
  durable"), and it is not retried.
