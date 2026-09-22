### Fixed

- **Parallel agents in one worktree no longer race each other's code-graph
  writes.** The PostToolUse `reindex-hook` indexed each edited file in the
  foreground, one process per Edit/Write, so a session fanning out subagents
  in one worktree put that many uncoordinated writers on one branch view.
  Against a deployed graph they lost each other's optimistic-concurrency races
  until the retry budget ran out (Sentry WITAN-12). Both hooks now put their
  target on a per-checkout queue (SessionStart queues the project dir,
  PostToolUse the edited file) and a single detached drainer applies it,
  indexing each queued target once however many times it was queued.

  The index now lands a few seconds after the edit instead of before the hook
  returns. Outside a git checkout the hook still indexes in the foreground.

  The per-checkout state is keyed on the git toplevel rather than
  `CLAUDE_PROJECT_DIR`, so a session started in a monorepo subdirectory shares
  its edits' lock. A SessionStart refresh that finds an indexer already
  running is queued rather than dropped.

  The lock is now a kernel `flock` handed to the drainer, so a killed indexer
  no longer leaves the checkout locked until someone removes the lock by hand.
  The lock, the queue and the last drainer's log live in a private 0700
  `$TMPDIR/witan-code-<uid>/` rather than loose in a shared `/tmp`.
