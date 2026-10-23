### Changed

- `apply` now keeps its state files (`lock.json` for `--prune`, `applied.json`
  for staleness detection) under
  `${XDG_STATE_HOME:-~/.local/state}/agent-config-kit/`, in a directory keyed
  by the manifest's resolved path and, for `project` scope, the directory
  `apply` ran from. They used to sit next to the manifest
  (`<manifest>.lock.json`, `<manifest>.applied.json`), so every apply left an
  untracked file in the repo the manifest is committed to, or at the repo root
  (`.agent-config-kit-state.json`, `.agent-config-kit-applied-state.json`) for
  a project-scope apply of a manifest from outside the repo. The first
  non-dry-run `apply` after upgrading reads the old file when no new one
  exists yet and deletes it after writing the new one, so `--prune` still
  knows what it wrote before. `validate` reads the old file too but never
  deletes it.
- `default_state_path()` and `default_applied_state_path()` take the write
  `scope` as a second argument. `agent_config_kit.prune.state_dir()` is new
  and returns the directory both live in.

### Fixed

- A relative `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` or `XDG_STATE_HOME` is now
  ignored, as the XDG Base Directory spec requires, instead of resolving
  against the directory `agent-kit` ran from.
