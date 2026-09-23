### Fixed

- Project-scoped Pi MCP registration (`apply --scope project`) now writes to
  `.pi/mcp.json`, the Pi project override pi-mcp-adapter actually reads,
  instead of `.pi/settings.json` (Pi core's own settings file, which the
  adapter never reads MCP servers from, so project-scoped Pi MCP entries were
  a silent no-op). `validate` and `apply --prune` read and prune the same
  file. The global target (`~/.pi/agent/mcp.json`) and the project
  extensions/skills targets (`.pi/extensions`, `.pi/skills`) are unchanged.
  Entries previously written to `.pi/settings.json` are not migrated; remove
  its `mcpServers` key by hand.
