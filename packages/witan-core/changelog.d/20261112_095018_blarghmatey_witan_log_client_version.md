### Added

- The `mcp.tool_call` log line carries `client_name` and `client_version`, the calling client's self-reported MCP `clientInfo`. A post-release check can now tell an un-upgraded client from a fix that didn't work. A client that never set its own reports the SDK default, `mcp 0.1.0`.
