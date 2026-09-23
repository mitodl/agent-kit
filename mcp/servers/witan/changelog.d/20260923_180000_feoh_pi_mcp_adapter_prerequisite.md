### Changed

- `witan setup --agent pi` (and `--agent all` when Pi is detected) now warns
  when the pi-mcp-adapter Pi package is not declared in Pi's settings, since
  Pi ignores the witan MCP entry in `~/.pi/agent/mcp.json` without it. The
  warning comes from agent-config-kit's preflight via
  `witan_core.cli.report_install`. The README and the `setup` help document
  the prerequisite (`pi install npm:pi-mcp-adapter`).
