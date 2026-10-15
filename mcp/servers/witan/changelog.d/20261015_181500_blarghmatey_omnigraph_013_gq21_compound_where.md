### Fixed

- `task_unlink` and `workflow_project_unblock` delete exactly the one edge
  named, with a single delete on both endpoints. They used to delete every
  edge on one endpoint and re-insert the others, so a concurrent reader could
  see those edges missing and a failure between the two steps lost them.

### Changed

- Blocker checks in `task_create`, `task_ready`, `task_unlink` and the
  unblock pass after `task_close` read the status of every blocker they need
  in one query instead of one `get_task` per blocker.
  `workflow_project_get_blockers`, `recall`'s contradiction pass and
  `witan migrate topics` do the same for their per-slug reads.
