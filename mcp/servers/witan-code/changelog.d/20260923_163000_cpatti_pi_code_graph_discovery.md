### Added

- **`witan-code inject-context --client pi`.** The status block told every
  agent to run Claude's `ToolSearch` and then call `code_*` tools directly,
  including under Pi, which has no `ToolSearch` and reaches MCP tools only
  through pi-mcp-adapter's `mcp` proxy under a server-prefixed name. With
  `--client pi` the block instead says to find the exact name with
  `mcp({ search: "code_find_definition callers impact" })` and call it with
  `mcp({ tool: "<name>", args: {...} })`, and points at `/skill:witan-code`.
  The default (`--client claude`, what the Claude hook runs) is unchanged.

### Changed

- **The Pi extension passes `--client pi`**, so a Pi install needs a
  `witan-code` CLI from this release or later: an older one rejects the flag
  and the extension, as with any failure, injects no block.
- **The `witan-code` skill's tool reference is client-neutral**, with short
  notes on reaching the tools from Claude Code (`ToolSearch`) and Pi (the
  `mcp` proxy).
