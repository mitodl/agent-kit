### Changed

- Breaks older clients: upgrade witan-council before merging into a
  deployment running this release. One `store_merge` call now writes at most 32 tasks
  the deployment already holds (and 96 KiB of them), and the server refuses a
  batch whose collisions need more with `MergeBatchTooLarge`, before writing
  anything. A dry run is refused the same way. `witan migrate merge` from this
  release splits its batches to at most 32 Task rows per call, so it never
  hits the refusal. Older clients send batches of up to 2 MiB, so they are
  refused on every run that would overwrite more than 32 tasks with newer
  copies; the refusal names this version and the upgrade command
  (`uv tool upgrade ol-agent-kit`, or `uv tool upgrade witan-council`).

### Fixed

- `store_merge` and `witan migrate merge` no longer overwrite a claim that
  lands while they run. A winning source row for a task the target already
  holds is now written by a mutation fenced on a fresh read of that task
  rather than loaded, so a task whose status, assignee or `claimed_at` changed
  since the merge's export keeps the target's row. Its decision comes back as
  `kept-target` with a `fenced` reason (`claim_changed`, or `target_newer` when
  re-running newest-record-wins against the fresh row keeps the target).
  Those writes go in fenced commits of at most 32 tasks and 96 KiB, sized from
  measurements against omnigraph 0.13.0 (`witan/merge_bounds.py`).
