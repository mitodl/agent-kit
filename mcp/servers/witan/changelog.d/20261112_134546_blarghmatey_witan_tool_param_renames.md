### Added

- `task_search` takes `limit` (default 20, at most 10,000). Agents were
  passing it and getting a validation error. A limit above 20 reads through
  the uncapped search queries, since the default pair caps each half at 20 in
  the query. `witan tasks QUERY --limit N` now passes its limit through
  instead of stopping at 20.

### Changed

- **BREAKING: `task_comment`'s `text` parameter is now `body`.** `body` was
  the name agents guessed (48 wrong-parameter errors in about 30 days of
  transcripts), and it is the field name `task_get` already returns comments
  under. No alias is kept, so a client still sending `text` gets a validation
  error from a deployment running this version. That includes an older
  `witan` CLI (`witan task comment`) and a `task_comment` an older client
  queued in its offline journal, which replay drops as refused (with a
  logged warning).
- `workflow_session_end`'s `session_slug` description now says it is the `ws-`
  slug from `workflow_session_start` and not the agent's own session id, which
  agents were passing as `session_id`.
