# Changelog

All notable changes to `witan-core` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (pre-1.0:
a MINOR bump may include breaking changes).

## [0.5.0] - 2026-07-30

### Changed

- **Requires FastMCP 4 (`fastmcp>=4.0.0b1,<5`).** The 3.4.x end of the previous
  range is dropped, along with the cross-version shims it needed:
  `_tool_input_schema` / `_next_cursor` in `remote/proxy.py` now read
  `input_schema` / `next_cursor` directly, and `elicit.py` imports `mcp_types`
  unconditionally. **FastMCP 4.0 is still a pre-release**, so an installer that
  does not accept pre-releases cannot resolve this: `pip install` works (the
  explicit `>=4.0.0b1` admits it), but `uv pip install` / `uv add` need
  `--prerelease=allow` because `fastmcp` pins `fastmcp-slim` to the same
  pre-release version transitively.

### Added

- **`witan_core.caching`** — the shared `ttlMs`/`cacheScope` hint both servers
  declare on their list results (MCP 2026-07-28, SEP-2549). 300s at `private`
  scope; see the module docstring for why `public` is the wrong default when a
  server holds per-actor data.
- **MRTR elicitation.** `elicit.confirm` / `elicit.text` now pick their wire
  mechanism per request: multi-round-trip (SEP-2322) on a 2026-07-28 connection
  whose client advertises elicitation, `ctx.elicit` on the handshake eras, and
  the caller's default when neither is possible. This fixes elicitation being
  silently dead on 2026-07-28 — that era removed the server→client back-channel,
  so `ctx.elicit` raises and the previous blanket `except Exception` turned every
  prompt into its default. `MRTRElicitationMiddleware` must be registered on the
  server for the MRTR path to work.
- **`RemoteMCPProxy` answers elicitation prompts.** New `_elicitation_handler()`
  hook, defaulting to `console_elicitation_handler`, which prompts on the
  terminal. Previously a prompt raised by a deployment could never be answered
  over the CLI.
- **The proxy honors the server's `ttlMs`** for its cached tool list, instead of
  holding it for the whole process lifetime.

## [0.4.0] - 2026-07-21

### Changed

- **`repo_key.normalise` now case-folds GitHub/GitLab repo keys** (issue
  #142): the host is always lowercased (DNS hostnames are inherently
  case-insensitive), and the org/repo path is additionally lowercased for
  `github.com`/`gitlab.com`, whose org/repo names are themselves
  case-insensitive. A generic/self-hosted git host's path is left as-is,
  since its paths aren't guaranteed case-insensitive. Previously
  `https://github.com/Org/repo` and `https://github.com/org/repo`
  canonicalized to two different keys, silently fragmenting every
  `repo`-keyed record (tasks, memories, workflow projects, symbol ids)
  across whichever case happened to be detected in a given session. This is
  a **breaking change to the golden-contract output** for any repo whose
  GitHub/GitLab org or name contains uppercase characters — existing data
  written under the old, differently-cased key needs a one-time migration;
  see witan-council 0.6.0's `witan migrate repo-keys` (witan-code's
  per-repo/bridge stores are unaffected — they were already
  case-insensitive via `graph_id`, or are documented re-derivable caches
  fixed by `witan-code reindex`).

## [0.3.0] - 2026-07-20

### Added

- `target_config` module — the shared `[targets.<name>]` routing logic
  (`match_target`, `parse_target_tables`, `to_list`, `local_project_path`),
  extracted from witan's `config.py`. Adds a `match_paths` tier (local
  checkout path prefix) alongside the existing `match_orgs`/`match_repos`/
  `match_hosts`, checked first since it's the most specific — it pins a
  filesystem location regardless of remote, and runs even when no
  `repo_uri` is available. `match_target` is structurally typed (a
  `Protocol` over the four `match_*` lists), so each server keeps its own
  typed target model (different override fields) and both route through the
  same precedence rules. witan-code now uses this too (see its CHANGELOG).
- `config_file.load_toml` — shared config.toml loading (`WITAN_CONFIG` env
  var, missing-file/parse-error handling). Both servers read the *same*
  file by convention, so one `[targets.<name>]` block can carry overrides
  for both at once.

## [0.2.0] - 2026-07-16

### Added

- Remote-access layer (`witan_core.remote`, the `remote` extra) — the
  transport-agnostic ADR-0005 path-a client stack, so a second deployed server
  can reuse it: `DeviceAuth` (`witan_core.remote.oidc`) drives the OIDC
  device-authorization grant (RFC 8628) + a 0600 token cache, parameterized by
  cache path and login hint; `RemoteMCPProxy` (`witan_core.remote.proxy`) mirrors
  a FastMCP server's tool surface over `streamable-http` (positional→name arg
  mapping, `{"result": …}` envelope unwrap), with subclass hooks for the
  admin-tool refusal set, client-side repo resolution, and error wording.
  witan-council now binds its policy (cache location, `witan login` hint,
  `_ADMIN_ONLY`, `repo.detect`) via thin shims instead of owning the mechanism.
- CLI scaffolding (`witan_core.cli`, the `cli` extra): `AgentName`/`AGENT_NAMES`
  (the supported coding-agent constants), `make_app` (the `--version`-wired
  cyclopts app factory), `resolve_author` (`--author` → `git config user.name` →
  `$USER` → `"unknown"`), and `report_install` (the agent-config install-result
  printer — styled with a rich `console`, plain `print` without one). Both
  servers' `setup` commands now share these instead of carrying divergent copies.
- `agent-config-kit` is now a dependency of the `cli` extra (supplies
  `resolve_version` and the `InstallResult` type) — a valid leaf→leaf edge, since
  both servers already depend on it.

## [0.1.0] - 2026-07-15

Initial release of the shared `witan-core` package (import `witan_core`),
mirroring `agent-config-kit`. Both witan MCP servers now depend on it via a
`[tool.uv.sources]` editable path plus a `witan-core>=0.1,<1` PyPI range. This
establishes the package and the deliberate reversal of the "no cross-package
import" convention. See `docs/design/witan-core-extraction-spec.md`.

### Added

- `popen_detached` (`witan_core._detach`) — cross-platform detached subprocess spawning.
- `install_omnigraph` (`witan_core.omnigraph_install`) — the pinned omnigraph
  binary installer and single source of `_OMNIGRAPH_VERSION` (`rich` imported lazily).
- `confirm`/`text` (`witan_core.elicit`) — MCP elicitation primitives (the `mcp` extra).
- `normalise`/`find_git_config` (`witan_core.repo_key`) — the cross-layer
  repo-key canonicalizer, with a golden contract test.
- `now_iso` (`witan_core.timeutil`).
- Throttled-optimize mechanics (`witan_core.maintenance`) — interval parsing,
  atomic last-run stamp, and due-check.
- `OmnigraphClient` base (`witan_core.omnigraph`) — the omnigraph-CLI subprocess
  wrapper (write lock, retry/repair, per-actor admission-cap backoff,
  `OmnigraphConflict`); each server subclasses it for its own tail.

Extracted from the duplicated surface of `witan` (witan-council) and
`witan-code`; the now-duplicated copies were deleted from both servers.
