# Changelog

All notable changes to `witan-council` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (pre-1.0:
a MINOR bump may include breaking changes).

## [0.11.3] - 2026-08-07

### Fixed

- `memory_search` and `recall` now find terms that appear only in a memory's
  **title**. All four search queries matched `search($m.content, …)` alone, so a
  term in a title and nowhere in the body could never match — not "ranked low",
  never, at any corpus size. Titles in this corpus are full sentences carrying
  the distinguishing identifiers, and an agent recalling context queries with
  exactly those words, so this was the common case failing quietly.
- `read.gq` gains a `_title` twin for each of the four search queries. They are
  separate queries rather than one predicate because the query language will not
  `or` two `search(…)` calls in a single match (`expected comp_op`). `_search_rows`
  unions the two runs, deduping by slug.

### Changed

- Content matches rank above title-only matches: the two BM25 runs are not on a
  comparable scale, so title hits are appended rather than interleaved by score.
  Downstream ranking reads rank *position* (`_rerank`'s `norm_bm25` proxy), so
  appending is what preserves the ordering. The union is capped at
  `_SEARCH_LIMIT` (20) — the documented result size — rather than returning up
  to 40 rows.

## [0.11.2] - 2026-08-07

### Fixed

- `witan migrate merge` against a deployment no longer dies with a raw
  `fastmcp.exceptions.ToolError: … 413: Request body too large`. The refusal is
  classified as `RemotePayloadTooLarge` and printed as a sentence by `main()`,
  alongside the three remote failures already handled. This is the other half of
  0.11.1's client bound: that stops witan *producing* oversized bodies, this is
  what happens when one gets through anyway — a deployment whose cap is lower
  than `MCP_LOAD_MAX_BYTES` assumes, or a single record no batch size can split.

### Added

- `_merge_batch_refusal` adds merge-specific context to a 413 refusal, where it
  is known rather than assumed: which batch was refused, the budget it was sized
  against, and how many batches already landed. A `--dry-run` is reported as
  writing nothing at all rather than as a partial write — the base message used
  to tell its user the merge "stopped part-way", which invites a hunt for
  half-migrated rows that do not exist.

### Changed

- `witan-core` floor raised to `>=0.12` — `RemotePayloadTooLarge` is imported at
  module scope in `remote/proxy.py` and `cli/__init__.py`, and 0.11 does not
  export it. Third time this floor has been tripped; the pin comment now says so
  and says what to do about it.

## [0.11.1] - 2026-08-07

### Fixed

- **`migrate merge` against a deployment sized its batches by the wrong
  ceiling.** It chunked on `LOAD_MAX_BYTES` — omnigraph's 8 MiB buffered-body
  budget — but rows shipped through the MCP tier ride as a JSON tool parameter,
  where the MCP SDK caps request bodies at 4 MiB. A real personal graph went out
  as one oversized request and the deployment answered `413 Request body too
  large`. Now bounded by `witan_core.chunking.MCP_LOAD_MAX_BYTES`: on the store
  that produced the original failure, 2 requests with a 4.78 MB body becomes 4
  requests with a largest of 2.10 MB.

### Changed

- **Requires `witan-core>=0.11`** — `remote/proxy.py` imports
  `MCP_LOAD_MAX_BYTES` at module scope, which 0.10 does not export.

## [0.11.0] - 2026-08-07

> Also covers 0.10.0, which was published from the workspace without its own
> entry; its batched-write work is folded in below.

### Added

- **`witan target add|list|remove` — join a deployment without hand-editing
  TOML.** Registering a deployed witan meant hand-writing a `[targets.<name>]`
  block with two URLs you had to get exactly right, in a file you had to know
  the path of. `witan setup` did not help: it writes a starter config only when
  none exists, and every remote key in it is commented out.

  The sharp edge was the issuer — a typo in it did not surface as a *config*
  error, it surfaced later as an auth failure during `witan login`, far from
  its cause. So `target add` verifies the issuer **at registration time**
  against its `.well-known/openid-configuration`, reusing the same
  `discover_endpoints` the device grant itself later relies on (RFC 8414 §3.3
  issuer-match check included). Nothing is written if it fails; `--no-verify`
  covers offline registration.

  Blocks are appended as *text*, never round-tripped through a TOML writer: the
  shipped config.toml is almost entirely comments documenting every key, and
  re-serialising the parsed document would silently delete all of them.
  `--force` replaces a block **in place** — `match_target` returns the first
  match, so moving it to the end of the file would silently re-order routing
  precedence. Writes are atomic (temp file → `fsync` → `os.replace`) and
  explicitly UTF-8: these are the only commands that rewrite a user-owned file,
  and a truncated config.toml is not a soft failure, since `load_toml` raises
  on a decode error and every later `witan` command stops working.

  Deliberately *not* included: an `--env <name>` shorthand deriving both URLs
  from one flag. That needs a specific organisation's SSO and service hostnames
  compiled into a general-purpose package; verifying the issuer generically
  removes the same class of typo without the coupling.

- **`--target` on `login`, `logout`, and `whoami`.** A target carrying no
  `match_*` selectors never selects itself, so it was previously reachable only
  by exporting `WITAN_TARGET`.

- **`store_merge` — `witan migrate merge` through the MCP tier (ADR-0007 D5).**
  A user with `remote_url` + `witan login` can now merge their local store into
  the deployed graph with no kubectl, no port-forward and no AWS credentials.
  The client exports its own store and ships rows in batches; the server
  reconciles each batch against the graph it already holds a client on and
  writes the winners — **as the calling user**, evaluated by Cedar, rather than
  as `svc-witan-admin`, which is what the in-cluster fallback records.
  `--dry-run` works over this path, and re-running is a no-op.

- **`task_unlink`** — remove a link recorded backwards or against the wrong
  slug. Removing a `blocks` edge is how a task wrongly marked blocked becomes
  ready again.

- **Structured logging and OpenTelemetry** (`witan_core.observability`), wired
  into `witan serve`, plus Cedar policy bundles shipped in the image with
  membership rendered at boot.

### Fixed

- `migrate merge` addressed every store as `--store <uri>`, which omnigraph
  0.8.1 rejects for an http(s) target — a remote graph is reachable only as
  `--server <url> --graph <id>`, so the merge failed at its first export
  against any deployment. It also now accepts an `omnigraph export` JSONL as
  its source, which matters because a Lance store embeds absolute paths and
  therefore cannot travel; only its export can.
- Unleased `in_progress` tasks are treated as held rather than free.
- Unprovisioned policy groups are dropped instead of rendered empty.
- MCP tools refuse positional arguments instead of guessing their names.

### Changed

- **Requires `witan-core>=0.10`** — `remote/proxy.py` imports
  `witan_core.chunking` and `config.py` imports `resolve_config_path`, neither
  of which exists in 0.9. The previous `>=0.9` floor would let an external
  install resolve a witan-core this server cannot import.

## [0.9.0] - 2026-08-01

### Added

- **`witan session sweep` — close sessions that leaked open.** ~10 sessions in
  the graph today have no `ended_at`, residue of the temp-file session
  mechanism that never worked against the deployed service (fixed going
  forward by the tool-returned handle, but that fix cleans up nothing). An open
  session is not cosmetic: `workflow_project_complete` folds every linked
  session into the corpus trace, so a leak inflates `session_count`,
  contributes its phase having recorded nothing, carries no handoff summary,
  and cannot extend `duration` (computed from `max(ended_at)`). It also drives
  the context hook's "N sessions in <phase>" staleness nag on projects that are
  progressing fine.

  `--older-than` (default `6h`) keeps the one legitimately-running session
  safe; dry-run by default, `--yes` performs it; `--project` narrows the scope.
  The sweep summary says plainly that it was a sweep and that nothing is known
  about what the session did — these were never checkpointed, so borrowing the
  Stop hook's wording would be a lie in the corpus. Idempotent (re-closing just
  re-stamps `ended_at`), and it clears the local handle so a later Stop hook
  doesn't try to re-close what was just swept.

  Dispatches through `_srv()`, not a direct `OmnigraphClient` — working only
  locally is the exact bug that created this backlog. That needed a listing on
  the tool surface, so **`workflow_session_list`** is new too (`project_slug`,
  `open_only`; superseded sessions always excluded). Under a deployment the
  per-actor client scopes it to the caller, so a sweep cannot reach a
  teammate's sessions.

- **`witan project update`** — the CLI half of `workflow_project_update`, which
  shipped without one. Surfaces `--title`, `--description`, `--repos` /
  `--add-repo` / `--remove-repo`, `--tags`, `--github-issue` and `--status`,
  and keeps the tool's two refusals: no `--phase` (that belongs to `project
  advance`, so transitions stay behind the ordering check) and no `--status
  completed` (that belongs to `project complete`, which seals a corpus trace).
  Fixing a stale project description is most urgent exactly when the graph is
  misbehaving, and a human at a terminal shouldn't need an agent session to do
  it. (#144)

- **`memory_update` and `memory_delete` — repairing a memory no longer means
  dropping to `omnigraph export`/filter/`load`.** A memory written against the
  wrong `repo` silently vanished from every repo-scoped read, and the only fix
  on the MCP surface was to store a duplicate and supersede the original, which
  leaves the mis-scoped node in the graph forever. `memory_update` is a
  per-field-optional read-merge-write over the existing `update_memory`
  mutation (partial updates preserve omitted fields; `repo` is case-folded
  through `normalise` so the correction actually matches what `repo.detect`
  returns). `memory_delete` hard-deletes, guarded by `confirm=True` plus an
  author check, and returns the deleted node so an accidental delete can be
  re-stored from the tool result.

  Deletion is documented — in both tool descriptions and the server
  instructions — as **graph hygiene, not secret erasure**: a deleted memory
  stays fully readable from any prior commit via `omnigraph query --snapshot`,
  so the answer for leaked credentials is rotation, and scrubbing history is an
  admin `omnigraph cleanup`. Soft delete was rejected: `superseded_by` already
  occupies that role, and two hide-mechanisms on one node type is worse than
  one. Neither tool is `_ADMIN_ONLY` — both are per-user and author-scoped, so
  they work over the remote CLI like the rest of the memory surface. (#145)

### Changed

- **`ActorTokenResolver` moved to `witan_core.identity`,** joining
  `derive_actor_id` there. witan-code resolves the same actors' tokens
  server-side now: under ADR-0005 path c the deployed MCP tier performs a
  remote indexer's code-graph writes as the caller, from the same provisioned
  `{actor_id: token}` file, in the same process `witan serve` mounts both tool
  surfaces into. `witan.identity` re-exports both, unchanged for every caller.

### Fixed

- **`witan inject-context` no longer fails the UserPromptSubmit hook on a bad
  config.** The command documents that it "always exits 0 and never blocks",
  and the rest of it honours that carefully — but `cfg_module.load()` ran
  unguarded as its first statement, so it failed before any of that machinery
  could help. Two ways in: `load_toml` raises on *any* TOML error and
  `tomllib` fails the whole document, so one stray character in a
  `[targets.*]` table takes out context injection entirely; and `load()` also
  raises for a `WITAN_TARGET` naming an undefined target, so a stale env var
  breaks the hook with a perfectly valid config file. Now guarded the same way
  `session-checkpoint` already was — including `SystemExit`, which is not an
  `Exception` — with the reason on stderr under `--debug` and stdout left
  empty. `load_toml` itself stays strict: failing loudly is right for `witan
  serve`; the hook is the caller that needs to be forgiving, and it is the
  caller that now says so.

- **Additive schema changes now reach an existing local store.**
  `_ensure_graph` early-returned on `store.exists()`, so `init` + `schema
  apply` ran only when a store was first created. After that, a new node type
  or field added to `schema.pg` never reached it, and the failure mode was a
  query erroring or silently returning nothing against a store one revision
  behind. The remedies all existed but all required knowing to run them after a
  version bump: the `apply_schema` admin tool, `witan migrate schema`, or
  re-running `install.sh`.

  An existing store is now re-applied when `schema.pg`'s mtime differs from the
  stamp beside it — witan-code's approach, now shared via
  `witan_core.omnigraph`. Remote (http/s3) URIs stay a no-op: a deployment's
  schema is provisioning's job, and `schema apply` against a server takes a
  different argument form entirely. The re-apply cannot raise, because
  `_ensure_graph` runs at import time and a failure there would take down
  `witan serve` at startup; a failed apply leaves the stamp unwritten and is
  retried next call. Creation keeps `check=True`.

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
