### Added

- `apply` now surfaces a platform's MCP prerequisite
  (`AgentPlatform.mcp_conditional_on`) whenever it plans MCP entries for that
  platform, on dry-run and real runs alike, as `InstallResult.prerequisites`
  (a list of the new `Prerequisite`). `agent-kit apply` prints it after the
  results table. For Pi, a read-only preflight
  (`AgentPlatform.mcp_prerequisite_check`) looks for `pi-mcp-adapter` in
  `~/.pi/agent/settings.json` (plus `.pi/settings.json` at project scope):
  when it is missing, the CLI prints a yellow warning with
  `pi install npm:pi-mcp-adapter`; when it is found, a dim note. The preflight
  never runs `pi`/`npm` or touches the network, and neither outcome changes
  the exit code. It decodes settings files the way Pi does (invalid bytes
  replaced, BOM stripped), and a file it cannot open is reported as
  unreadable instead of aborting `apply` or claiming the adapter is missing.

### Changed

- Pi's `mcp_conditional_on` text now names pi-mcp-adapter as the one package
  that reads the files agent-config-kit writes.
