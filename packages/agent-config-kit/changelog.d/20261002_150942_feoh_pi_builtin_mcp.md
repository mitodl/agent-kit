### Changed

- Pi MCP entries are written in the format of Pi's built-in MCP support (Pi
  0.99 and later), which pi-mcp-adapter 5.x also reads from the same files.
  An OAuth server no longer gets `"auth": "oauth"`, which both reject, so they
  skipped the whole entry. A manifest's `oauth.callbackPort` becomes
  `oauth.callbackUrl: "http://localhost:<port>/callback"`, the same redirect
  URI as before, and a manifest's `oauth.redirectUri` becomes
  `oauth.callbackUrl`.
- The Pi MCP preflight now counts Pi's built-in MCP: `apply` warns only when
  pi-mcp-adapter is not declared and Pi's settings turn the built-in off
  (`-builtin:mcp`, which pi-mcp-adapter adds and leaves behind when removed)
  or record a Pi older than 0.99 as the last one run
  (`lastChangelogVersion`). The warning says how to turn the built-in back
  on. `adapters.pi.mcp_adapter_prerequisite` is renamed `mcp_prerequisite`.
