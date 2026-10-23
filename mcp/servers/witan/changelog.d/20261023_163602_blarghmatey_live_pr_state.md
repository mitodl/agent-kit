### Added

- `task_get` and `workflow_project_get` add `live_pr` when the record's
  `external_uri` (task) or `github_pr` (project) is a GitHub PR or issue: its
  state, draft flag, review decision and check counts, fetched just now with
  your own `gh`, plus `fetched_at`. It is added on your machine, by the stdio
  server or by `witan serve`'s forwarder to a deployment; the deployment's own
  response is unchanged. A failed lookup reports `{"error": ...}`.
- The context hook shows the same state as a `(live, HH:MM)` line under each
  shown project with a `github_pr`, each in-flight-branch task, and each held
  task with a linked PR (in a new `## Work You Hold` block when no other block
  names the task). At most 5 lookups per render, run concurrently under the
  hook's shared deadline. A lookup that fails or misses it renders nothing.
- `witan task show`, `witan project` and `witan project status` print the live
  line too, and `witan project status` prints the project's `updated_at`.
- Comments in the hook's `New Comments on Work You Hold` block carry their
  date.
- `WITAN_LIVE_PR=0` turns the lookups off. Answers are cached per URL for 120s.

### Changed

- `workflow_project_list` rows carry `github_pr` and `task_for_branch` rows
  carry `external_uri`, which the hook needs to find the linked PR.
- The witan-task skill says stored descriptions and comments are snapshots and
  that `live_pr` wins where they disagree.
