### Added

- `task_search` takes `limit` (default 20, at most 10,000). Agents were
  passing it and getting a validation error. A limit above 20 reads through
  the uncapped search queries, since the default pair caps each half at 20 in
  the query. Those queries apply `status` and `repo` (and the project
  queries `status`) before their own cap, so matches outside the requested
  scope cannot crowd out the ones inside it. `witan tasks QUERY` does not send
  `limit` yet, so it keeps working against a deployment that predates the
  parameter, and `--limit` does not apply to a search.

### Changed

- **BREAKING: `task_comment`'s `text` parameter is now `body`, and `witan task
  comment`'s `--text` flag is now `--body`.** `body` was the name agents
  guessed (48 wrong-parameter errors in about 30 days of transcripts), and it
  is the field name `task_get` already returns comments under. No alias is
  kept, so a client and the deployment it talks to must both be on this
  version or both be older. A mismatch either way fails with a validation
  error, and that includes a `task_comment` queued in the offline journal and
  replayed across the upgrade, which replay drops as refused (with a logged
  warning). When an older client replays a queued task write that no longer
  applies, it posts a conflict note on the task with `text`, which an
  upgraded deployment refuses too, so replay reports that write as lost.
- `workflow_session_end`'s `session_slug` description now says it is the `ws-`
  slug from `workflow_session_start` and not the agent's own session id, which
  agents were passing as `session_id`.
