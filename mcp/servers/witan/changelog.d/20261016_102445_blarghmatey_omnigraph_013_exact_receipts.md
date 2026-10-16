### Fixed

- `task_claim` decides whether a task is claimable on the same read its conditional write is fenced on. It used to decide on an earlier read, so a rival that claimed in between was overwritten by a write whose precondition still held, and both callers could be told `claimed: true`. A rival found on the fenced read is now reported as `lost_race` and nothing is written.

### Removed

- `task_claim`'s post-write verification read, the loop that retried it until its `graph_commit_id` caught up with the claim's own, and the `witan.task_claim.verify` / `witan.task_update.conditional` log lines. A successful conditional write already proves nobody claimed, closed or blocked the task first, so the read-back could only observe a later write that skips the check (a `force` steal, a `task_update`). `witan.commit_ids`, which only that loop used, is gone too.
- The unconditional fallback for a conditional task write whose read reported no `graph_commit_id`. omnigraph 0.13 reports one on both transports, so such a read now raises instead of sending the claim without a precondition.
