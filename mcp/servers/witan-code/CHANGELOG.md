# Changelog

All notable changes to `witan-code` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (pre-1.0:
a MINOR bump may include breaking changes).

## [Unreleased]

### Added

- **`witan-code --output-format`**: `repos`, `symbols`, and `stitch` can now
  render their table data as `json`, `toml`, or `yaml` in addition to the
  default Rich table output. The same option is honored when mounted as
  `witan --output-format … code …`.

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
