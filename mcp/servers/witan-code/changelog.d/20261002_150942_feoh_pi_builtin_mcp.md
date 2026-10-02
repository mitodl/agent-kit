### Added

- `witan-code inject-context --client pi-builtin` renders the code-tool
  discovery line for Pi's built-in MCP: a `codemode` script that finds the
  `mcp__<server>__code_*` name with `searchTools()` and calls it as
  `tools.<name>()`. The Pi extension passes it unless pi-mcp-adapter's `mcp`
  tool is registered, in which case it still passes `--client pi`, whose
  output is unchanged.

### Changed

- The `witan-code` skill describes calling the `code_*` tools under Pi's
  built-in MCP as well as under pi-mcp-adapter, and `witan-code setup --agent
  pi` no longer warns that pi-mcp-adapter is missing when Pi's built-in MCP is
  on (with an agent-config-kit release that includes this change).
