# Changelog

All notable changes to `witan-code` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (pre-1.0:
a MINOR bump may include breaking changes).

## [0.12.1] - 2026-08-07

### Fixed

- **`RemoteStoreClient.load` sized its batches by the wrong ceiling.** The same
  defect found in witan's `migrate merge`, latent here: `load` defaulted to
  `LOAD_MAX_BYTES` (omnigraph's 8 MiB buffered-body budget) while this transport
  ships records as a JSON tool parameter, where the MCP SDK caps request bodies
  at 4 MiB. A large enough index over `code_transport = "mcp"` would have failed
  with `413 Request body too large` — from the SDK rather than from omnigraph.
  Now defaults to `witan_core.chunking.MCP_LOAD_MAX_BYTES`.

  Not hit in practice before now, which is why it is recorded as a fix rather
  than a regression: the deployed indexer does not use this transport.

### Changed

- **Requires `witan-core>=0.11`** — `remote/store.py` evaluates
  `MCP_LOAD_MAX_BYTES` as a default argument, so it resolves at import time and
  0.10 does not export it.

## [0.12.0] - 2026-08-07

> Also covers 0.11.0 and 0.11.1, published from the workspace without their own
> entries; their content is folded in below.

### Changed

- **Requires `witan-core>=0.10`.** `chunk_records` and the batch-loading half
  of `OmnigraphClient` moved *out* of this package and into
  `witan_core.chunking` / `OmnigraphClient.load_batch`, so a code index and a
  memory merge share one split rule instead of two copies. Behaviour is
  unchanged; this is a breaking change only for anyone importing
  `witan_code.chunking` directly, which is why it is a minor bump.

### Fixed

- **`code_indexed_repos` returned nothing against a multi-graph server.**
  Enumerating a server's graphs is not something the CLI can answer — it now
  goes over HTTP to the server's own `/graphs`, whose envelope the previous
  parser did not match.

### Performance

- **Per-graph listing queries now fan out** rather than running in sequence:
  6.4s → 0.70s across the deployed graph set.

### Added

- **`code_store_mutate_many` — batched writes for the MCP transport.**
  `RemoteStoreClient.change_many` was the last per-row writer in witan-code:
  every other write path collapses N rows into one `omnigraph mutate` and
  therefore one Lance version, while this one looped `code_store_mutate`
  because that tool takes a query *file* the server resolves against its own
  `queries_dir` and there was no way to hand it a composed body. The new tool
  takes the same `(query, name, params)` steps `mutate` takes one at a time and
  calls the real `change_many` server-side, so the splice stays where
  `queries_dir` lives and the wire payload stays params rather than GQ — a
  client still cannot send arbitrary GQ through a surface Cedar scopes by named
  query. The concrete win is the reindex delete path: `_delete_file_steps`
  emits 2 steps per changed-or-purged file, so a 200-file reindex over this
  transport was 400 sequential MCP calls and 400 Lance versions and is now one
  of each. `chunk_size` keeps meaning statements-per-commit, applied as one
  call per chunk, so commit granularity is identical to the subprocess
  transport's. **Feature-detected**, since this is a deployed contract: a
  client meeting a server that predates the tool falls back to the per-step
  loop, which is slow rather than broken.

## [0.10.0] - 2026-08-01

### Added

- **Index onto a cluster code graph from outside the cluster —
  `code_transport = "mcp"` (ADR-0005 path c).** The omnigraph-server holding
  the code graphs is ClusterIP-only and stays that way, so until now a
  developer's checkout could reach it only through `kubectl port-forward`.
  Set the transport (env `WITAN_CODE_TRANSPORT`) and every store operation
  travels to the deployed witan MCP endpoint — the one exposed boundary —
  which performs it against the cluster graph **as the caller**: it resolves
  the actor from the validated JWT, looks up that actor's own omnigraph token,
  and applies the branch-view ownership guard server-side. The client keeps
  everything that needs a working tree; `indexer`/`bridge` are unchanged, and
  `RemoteStoreClient` stands in for an `OmnigraphClient`. It holds one
  connection open for the run rather than one per call, since an index is
  thousands of store operations, and reconnects once on a dropped one.
  Deliberate consequences: a write through this route can only target a view
  the caller's own actor owns, and can never target the shared
  default-branch view — that one's writer is the CI indexer, in-cluster over
  `code_server`, which is unchanged. Compaction and view reaping refuse here
  too; they belong to the cluster's own scheduled jobs.
- **`code_store_*` tools** — the server half of the above: `read`, `mutate`,
  `load`, `open`, `views`, `graphs` (`mutate_many` joined them later),
  mirroring the store operations the write
  path performs rather than modelling indexing itself, so indexing policy
  stays with the client that has the checkout. Mediated, not arbitrary:
  `query` may only name a query file bundled with the server, and the graph is
  resolved from a repo URI against the server's own configuration. Registered
  **only on a deployment** (`WITAN_OIDC_ISSUER` set; override with
  `WITAN_CODE_STORE_TOOLS`) — a local stdio server writes its stores directly,
  so serving them there would add six machine-facing tools to every agent's
  tool list to serve a caller that cannot exist.

- **`witan-code reap-views` — a stale branch-view reaper for the shared graph.**
  With per-writer views on one cluster graph, every developer's every git branch
  gets a view and nothing ever removes one. This sweeps views nobody has written
  in `WITAN_CODE_VIEW_MAX_IDLE_DAYS` (default 14; `0` disables). It is not
  `branches --prune` with a wider scope: prune asks whether *this checkout* still
  has the git branch, which is unanswerable about a store every user writes, and
  it keeps refusing there. This asks the store's own commit log how long ago a
  view was last written. `main` is never reaped (idle by design between merges),
  and neither is a view with no commits of its own — it holds nothing that isn't
  already on its fork point, and omnigraph records no branch-creation time to
  age it by, so reaping it would race the indexer that just made it. Reports by
  default; `--apply` deletes, and refuses on a shared graph unless
  `WITAN_CODE_INDEX_ROLE=ci`. Unparseable `commit list` output raises rather
  than reading as "never written" — `None` is what tells the reaper to leave a
  view alone, so degrading a parse failure to it would turn an omnigraph output
  change into a reaper that reports success forever while sprawl grows. A
  disabled window (`0`) skips the per-view aging entirely instead of scanning
  and discarding, and a graph that cannot be surveyed is reported and
  non-zero-exited without stranding the other stores in the sweep. See
  `docs/BRANCH_INDEXING.md` § Reaping stale views and witan `docs/adr/0006`.

### Changed

- **`graph.check_writable` takes `is_remote: bool`, not a `client`.** It only
  ever read that one bit off the client, and the MCP tier serving somebody
  else's write has no client of its own to read it from — the answer there is
  "shared, by construction", since that is the only reason the request exists.
  Taking the bit lets both sides ask the same question.
- **Cedar can gate `main` vs WIP, but not view ownership** — corrected from
  0.9.0's claim that one rule could. omnigraph 0.8.1 compiles a bundle rule to a
  bare `permit(...)` with no `when {}` clause, and its only branch predicate is
  the three-valued protected/unprotected scope: there is no branch-name pattern
  and no `principal.actor` to compare a prefix against. The `act-<sub>/` prefix
  is therefore enforced by the client write guard alone. The policy bundle now
  pins this gap with a deliberately-`allow` test case, so an omnigraph release
  that closes it fails the build instead of passing unnoticed.

- **Requires `witan-core>=0.7`.** The mtime-stamped schema re-apply
  (`_schema_apply` / `_schema_apply_if_changed` / `_schema_stamp` in
  `witan_code/store.py`) moved up into `witan_core.omnigraph` as
  `schema_apply` / `schema_apply_if_changed` / `schema_stamp_path`, so
  witan-council can pick up additive schema changes on an existing store the
  same way. No behavior change here — `ensure_store` and `ensure_bridge_store`
  call the shared functions with the same arguments — but the floor moves,
  because resolving witan-core 0.6 would now fail at import.

## [0.9.0] - 2026-07-31

### Added

- **Branch views are namespaced per writer, and the purge follows view
  ownership.** A git branch is not a unique key for an index: two checkouts on
  `feature-x` — two developers, one developer in two worktrees, an agent and
  its human — are two working trees, and a view named for the branch alone
  meant the second writer silently overwrote the first with a different
  uncommitted state. A view is now named `[<actor>/]<branch>` on a per-repo
  graph and `[<actor>/]<repo-slug>/<branch>` on the bridge, where `<actor>` is
  the ADR-0004 `act-<sub>` id resolved from the `witan login` session (shared
  derivation with the deployed server, `witan_core.identity`; never `$USER`,
  which is not what the cluster's tokens and Cedar policies are written
  against). Set `WITAN_ACTOR`, or `actor` on a `[targets.<name>]` block, for a
  writer with no interactive login. The actor comes first in both schemes on
  purpose: ownership is then a *prefix*, so one comparison, one reaper sweep,
  and one Cedar rule cover both stores.

  Isolation did not cost visibility, which is the whole reason branch views
  live on the shared graph: `code_indexed_branches(branch=<git branch>)` (CLI:
  `witan code branches --branch <b>`) lists every writer's view of a branch
  with its owner, and any listed view name goes straight back as `branch=` to
  `code_find_definition` / `code_search_symbol` / `code_symbols_in_file`.
  Default reads follow the checkout's branch to *this* actor's view first,
  then any other writer's, then `main`.

  `graph.owns_view` is now the single predicate behind both `check_writable`
  and `indexer._may_purge`, replacing two separate approximations of the same
  question: a local store has one user who is its writer, CI owns the shared
  default view, and every actor owns its own branch views. A developer may
  therefore purge their own branch view again — under the old "remote and not
  the designated writer" rule they could not, so files they had deleted
  lingered in their own view. Writing a view owned by another actor, or an
  un-owned branch view, is refused on a shared graph. Purely local use is
  unchanged: no identity, no prefix, no migration, and no login needed to
  index offline. `branches --prune` remains refused against a shared graph —
  no client can tell whose views are live. See
  [docs/BRANCH_INDEXING.md](docs/BRANCH_INDEXING.md#per-writer-branch-views).

  **Breaking (tool surface):** `code_indexed_branches` returns
  `{repo, views: [{view, branch, actor}]}` instead of
  `{repo, branches: [str]}`, and no longer lists `main` (the default view is
  not an in-flight branch view).

- **`WITAN_CODE_INDEX_ROLE` — an explicit writer role for the shared
  default-branch view.** On the deployed cluster a per-repo code graph is one
  graph for the whole team, and its `main` view — the one every reader falls
  back to — had no owner. It has one now: CI indexes the default branch,
  everyone else reads it. Indexing a shared graph's default-branch view is
  refused unless the process declares itself the writer
  (`WITAN_CODE_INDEX_ROLE=ci`, or `index_role = "ci"` on a `[targets.<name>]`
  block); an unrecognized value is an error rather than a silent demotion to
  reader, which would freeze the shared view with nothing to explain it.
  Authority is deliberately a **role, not a transport**: the CI indexer is
  remote too, so the blanket "refuse writes when remote" that 0.8.1 shipped as
  a down payment would have blocked the one writer the design depends on.
  `_may_purge` now reads "not remote **or** designated writer" accordingly —
  dropping rows for files deleted from the default branch is precisely CI's
  job. Local stores are unaffected (one user, who is their writer), and branch
  views stay writable by anyone: they are branch-scoped, so in-flight work
  never lands on the shared view. That exemption is deliberate — per-user
  branch views live *on* the shared graph, so isolated agents can see each
  other's work as it happens. The two follow-ups that decision required
  (namespacing views per writer, and re-scoping the purge to "this actor owns
  the view being written") landed in the entry above. See
  [docs/BRANCH_INDEXING.md](docs/BRANCH_INDEXING.md#who-may-write-the-shared-default-branch-view).

### Changed

- **The `inject-context` block now names a call instead of stating a
  preference.** The old block ended with "prefer `code_search_symbol` /
  `code_find_definition` / ... over grep", which measured badly: across 50
  sessions in `agent-kit` that received it, the block was injected 248 times
  and produced 5 `code_*` calls against 802 Grep/Read/Glob/Explore calls, with
  46 of the 50 sessions never calling a `code_*` tool at all. The cause is that
  the `code_*` tools reach the agent **deferred** — name only, no schema, a
  `ToolSearch` round-trip short of callable — in 50 of 50 of those sessions,
  while Grep/Read/Glob are always loaded. Preferring a tool that is not in the
  tool list is not an actionable instruction. The block now leads with the
  `ToolSearch(query="+code_ find_definition callers impact")` that makes them
  callable (the `+code_` form is used because `select:` needs the full
  `mcp__<server>__` prefix, which depends on the user's MCP config), gives a
  `code_find_definition` → `symbol_id` → `code_callers`/`code_impact` template
  to fill in, reports how many *other* repos are indexed so an empty
  `code_interface_*` result is not misread as "nothing consumes this", and
  points at the `/witan-code` skill. Net cost is ~110 chars per prompt; a
  regression test caps the whole block at 600.

### Fixed

- **`branches --prune` no longer prunes a shared graph.** It deletes store
  branches whose git branch is absent from *this machine's* refs — against a
  graph shared by the team, "this checkout doesn't have that branch" and "that
  branch is gone" are indistinguishable, so one user pruning would delete
  another user's in-flight branch view. Now refused per-repo when the store is
  remote, with the repo named. This is a **separate** check from the ADR-0005
  guard added in 0.8.1: that one refuses `--prune` in remote MCP-*client* mode
  (`WITAN_REMOTE_URL`), which is about where the read tools dispatch, whereas
  cluster addressing is the data tier — either can be remote without the other.

## [0.8.1] - 2026-07-31

### Fixed

- **Nested checkouts are no longer indexed as part of the parent repo.** The
  walk skipped `.git` *directories*, but a linked worktree or submodule marks
  itself with a `.git` **file**, so `.claude/worktrees/<name>/` was descended
  into and every file there was attributed to the parent repo — at a different
  path, at whatever revision that worktree happened to be on. In this repo's
  own store that was **732 of 990 indexed files (74%)**, spread across every
  branch view. The practical damage was to `code_search_symbol` and friends:
  a query returned the same function many times over with *different, older
  signatures*, so reading the first hit gave a confidently wrong answer, and
  `code_impact` inflated blast radius with callers that only exist on an
  abandoned branch. Any subdirectory holding a `.git` entry is now skipped.
  Indexing from *inside* a worktree is unaffected — only descending into one
  from the parent is refused — so the hooks keep working on a branch.
- **A full-repo index now purges files the repo no longer has**, reported as
  `purged=N`. Nothing removed a `CodeFile`/`Symbol` once written unless that
  same file was re-indexed, so deletions leaked forever: this repo's store
  still held `_detach.py` and its tests, moved out to `witan-core` several
  releases ago. This is also what clears the worktree rows above — excluding a
  path stops adding rows but cannot retract the ones already written, so
  without it the fix would only have stopped the bleeding.

  Purging never runs against a shared cluster graph (`is_remote`): there the
  default branch is indexed by CI and everyone else gets a read-only view, so
  one developer's working tree must not reconcile it for everybody. Inert
  today — code stores are still local — and load-bearing once witan-code
  addresses `--server`/`--graph`.

  Membership is decided by the set of files just collected, **not** by whether
  the file still exists on disk: a linked worktree's files are on disk and
  still must go. Purging requires a confirmed git root and a full-repo target;
  a subpath, a single file (the reindex hook), or a non-git directory never
  purges — without a git root `base` falls back to the target directory, which
  would make every stored path look stale.

### Changed

- An unreadable directory now suppresses the purge for that run (and is
  reported on stderr, counting toward `errors`). `os.walk` hands such a
  directory to `onerror` and otherwise continues silently, so a subtree the
  walk could not enter is indistinguishable from one that was deleted — which
  would have taken its still-present files' rows with it. Indexing still
  proceeds with whatever was readable; only the destructive half backs off.
- The file walk prunes as it goes (`os.walk`) instead of walking everything and
  filtering afterwards, so `node_modules`/`.venv` subtrees are no longer
  traversed in full. Collection order is now sorted, making a run reproducible.
- `code_reindex` returns a `purged` count alongside the existing counters.

## [0.8.0] - 2026-07-31

### Added

- **Remote MCP-client mode for the CLI (ADR-0005 parity).** With
  `WITAN_REMOTE_URL` + `WITAN_OIDC_ISSUER` set (or `remote_url`/`oidc_*` on the
  matched `[targets.<name>]` block), the read commands — `symbols`, `deps`,
  `stitch`, `repos`, `branches` — dispatch over `streamable-http` to a deployed
  witan service instead of this machine's stores, authenticated with a per-user
  Keycloak JWT. Previously only witan-council's CLI could reach a deployment,
  even though `witan serve` has always mounted this server's `code_*` tools onto
  the same endpoint. Indexing and store maintenance stay local unconditionally:
  they need the checkout and the store files on disk.
- **`witan-code login` / `logout` / `whoami`** — the OIDC device grant against
  the configured deployment. The token cache and default client id are shared
  with the `witan` CLI and keyed by `(issuer, client id)`, so a prior
  `witan login` already covers witan-code and vice versa.
- **Four MCP tools** backing the commands above, so both the CLI and an agent
  reach the same data: `code_repo_symbols` (a repo's contract surface — what it
  exports, what it expects), `code_repo_dependencies` (the whole "repo A depends
  on repo B" graph), `code_indexed_repos` and `code_indexed_branches` (coverage:
  which repos and branch views exist, and how fresh).

### Changed

- **The read commands route through the tool surface, not `OmnigraphClient`.**
  The CLI and the MCP server used to be two disconnected implementations of the
  same queries; the commands now dispatch through a `_srv()` indirection that is
  the in-process server module locally and an MCP proxy remotely. Output is
  unchanged in local mode.
- **Requires `witan-core>=0.6`** and pulls its `remote` extra — the shared
  `RemoteConfig` and OIDC client stack live there. No new transitive weight:
  `fastmcp` was already required and itself depends on `httpx2`.
- `witan-code branches --prune` refuses to run in remote mode (it deletes from
  the local stores, comparing against this machine's git refs). Listing works
  against a deployment.

## [0.7.0] - 2026-07-30

### Changed

- **Requires `witan-core>=0.5`** (was `>=0.2`) — the server now imports
  `witan_core.caching` and `elicit.MRTRElicitationMiddleware`, neither of which
  exists below that.
- **`code_reindex` is now async and no longer blocks the event loop.** It was a
  plain `def`, so a multi-minute index stalled every other request on the server
  for its whole run; the indexer now runs on a thread.
- **`tools/list` declares a cache directive** — `ttlMs=300000`,
  `cacheScope=private`.
- **Elicitation works on the stateless protocol era again** — the index-now
  confirms and the repo prompt had been silently returning their
  non-interactive defaults on any MCP 2026-07-28 connection. See the
  `witan-core` 0.5.0 entry.

### Added

- **`code_reindex` accepts task-augmented execution** (`io.modelcontextprotocol/tasks`,
  SEP-2663) via the new optional `witan-code[tasks]` extra: a client can take a
  handle and poll rather than holding the tool call open for the whole rebuild.
  Opt-in per call — a client that does not ask gets the same completed result as
  before. Without the extra the tool stays synchronous; the extra is separate
  because it pulls a Docket/Redis stack most installs never need, and because it
  requires FastMCP 4 while this package still supports 3.4.x. Note that while
  4.0 is still a pre-release, `witan-code[tasks]` needs `--prerelease=allow`
  under uv; the base package does not.

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
