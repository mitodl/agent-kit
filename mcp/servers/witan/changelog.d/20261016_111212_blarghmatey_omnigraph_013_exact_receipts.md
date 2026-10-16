### Fixed

- Every task write is fenced on the read it merged from. `update_task` replaces the whole row, and `task_update`, `task_close`, `task_release`, `task_link`, `task_unlink`, the unblock sweep and `migrate_repo_keys` wrote it unconditionally, so an edit that read a task before a claim landed put back `status=open` and no assignee, and a second caller was then granted the same task. Each write now states its read's `graph_commit_id` and, when the store refuses it, re-reads and merges again (up to five attempts with jittered backoff, then `WriteContention` with nothing written). Changes that depend on the row (`blocked_by` appends, the release holder check, the unblock sweep) are recomputed on each attempt's read.
- `task_release` checks the holder on the row its write is fenced on, so a claim someone else took after the caller's read is refused rather than released.
- `task_close` (and `task_update` to `closed`) no longer fails after its close committed. Reopening each dependent is its own fenced write; one that runs out of attempts is skipped, the rest are swept, and the closed task comes back with a `warning` naming the dependents still marked blocked (they are still ready). A retried close keeps its original `closed_at`.
- `task_unlink(kind="parent")` clears `parent_slug` whenever the row still names that parent, so re-running it repairs a sync that ran out of attempts after the edge was deleted.
- `task_claim` on a stale-blocked task whose reopen runs out of attempts reports `contention` instead of raising.
- `migrate_repo_keys` that runs out of attempts on a task says it is safe to re-run and reports the counts done so far, and no longer counts a task another writer had already folded.
- A `task_claim` that runs out of attempts reads the task once more and reports `lost_race` or `closed`/`blocked` when that is why, instead of `contention` with a remedy that asserted what it had not checked.

### Changed

- Requires witan-core 0.42 for `change_many(if_commit=...)`, which fences a task row and its edge in one commit.
