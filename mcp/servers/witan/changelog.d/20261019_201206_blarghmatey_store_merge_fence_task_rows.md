### Fixed

- `store_merge` and `witan migrate merge` no longer overwrite a claim that
  lands while they run. A winning source row for a task the target already
  holds is now written by a mutation fenced on a fresh read of that task
  rather than loaded, so a task whose status, assignee or `claimed_at` changed
  since the merge's export keeps the target's row. Its decision comes back as
  `kept-target` with a `fenced` reason (`claim_changed`, or `target_newer` when
  re-running newest-record-wins against the fresh row keeps the target).
