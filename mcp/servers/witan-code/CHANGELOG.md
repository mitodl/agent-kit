# Changelog

All notable changes to `witan-code` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (pre-1.0:
a MINOR bump may include breaking changes).

## [0.6.0] - 2026-07-21

### Fixed

- **`repo.detect(override=...)` and the `WITAN_REPO` env var now route
  through `normalise`** (issue #142), same as an auto-detected git remote —
  matching the fix in witan-council 0.6.0. Previously an explicitly-passed
  `repo=` (or `WITAN_REPO`) was stored verbatim, bypassing
  canonicalization. `graph_id` (the shared-cluster graph id) already
  case-folded unconditionally and needs no change; a repo whose canonical
  key changes case after this fix should be re-indexed (`witan-code
  reindex`) so its per-repo store lands under the new key.
- Depends on `witan-core[cli]>=0.2,<1` (unchanged range; picks up 0.4.0's
  repo-key case-fold in `normalise`, which this package's `repo.detect`
  relies on).

## [0.5.0] - 2026-07-20

### Added

- `config.toml` + `[targets.<name>]` support (previously env-var only):
  `WITAN_CONFIG` / `~/.config/witan/config.toml`, a global `code_dir`/
  `author`, and named targets overriding `code_dir`/`author`, selected by
  `WITAN_TARGET` env var, an explicit `load(target=...)`, or auto-detection
  via `match_paths`/`match_repos`/`match_hosts`/`match_orgs` (same
  precedence and file as witan — see `witan_core.target_config` and
  `witan.config.load()`'s docstring). A target can carry witan's
  `server`/`graph`/`token` alongside this server's `code_dir` under one
  name; witan-code reads only the fields it knows. `Config` gained
  `target_name`. `load()` now optionally takes a `target` argument (was
  zero-argument only). New dependency: `pydantic>=2,<3`.

### Changed

- Now depends on `witan-core[cli]>=0.2,<1` (unchanged range; picks up
  0.3.0, which adds the shared target-routing logic this release uses).

## [0.4.0] - 2026-07-16

### Added

- **`witan-code --output-format`**: `repos`, `symbols`, and `stitch` can now
  render their table data as `json`, `toml`, or `yaml` in addition to the
  default Rich table output. The same option is honored when mounted as
  `witan --output-format … code …`.

### Changed

- Adopted the shared `witan-core` package for the CLI scaffolding
  (`make_app`/`report_install`/`resolve_author` + agent-name constants),
  replacing the previously duplicated copies. No user-facing behavior change.
  Now depends on `witan-core[cli]>=0.2,<1`.

## [0.3.0] - 2026-07-10

### Added

- **`witan-code --version`**: prints the installed package version,
  appending a git short ref when installed editable (local workspace) or
  directly from a VCS URL (`uvx --from git+...`), via the new
  `agent_config_kit.resolve_version()` helper.

### Changed

- `agent-config-kit` dependency bumped to `>=0.4,<1` (was `>=0.1,<1`) to
  require the version that ships `resolve_version`, which backs the new
  `--version` flag.

## [0.2.1] - 2026-07-09

### Fixed

- **Pinned `tree-sitter` to `0.25.x`**: `0.26.0` has a use-after-free in its
  pyo3 `Node` binding that segfaults (exit 139) under the heavy
  `.parent()`/`.children()` churn `_walk_defs` does on real-world-sized
  files — deterministically on the 2nd-3rd parse in one process, so
  `witan-code index` hard-crashed on any real repo. `0.25.0`-`0.25.2` are
  crash-free with the same ABI-15 grammar wheels.

## [0.2.0] - 2026-07-09

Initial PyPI release. `witan-code` is a tree-sitter-based code graph MCP
server (Layer 2): indexes a repo's symbols (functions, methods, classes,
modules) and their relationships, then exposes `code_search_symbol` /
`code_find_definition` / `code_find_references` / `code_callers` /
`code_impact` queries to the agent. Includes the Layer 2.5 cross-repo bridge,
linking SOA repos by shared `env_var`/`endpoint`/`package`/`service` contract
keys. Mounts standalone as `witan-code` or under `witan code …` when the
`witan` umbrella CLI is installed alongside it.
