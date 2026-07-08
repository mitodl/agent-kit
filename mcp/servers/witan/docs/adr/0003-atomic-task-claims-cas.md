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
