### Added

- The `mcp.tool_call` log line carries `client_name` and `client_version`, the calling client's self-reported MCP `clientInfo`. A post-release check can now tell an un-upgraded client from a fix that didn't work. Both strings are capped at 128 characters. A client that never set its own reports the SDK default, `mcp 0.1.0`.
- The remote proxy (the witan and witan-code CLIs' remote mode) names itself `witan-remote-proxy <witan-core version>` instead of `mcp 0.1.0`, so its calls aren't mistaken for an un-upgraded witan-code's.
