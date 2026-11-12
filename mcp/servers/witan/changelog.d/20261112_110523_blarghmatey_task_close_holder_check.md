### Fixed

- `task_close` now refuses, writing nothing, while another person holds a live
  claim on the task, and answers with `held_by` and a `remedy` naming
  `witan task close <slug> --force`. A worker whose lease lapsed and whose task
  was taken over could close the new holder's work when it finished late, and
  unblock its dependents with it. A claim held by any session of the closer's
  own identity, an expired lease, and a task with no named holder close as
  before. `task_update(status="closed")` gets the same check, so it is not a
  way around it. Both take `force`, `task_close` takes `assignee` for a claim
  made under an explicit name, and `witan task close` / `witan task update`
  take `--force` and exit 1 on a refusal.
