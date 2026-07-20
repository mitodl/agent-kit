# Changelog

All notable changes to `witan-council` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (pre-1.0:
a MINOR bump may include breaking changes).

## [0.5.0] - 2026-07-20

### Added

- `[targets.<name>]` blocks gained a `match_paths` criterion — routes by
  local checkout path prefix (e.g. `match_paths = ["~/code/personal"]`),
  checked before `match_repos`/`match_hosts`/`match_orgs` since it pins a
  specific filesystem location regardless of remote, and applies even when
  no repo remote is configured at all. See the `load()` docstring in
  `witan/config.py` for the full precedence order.

### Changed

- Target routing (`match_target`, `parse_target_tables`, `to_list`) moved to
  `witan_core.target_config`; `witan-code` now shares it (and this same
  `config.toml`), so a single `[targets.<name>]` block can carry overrides
  for both servers — witan's `server`/`graph`/`token`/… alongside
  witan-code's `code_dir`. No user-facing behavior change for existing
  `match_orgs`/`match_repos`/`match_hosts` configs. Now depends on
  `witan-core[cli,remote]>=0.2,<1` (unchanged range; picks up 0.3.0).

## [0.4.0] - 2026-07-16

### Added

- **`witan --output-format json|toml|yaml`**: a global option (default
  `txt`; env `WITAN_OUTPUT_FORMAT`) that renders table-producing commands
  (`tasks`, `projects`, `memory`, `traces`, `scan test`, `scan rules`, plus
  `witan-code`'s `code repos`, `code symbols`, and `code stitch` when
  installed) as a machine-readable dump of the same rows instead of a rich
  table.

### Changed

- Adopted the shared `witan-core` package for the CLI scaffolding
  (`make_app`/`report_install`/`resolve_author` + agent-name constants) and the
  remote MCP-client stack (OIDC device-auth + `RemoteServerProxy`), replacing
  the previously duplicated copies; witan-council now binds only its own policy
  on top. No user-facing behavior change. Now depends on
  `witan-core[cli,remote]>=0.2,<1`.

## [0.3.0] - 2026-07-10

### Added

- **`witan --version`**: prints the installed package version, appending a
  git short ref when installed editable (local workspace) or directly from
  a VCS URL (`uvx --from git+...`), via the new
  `agent_config_kit.resolve_version()` helper.

### Changed

- `agent-config-kit` dependency bumped to `>=0.4,<1` (was `>=0.1,<1`) to
  require the version that ships `resolve_version`, which backs the new
  `--version` flag.

## [0.2.0] - 2026-07-09

Initial PyPI release. `witan` is the umbrella CLI and MCP server for the
team-wide work-coordination graph: `memory_*` (patterns, project facts,
lessons, agent context, with BM25 + graph-expansion recall), `task_*`
(work-coordination graph with dependencies and claiming), and `workflow_*`
(cross-session project/session tracking). Covers `witan serve` (the MCP
server), `witan tasks`/`witan memory`/`witan project`/`witan trace` CLI
commands, `witan scan`/`witan migrate` graph maintenance, session hooks, and
mounting `witan-code` as `witan code …` when it's installed alongside it.
