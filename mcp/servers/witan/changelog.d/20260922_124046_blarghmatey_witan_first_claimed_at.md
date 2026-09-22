### Added

- **`Task.first_claimed_at`: when the work actually started.** `claimed_at`
  could never answer that. `task_claim` rewrites it on every lease renewal and
  the lease is an hour, so any task worked for longer than that has already
  lost its first claim; `task_release` nulls it outright. The new field is set
  on the first arrival at `in_progress` — through `task_claim` or
  `task_update(status="in_progress")` — and is then never cleared, by a
  release, a close or a reopen. It is returned by `task_get` and every
  `task_list`/`task_ready` projection.

  **No backfill.** A task last claimed before this ships has no first-claim
  time, and anything plotting it should say so rather than substituting
  `claimed_at`, which means something else.

  **Additive nullable property, so it carries the agent-kit#326 ordering
  constraint:** omnigraph's schema apply has to land before the witan image
  that writes it. Deployed, getting that backwards fails the migrations Job
  and blocks the rollout rather than breaking the running service (see
  `les-a-witan-schema-image-ordering-violation-fails-cl-a16abf`); recovery is
  to deploy pulumi-omnigraph and re-trigger the witan deploy. A local store
  needs nothing: `_ensure_graph` re-applies on `schema.pg`'s mtime.

  Verified as an additive migration against a store built from the previous
  `schema.pg` carrying a real `in_progress` Task row: `schema plan` reports
  `supported: yes` with the single `add property` change, apply moves the
  graph manifest to 3, the pre-existing row keeps its data with
  `first_claimed_at` null, and all 113 read queries and 59 mutations lint
  clean against the new schema.

### Changed

- **`closed_at` is now an invariant of `status`.** It recorded "when this task
  last closed", which is not the same thing and could not be plotted. Two
  surfaces produced rows that contradicted their own status: `task_release`
  accepts a `status` that may be `closed` but wrote only status, assignee and
  `claimed_at`, leaving a closed task with no close time at all; and reopening
  through `task_update` kept the old value, leaving an OPEN task carrying one.

  Every transition to `closed` now stamps it and every transition out of
  `closed` clears it, whichever surface makes the move — the rule is derived
  from the merged status inside `_update_task`, which `task_claim`,
  `task_update`, `task_close` and `task_release` all funnel through, rather
  than being restated at each of them. A closed row keeps its original close
  time across unrelated edits.

  Rows already written wrong are not swept: they are corrected the next time
  anything writes them, so a row untouched since then still carries the old
  spelling.

  This supersedes the caveat recorded with "Task list rows carry `created_at`
  and `closed_at`", which said `closed_at` was not yet an invariant of status.
