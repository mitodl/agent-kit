# Changelog

All notable changes to `witan-council` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (pre-1.0:
a MINOR bump may include breaking changes).

## [0.6.0] - 2026-07-21

### Added

- **`witan migrate repo-keys`** (issue #142): a one-shot, idempotent
  migration that folds every stored repo key onto its canonical,
  case-folded form (`witan-core` 0.4.0's `normalise` change). Rewrites
  Task/Memory/WorkflowSession `repo` (and their `symbol_refs` repo
  prefixes), WorkflowProject/WorkflowTrace `repos` lists (deduping entries
  that fold onto the same key), and CodeBranch (recreated under the
  canonical slug — its slug embeds `repo`, so it can't be updated in place
  — with the stale row marked `abandoned`). Now part of `witan migrate
  all`, so a routine deploy self-heals a store instead of needing this run
  by hand. Prints which repos' canonical key changed case, since the code
  graph (witan-code) isn't touched by this migration and needs its own
  `witan-code reindex` for those repos.
- The injected context block now warns when it detects pre-migration,
  differently-cased data for the current repo (reusing reads the hook
  already makes, so this costs nothing extra) — a nudge to run `witan
  migrate repo-keys`, not the exhaustive check itself.

### Fixed

- **`repo.detect(override=...)` and the `WITAN_REPO` env var now route
  through `normalise`** (issue #142), same as an auto-detected git remote.
  Previously an explicitly-passed `repo=` (or `WITAN_REPO`) was stored
  verbatim — an SSH-style URL, a `.git`-suffixed URL, or mismatched case all
  bypassed canonicalization and could never join against auto-detected
  data for the same repo. Same fix applied to the context-injection hook's
  own `WITAN_REPO` handling (`context.py`), so it can't drift from
  `repo.detect`'s resolution.
- Depends on `witan-core[cli,remote]>=0.2,<1` (unchanged range; picks up
  0.4.0's repo-key case-fold).

## [0.5.0] - 2026-07-20

### Added

- `[targets.<name>]` blocks gained a `match_paths` criterion — routes by
  local checkout path prefix (e.g. `match_paths = ["~/code/personal"]`),
  checked before `match_repos`/`match_hosts`/`match_orgs` since it pins a
  specific filesystem location regardless of remote, and applies even when
  no repo remote is configured at all. See the `load()` docstring in
  `witan/config.py` for the full precedence order.
- `[targets.<name>]` blocks can now also carry `remote_url`/`oidc_issuer`/
  `oidc_client_id`/`oidc_audience` — the CLI's remote MCP-client mode (ADR
  0005) is now resolved the same way as the omnigraph `server`/`graph`
  fields (env var > target > global config.toml > default), instead of
  `WITAN_REMOTE_URL`/`WITAN_OIDC_*` env vars alone. This lets different
  orgs/repos/checkouts point at different deployed witan services, and a
  single target can route both the omnigraph store and the deployed
  service under one name. `RemoteConfig` gained `target_name`; `witan
  whoami` now shows it. `load_remote_config()` gained an optional `target`
  argument. Existing `WITAN_REMOTE_URL`/`WITAN_OIDC_*`-only setups are
  unaffected (env vars still take precedence over any target).

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
