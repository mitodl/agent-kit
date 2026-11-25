### Added

- `witan.harness`: the adapter layer fan-out runners will start workers
  through. `ClaudeAdapter` (`claude -p --output-format stream-json`) and
  `PiAdapter` (`pi -p --mode json`) start a session in a worktree, report the
  harness session id as a `SessionRef`, and resume it on the same host. Each
  normalizes its harness's output into ACP v1 session updates and appends
  them to a per-session JSONL event log, with witan's fields under `_meta`.
