### Added

- Against a deployed witan, `task_comment`, `task_create`, `task_update`,
  `task_close` and `task_release` are queued locally when the deployment
  cannot be reached and replayed on the next call that reaches it. The agent
  gets `{"buffered": true, "journal_id", "pending", "note"}` instead of an
  error. `task_update`, `task_close` and `task_release` are also queued after
  a gateway 502/504, since applying them twice is harmless. Reads and
  `task_claim` still fail outright.
- On replay, a queued update, close or release of a task someone else now
  holds, or that was closed since, is posted as a comment on the task instead
  of applied. A queued `task_create` is skipped if a task with the same title
  was created since.
- `witan journal list|replay|drop` shows, replays, or discards queued writes,
  and the context hook prints a line when any are queued.

### Changed

- Requires witan-core 0.44.
