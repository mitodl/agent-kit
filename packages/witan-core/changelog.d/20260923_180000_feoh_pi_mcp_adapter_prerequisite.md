### Added

- `witan_core.cli.report_install` prints an `InstallResult`'s
  `prerequisites` (agent-config-kit's platform-prerequisite preflight, e.g.
  Pi's pi-mcp-adapter check) as a warning or a note under the platform's
  paths. It reads the field with `getattr`, so results from an older
  agent-config-kit without it still print as before.
