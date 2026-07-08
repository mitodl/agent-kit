# Changelog

All notable changes to `agent-config-kit` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (pre-1.0:
a MINOR bump may include breaking changes).

## [0.3.2] - 2026-07-08

### Fixed

- **`--prune` state-file identity for a shared/resolved manifest**
  (O-STATE): a manifest resolved via `[[org]]`/`[[scope]]`/
  `default_manifest` typically lives outside the repo it's applied
  into — a shared bundle referenced from many repos. The previous
  manifest-adjacent `<manifest>.lock.json` default meant every repo
  applying that same shared manifest with `--scope project` silently
  shared (and clobbered) one state file, corrupting what `--prune`
  believed it had safely written to each repo's own project-scope
  targets. `apply --prune` now defaults to a repo-scoped
  `<repo>/.agent-config-kit-state.json` whenever the effective write
  scope is `project` and the resolved manifest isn't already inside the
  repo being applied into; a manifest that already lives in the repo (an
  explicit local path, or the repo-local zero-arg case) is unaffected.
- Rich console markup was silently swallowing literal `[[org]]`/
  `[[scope]]` in the "no MANIFEST given" zero-arg error message (`[...]`
  is markup syntax) — properly escaped now.

### Added

- **Precedence/layering documentation**: README sections for the global
  config file's full `[[org]]`/`[[scope]]` schema and a "Precedence &
  resolution order" summary covering both which-manifest resolution (§7.2)
  and within-manifest layering (§5.3). Spec §9's open questions (O-DEFAULT,
  O-INSTR, O-MEM, O-STATE, O-PRIORITY) are all resolved and documented in
  place rather than left open.
- Integration tests (`tests/test_integration.py`) exercising the full
  feature set together through the CLI: profile stacking with `inherits`
  + top-level `include`, include-cycle errors surfacing cleanly, zero-arg
  apply resolving via org/prefix/default with project-scope
  materialization, per-profile `include`, and the O-STATE fix above.

## [0.3.1] - 2026-07-08

### Added

- **GitHub org-scoped zero-arg apply**: zero-argument `apply`/`validate`
  now also tries an `[[org]]` match (spec §8) between the repo-local and
  directory-prefix resolution steps — `resolve.detect_org()` parses the
  GitHub owner from `git remote get-url origin` (falling back to other
  remotes), for both SSH and HTTPS remote URL forms. No network call, no
  `gh` dependency, and no check that you're actually a member of the
  matched org (deferred). Degrades to `None` — an ordinary fall-through to
  the next O2 step, not an error — outside a git repo, with no
  `github.com` remote, or when `git` isn't on `PATH`.

## [0.3.0] - 2026-07-08

### Added

- **Manifest composition**: a top-level `include = [ref, ...]` list
  (local path or remote `https://`/`git+` URI) merges other manifests in
  depth-first, left-to-right order, with the including manifest's own
  tables merged in last — local always wins on a same-key collision.
  Reference cycles raise a `ManifestError` naming the cycle. A
  `[profiles.<name>]` may also carry its own `include`, selecting *all* of
  the referenced manifest's entries into that profile (bypassing its own
  profile slicing, if any), unioned with `inherits`/explicit key lists.
- **Zero-argument `apply`/`validate`**: `MANIFEST` is now optional —
  omitting it resolves one from the global config (a repo-local
  `agent-config.toml` at the repo root, then the longest matching
  `[[scope]] match_prefix`, then `default_manifest`), printing which
  source won (e.g. `resolved manifest from scope prefix '~/code/mit'`).
  That source's profiles/write-scope travel with it, still overridable by
  `--profile`/`--scope`.

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
