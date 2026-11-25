### Added

- `$WITAN_SESSION_ID` is read ahead of `$CLAUDE_SESSION_ID`, `$PI_SESSION_ID`
  and `$CODEX_SESSION_ID` for a task claim's holder and the CLI proxy's
  `session_id`. A runner sets one per worker slot, so parallel workers under
  one author get distinct claim holders on any harness, including Copilot and
  OpenCode, which export no id of their own. The parked workflow-session
  handle reads it last, after the harness ids, so the Stop hook and the Pi and
  Codex checkpoints still find a handle keyed by the harness id. The MCP
  instructions and the `witan-task` skill say to pass it on claims when set. A
  launch spec's `unset_env` now lists it, and the launcher strips an inherited
  one before it claims.
