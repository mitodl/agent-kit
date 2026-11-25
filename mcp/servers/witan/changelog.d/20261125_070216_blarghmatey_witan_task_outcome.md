### Added

- `task_record_outcome` (and `witan task outcome`) records how a claimed
  attempt ended: `ready_for_review` with the branch, commits and acceptance
  check output, `needs_human` with the question, or `cannot_proceed` with the
  reason. Only the claim's holder may record it (the exact session, since
  fan-out workers are one person in several sessions), `force` overrides, and
  it ends the claim. The holder and time are kept as `outcome_set_by` and
  `outcome_set_at`. `task_get` and `witan task show` return the outcome, and
  task listings carry `outcome`.

### Changed

- A task carrying an outcome waits on a person: `task_ready` and the context
  hook leave it out, and `task_claim` refuses it (`reason: "outcome"`) unless
  forced. `task_release` requeues it and clears the outcome (`cleared_outcome`
  names it), except when releasing to `closed`. A claim or a reopen from
  `closed` clears it too; `task_close` keeps it. A task write queued in the
  journal before an outcome was recorded replays as a comment instead of
  clearing it.

### Notes

- The `Task` schema gains seven optional `outcome*` properties. A deployed
  graph needs the schema applied before the image that writes them.
