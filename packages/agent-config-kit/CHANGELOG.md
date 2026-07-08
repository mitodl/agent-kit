# Changelog

All notable changes to `agent-config-kit` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (pre-1.0:
a MINOR bump may include breaking changes).

## [0.2.1] - 2026-07-08

### Changed

- **Breaking:** the console script is now `agent-kit`, not `ac-kit` — the
  shorter name was easy to mistake for an unrelated tool; `agent-kit`
  matches this repo's own name so there's one less mental mapping between
  "the repo I'm in" and "the command I run". No backwards-compat alias —
  `uv tool install 'agent-config-kit[cli]'` now installs only `agent-kit`.

## [0.2.0] - 2026-07-08

### Added

- **Profiles**: `[profiles.<name>]` manifest tables select a named,
  stackable subset of a manifest's `skills`/`mcp_servers`/`hooks`/
  `lsp_servers` by key, with `inherits` to compose profiles together.
  Validated at load time — an unknown key reference or an inheritance
  cycle raises `ManifestError` immediately, not just when that profile
  is selected. New `resolve_profile()` API, `--profile NAME` (repeatable)
  on `ac-kit apply`/`ac-kit validate`, `[options] default_profiles`, and
  a new `ac-kit profiles [MANIFEST]` command listing each profile's
  resolved entry counts.
- **Global config file**: `ac-kit config init` bootstraps
  `${XDG_CONFIG_HOME:-~/.config}/agent-config-kit/config.toml`
  (overridable via `--config`/`AC_KIT_CONFIG`) — non-interactively, with
  every optional key written as a commented-out example, or via
  `--wizard` for an interactive prompt flow. `agent_config_kit.config`'s
  `load_global_config()`/`GlobalConfig` model holds `default_manifest`,
  `default_profiles`, per-org (`[[org]]`) and per-directory-prefix
  (`[[scope]]`) manifest/profile defaults, in preparation for zero-argument
  `ac-kit apply` resolution.
- **Project-scope install targets**: the platform registry now populates
  a `project` `ScopeTarget` (relative to the repo root) for every platform,
  not just `global` — `claude` (`.mcp.json`, `.claude/settings.json`,
  `.claude/skills`), `pi` (`.pi/settings.json`, `.pi/extensions`,
  `.pi/skills`), `copilot` (`.vscode/mcp.json`, new `.github/skills`
  capability), and `opencode` (`opencode.json`, new `.opencode/skill(s)`
  capability). Makes `ac-kit apply --scope project` actually write
  somewhere on every platform.
- Remote (`https://`/`http://`/`git+`) URIs are now accepted directly in
  a manifest's `skill_md_path`/`entry_path` fields, fetched and cached
  under `.agent-config-kit-cache/` (or `--cache-dir`) with conditional-GET
  staleness checks and offline fallback to the last-good copy.

### Changed

- **Breaking:** manifest `[skills]` is now a name-keyed table
  (`name = "path"`, or an inline table for future per-skill fields),
  replacing the `[[skills]]` array-of-tables form. No backwards-compat
  shim — update manifests to the new form. `hooks = [ {...}, {...} ]`
  (inline array-of-tables) is also now accepted as an equivalent to
  repeated `[[hooks]]` headers.

### Fixed

- A manifest's `[skills]` value being the wrong shape (e.g. the dropped
  `[[skills]]` list form) now raises a clean `ManifestError` instead of
  crashing with `AttributeError`.
- The global config's `~`-expansion no longer crashes with `TypeError`
  on a non-string value (e.g. `default_manifest = 123`); it now surfaces
  as a normal `ConfigError` from validation.
- `resolve_profile()`'s returned hook/skill order now follows the
  manifest's own declaration order deterministically, rather than the
  process-randomized iteration order of an internal `set`.
- `fetch.py`: guard a corrupted/partial HTTP cache sidecar (fall back to
  an unconditional GET instead of crashing manifest loading); fixed
  `git+` ref parsing for SCP-like URIs with no `/` (e.g.
  `git+git@host:repo.git@v1.0.0`).

## [0.1.0] - 2026-07-02

Initial release: canonical cross-platform models (`AgentPlatform`,
`McpServer`, `Hook`, `SkillSource`, ...), the `agent-config.toml` manifest
loader, `ac-kit apply`/`ac-kit validate` against Claude Code, Pi, GitHub
Copilot, and OpenCode, and `apply --prune` state tracking.
