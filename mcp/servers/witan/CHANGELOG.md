# Changelog

All notable changes to `witan-council` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (pre-1.0:
a MINOR bump may include breaking changes).

## [Unreleased]

### Changed

- **`ActorTokenResolver` moved to `witan_core.identity`,** joining
  `derive_actor_id` there. witan-code resolves the same actors' tokens
  server-side now: under ADR-0005 path c the deployed MCP tier performs a
  remote indexer's code-graph writes as the caller, from the same provisioned
  `{actor_id: token}` file, in the same process `witan serve` mounts both tool
  surfaces into. `witan.identity` re-exports both, unchanged for every caller.

## [0.8.0] - 2026-07-31

### Changed

- **`derive_actor_id` moved to `witan_core.identity`.** witan-code needs the
  same ADR-0004 `sub` → `act-<id>` mapping client-side, to name the code-graph
  branch views a writer owns; a view named by one derivation and authorized
  against another would be a bug with no symptom until two users collide on
  it. `witan.identity` re-exports it, so `from witan.identity import
  derive_actor_id` is unchanged. `ActorTokenResolver` stays here — reading the
  provisioned token map is server-side only.

### Added

- **`workflow_project_update` — the missing post-creation edit path.** Until now
  a project's `title`, `description`, `tags` and `github_issue` could not be
  changed at all after creation, and its repo set could only ever *grow*, as a
  side effect of running `workflow_session_start` in a new repo. That made the
  common case — a repo set guessed during discovery, before the work's real
  blast radius was known — uncorrectable, and a project missing a repo does not
  surface in that repo's injected context at all. The new tool edits every
  mutable field, replaces the repo set wholesale (`repos`) or by delta
  (`add_repos` / `remove_repos`, both canonicalized so a differently-cased
  spelling still matches), and can mark a project `abandoned`. Repos are a plain
  list field rather than edges, so a removal really removes — unlike
  `workflow_project_unblock`, which can only update its denormalized field.
  Deliberately cannot set `phase` (`workflow_project_advance` owns transitions,
  including backwards ones) or `status="completed"` (`workflow_project_complete`
  stays the only route to a corpus trace, so a trace never exists without an
  outcome narrative).
- **`witan migrate dedupe-sessions`** reconciles sessions the pre-fix
  `workflow_session_start` duplicated, marking them `superseded_by` the
  surviving session (a new `WorkflowSession` field — run `witan migrate schema`
  first). Nothing is deleted: a marked session keeps its row and its edges and
  is simply skipped by every aggregate read. Dry by default, and deliberately
  not part of `migrate all` — unlike the other migrations it makes a judgment
  call about corpus content.

  Sharing a `session_id` is not on its own evidence of duplication, so only
  sessions that overlap *in time* are considered — and overlap is transitive:
  given `s1 [10:00-10:10]` and a retry `s2 [10:05-10:20]`, a session starting
  10:12 is still a duplicate, because s2 was open and the fixed
  `workflow_session_start` would have handed back its handle. Within an
  overlapping run only members with no summary of their own are marked, keeping
  the fullest summary as the survivor; a run where every member wrote a real
  summary is reported for review rather than guessed at, and resolved with
  `--supersede <duplicate>=<survivor>`.

### Fixed

- **`workflow_session_start` is re-entrant, and no longer duplicates a session
  on retry.** Every call minted a node, so a hook retry, a transport reconnect,
  or the replica failover its own docstring warns about silently created a
  second `WorkflowSession` for one real agent session — and
  `workflow_project_complete` aggregates every linked session into the
  `WorkflowTrace`, so duplicates inflated the corpus and skewed anything mined
  from it. A call for a (`project_slug`, `session_id`) whose session is still
  open now returns that handle with `existed: true`, merging any newly-supplied
  `repo` and `tags` into it.

  That check and the insert are not one atomic operation, so two *simultaneous*
  starts — a client retrying while the first request is still in flight, or two
  replicas handling one retry — can still both insert. The engine can't
  arbitrate it the way it does for `task_claim`: optimistic concurrency detects
  competing writes to one row, and these are two rows under two freshly-minted
  slugs. So it is resolved immediately after the insert instead, by keeping the
  earliest-started open session and superseding the rest. Writes serialize and a
  reader sees every write that preceded it, so the racer who inserted second
  always observes both rows, and keep-earliest is a rule both compute
  identically — they converge on the same handle. Costs one extra read per *new*
  session; the re-entrant path never reaches it.

  Idempotency is keyed on the *open* session rather than the pair alone,
  deliberately: one `$CLAUDE_SESSION_ID` routinely spans several working stints
  that are each closed with their own summary (the corpus holds clusters of
  eight), and folding those into one node would destroy seven of them. A retry
  always re-fires before the first call was ended; a new stint always starts
  after. Because the repo accretion still runs on the re-entrant path, calling
  this once per repo remains a valid way to widen a project's repo set — but
  `workflow_project_update` now does that directly.

- **Deployed multi-user writes are attributed to the calling user, not the
  server container.** `cfg.author` is resolved once at process startup, so under
  a deployment every `Memory`, `WorkflowProject`, `WorkflowTrace`,
  `WorkflowSession`, and `Task` carried a single author value deployment-wide —
  making `workflow_trace_list(author=…)` an inert filter, flattening the ranking
  layer's author-trust signal, and leaving mined corpus traces without usable
  provenance. A new `_current_author()` resolves the identity per request from
  the validated JWT (`preferred_username`, then `email`, then the derived
  `act-<sub>`). `task_claim` / `task_release` default their holder to the same
  helper, so parallel agents no longer all claim as one identity. Local stdio
  behavior is unchanged — `WITAN_AUTHOR` / git config / `$USER` remains correct
  there and stays in use. See ADR-0004 addendum D5.

## [0.7.2] - 2026-07-31

### Changed

- **`RemoteConfig` and the remote-config resolution moved to
  `witan_core.remote.config`** (re-exported from `witan.config`, so imports and
  behavior are unchanged). It carried nothing witan-council-specific and should
  have been extracted alongside `witan_core.remote.oidc`/`proxy`; witan-code now
  shares it, which is what lets one `[targets.<name>]` block and one login route
  both CLIs at the same deployment.
- The token-cache location moved to `witan_core.remote.oidc.cache_path()` — same
  `~/.config/witan/tokens.json` path and `WITAN_TOKEN_CACHE` override, now
  shared with witan-code rather than a private copy.
- **Requires `witan-core>=0.6`.**

## [0.7.0] - 2026-07-30

### Changed

- **Requires `witan-core>=0.5`** (was `>=0.2`) — the server now imports
  `witan_core.caching` and `elicit.MRTRElicitationMiddleware`, neither of which
  exists below that.
- **Elicitation works on the stateless protocol era again.** Every prompt the
  server raises — steal-the-claim (`task_claim`), supersede (`memory_link`), the
  backward-phase confirm (`workflow_project_advance`), the thin-outcome expand
  (`workflow_project_complete`), and the repo prompt on writes — had been
  silently returning its non-interactive default on any MCP 2026-07-28
  connection. See the `witan-core` 0.5.0 entry for the mechanism.
- **`tools/list` declares a cache directive** — `ttlMs=300000`,
  `cacheScope=private` — so clients can stop re-fetching the 37-tool surface
  every session.
- **`instructions` now distinguish `task_*` from MCP `tasks/*`.** The tool family
  tracks work items and is unrelated to MCP's async-execution extension.

### Added

- **ADR-0006** (`docs/adr/0006-stateless-mcp-protocol-era.md`) records the move
  to the stateless 2026-07-28 era: what it unlocks (multi-replica behind a plain
  round-robin LB, no session affinity), the two pieces of state that are still
  per-replica, and why the fastmcp 3.4.x/4.x straddle stays until 4.0 GA.
  ADR-0004 and ADR-0005 gained pointers to it.

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
