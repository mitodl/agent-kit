### Fixed

- The Pi adapter now emits a stdio server's `cwd` and a remote server's
  `headers` (both `ServerEntry` fields in pi-mcp-adapter's `types.ts`)
  instead of silently dropping them, matching the Claude adapter. Unset
  `cwd` and empty `headers` are still omitted, and headers coexist with the
  existing OAuth `callbackPort` to `redirectUri` translation. Header values
  stay redacted in `apply --diff` output.
