### Added

- `$WITAN_SESSION_ID` is read ahead of `$CLAUDE_SESSION_ID`, `$PI_SESSION_ID`
  and `$CODEX_SESSION_ID` wherever witan resolves the agent session id (the
  claim holder, the CLI proxy's `session_id`, the parked workflow-session
  handle). A runner sets one per worker slot, so parallel workers under one
  author get distinct claim holders on any harness, including Copilot and
  OpenCode, which export no id of their own. A launch spec's `unset_env` now
  lists it, and the launcher strips an inherited one before it claims.
