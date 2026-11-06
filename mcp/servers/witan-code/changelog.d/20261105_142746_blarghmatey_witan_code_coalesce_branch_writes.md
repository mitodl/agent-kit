### Changed

- The PostToolUse drainer indexes its whole queue as one batch
  (`indexer.index_paths`) and waits `FLUSH_INTERVAL_SECONDS` (30s) since the
  checkout's last flush before starting the next. An edit-heavy session now
  runs at most one index per 30s per checkout (three commits on the branch
  view when the batch is at most 64 changed files and fits one load chunk),
  instead of one index run per edit back to back. The first edit after a quiet spell still flushes at once, and edits
  made during the wait join the next flush, so the last edit of a session is
  still indexed.
- The Pi extension queues edits through `witan-code reindex-hook` and seeds
  through `witan-code session-init`, the commands the Claude and Codex hooks
  run, instead of a detached `witan-code index` per edit that bypassed the
  drainer.

### Removed

- `RemoteStoreClient.change_many` no longer falls back to one
  `code_store_mutate` per statement when the deployment's tool list lacks, or
  fails to return, `code_store_mutate_many`. A failed listing was cached for
  the connection's lifetime, which turned a reindex's deletes into one commit
  per statement. `StoreSession.has_tool` is gone with it.
