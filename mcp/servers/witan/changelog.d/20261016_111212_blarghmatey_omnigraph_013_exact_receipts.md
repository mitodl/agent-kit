### Fixed

- Every task write is fenced on the read it merged from. `update_task` replaces the whole row, and `task_update`, `task_close`, `task_release`, `task_link`, `task_unlink`, the unblock sweep and `migrate_repo_keys` wrote it unconditionally, so an edit that read a task before a claim landed put back `status=open` and no assignee, and a second caller was then granted the same task. Each write now states its read's `graph_commit_id` and, when the store refuses it, re-reads and merges again (up to five attempts with jittered backoff, then `WriteContention` with nothing written). Changes that depend on the row (`blocked_by` appends, the release holder check, the unblock sweep) are recomputed on each attempt's read.
- `task_release` checks the holder on the row its write is fenced on, so a claim someone else took after the caller's read is refused rather than released.
- A `task_claim` that runs out of attempts reads the task once more and reports `lost_race` or `closed`/`blocked` when that is why, instead of `contention` with a remedy that asserted what it had not checked.

### Changed

- Requires witan-core 0.42 for `change_many(if_commit=...)`, which fences a task row and its edge in one commit.
