### Added

- `report_install` prints an install result's `notices`, one `⚠ action` line each (`ACTION:` without a rich console). For Codex that is the line saying a newly written hook won't run until it is trusted with `/hooks` in the Codex TUI, which `witan setup` and `witan code setup` never showed before.
- `codex` is an `AgentName`, labelled "Codex CLI", so both servers' `setup --agent codex` installs into Codex alone.

### Changed

- The `cli` extra requires `agent-config-kit>=0.13`, the first release with `InstallResult.notices` and the `codex` platform. `report_install` reads `prerequisites` directly instead of through `getattr`, since every release the floor admits has it.
