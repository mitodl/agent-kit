### Fixed

- **Parallel agents in one worktree no longer race each other's code-graph
  writes.** The PostToolUse `reindex-hook` indexed each edited file in the
  foreground, one process per Edit/Write, so a session fanning out subagents
  in one worktree put that many uncoordinated writers on one branch view.
  Against a deployed graph they lost each other's optimistic-concurrency races
  until the retry budget ran out (Sentry WITAN-12). The hook now queues the
  path and a single detached drainer per checkout applies the queue, indexing
  each queued file once however many times it was edited. The drainer shares
  the SessionStart index's lock, so a full index and per-edit reindexes never
  write at the same time either, and edits queued during a full index are
  applied when it finishes.

  The index now lands a few seconds after the edit instead of before the hook
  returns. Outside a git checkout the hook still indexes in the foreground.

  The lock now records its holder's pid, so a lock left by a killed indexer is
  cleared by the next hook instead of blocking that checkout's indexing until
  someone removes it by hand.
