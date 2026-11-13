### Added

- Every tool carries MCP annotations: `readOnlyHint` on reads, and on writes
  `destructiveHint` (true for deletes, closes, merges, field overwrites and
  link removal) plus `idempotentHint` where a repeat changes nothing more.
  Clients can auto-approve the reads. `witan serve` against a deployed target
  republishes the deployment's annotations.

### Changed

- Tool and parameter descriptions are cut to what an agent needs to choose a
  value. Text several tools repeated (the agent session id, `session_slug`,
  `assignee`, slug prefixes, `ui_url`, `live_pr`, `lease_expired`, write
  contention) now lives once in the server instructions. The agent-facing
  manifest drops from 94,561 to 62,231 characters.

### Fixed

- `witan serve` against a deployed target sent no server instructions, so
  every "see instructions" in a tool description pointed at nothing. It now
  sends them, and appends witan-code's instructions when it mounts the
  `code_*` tools, which `mount` alone dropped.
