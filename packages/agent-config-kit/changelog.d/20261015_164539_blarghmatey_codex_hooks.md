### Added

- Declarative hooks for Codex CLI, written to `$CODEX_HOME/hooks.json`
  (global scope only) and merged, deduped and pruned the same way as Claude
  Code's. Timeouts are rounded up to whole seconds, since Codex rejects a
  fractional one. Codex runs a hook only after the user trusts it, and
  agent-config-kit never writes that trust state, so `agent-kit apply` prints
  one line per Codex hook it adds or changes, telling the user to trust it
  with `/hooks` in the Codex TUI (`InstallResult.notices`). Hooks are on by
  default from Codex 0.124.0; an older Codex needs `[features] codex_hooks =
  true`.
- An optional `skill` field on a declarative hook. `{skill_dir}` in its
  `command` is replaced with the directory each platform installs that skill
  into, so one manifest entry runs a skill's script on Claude Code and Codex
  alike. A hook naming a skill that isn't in `[skills]` fails to load, and
  `apply`/`validate` exit `2` before writing anything when the run doesn't
  install that skill for a platform the hook goes to (`HookSkillError`).

### Changed

- `apply_all` (and so `witan setup`) now writes a bundle's declarative hooks
  to Codex's `hooks.json` when Codex is detected, where before it wrote none.
