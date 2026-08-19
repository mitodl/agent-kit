# Changelog

All notable changes to `witan-council` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (pre-1.0:
a MINOR bump may include breaking changes).

## [0.17.5] - 2026-08-19

### Changed

- **Collapsed several more multi-commit write paths to one commit each**,
  continuing #226's audit: `task_link`'s `blocks` and `parent` kinds (edge +
  denormalized field sync), `workflow_project_block` (edge + `blocked_by`
  sync), and `workflow_trace_mine`'s trailing writes (an `Informed` edge per
  mined memory plus the trace's own annotation update, previously N+1
  separate commits after the mined memories themselves). Nothing about what
  gets written or returned changes — only how many Lance commits it costs.
  `workflow_project_unblock`/`task_unlink`'s edge-removal paths are
  deliberately NOT touched here: `_unlink_edge`'s delete-then-reinsert
  sequence can't share a commit with the trailing field update anyway —
  omnigraph rejects a mutate body mixing deletes with inserts/updates — so
  batching those needs a different shape, not this pass's mechanism.

## [0.17.4] - 2026-08-18

### Fixed

- **`task_claim` no longer leaks a raw omnigraph conflict to callers under
  write contention.** Fixes
  tk-task-claim-exhausts-its-3-attempt-no-backoff-cas-674414: the CAS retry
  loop fired 3 immediate, unbacked-off attempts and, on exhaustion, re-raised
  the raw `OmnigraphConflict` — surfacing omnigraph's internal "write
  authority ... changed during preparation" text straight through the MCP
  boundary whenever an unrelated write kept colliding on a hot table (most
  often `node:Task`, written by every claim/update/close across every
  session). Widened the retry budget to 5 attempts, added jittered backoff
  between them, and report exhaustion as a structured `{"claimed": false,
  "reason": "contention"}` instead.
- **A CAS retry no longer risks resurrecting a task that closed mid-retry.**
  `_update_task`'s merge sets `status` from the caller's `claim` dict
  unconditionally, regardless of what its own fresh read shows, so a retry
  that didn't revalidate first could silently revert a close (or a new
  block) committed during the backoff window back to `in_progress`. The
  post-conflict re-read now checks for `closed`/`blocked` and reports that
  reason instead of ever looping back into a write that would stomp it.

## [0.17.3] - 2026-08-18

### Fixed

- **`task_claim`'s post-write verification no longer trusts a stale
  re-read.** Fixes tk-mutual-exclusion-violated-2-of-8-racers-both-got-52b3dd:
  under concurrent load, an unconstrained verification read could return a
  snapshot up to 2 seconds older than a rival's write that had already
  committed, letting two racers both observe themselves as the winning
  claimant. The verification read stays unconstrained (so it can still see
  a legitimate later clobber — a rival `force` claim, a concurrent
  `task_update`) but now retries, comparing its own reported
  `graph_commit_id` against the claiming write's, until it has caught up.
  Raised the `witan-core` floor to `>=0.25` for `change()`'s widened return
  value, which supplies that comparison floor.

## [0.17.2] - 2026-08-18

### Changed

- **Raised the `witan-core` floor to `>=0.24`** and requested its new
  `sentry` extra. 0.23 has no `sentry` extra published, so a lower floor
  would let an external install resolve a witan-core that silently lacks
  Sentry support (or errors on the unknown extra) rather than getting the
  `SENTRY_DSN`-gated reporting this release adds.

## [0.17.1] - 2026-08-18

### Added

- **Claim tracing: `witan.task_update.conditional` and
  `witan.task_claim.verify`.** Together they record, per racer, the
  `graph_commit_id` its conditional claim FENCED against and the one its
  post-write verification read was SERVED AT.

  Diagnostic for a mutual-exclusion violation observed against CI on
  2026-08-18, where two of eight racers both left `task_claim` with
  `claimed: true` on the same task. That failure is **silent** — all eight
  handlers reported `outcome: ok` with normal durations — so nothing in the
  existing telemetry separates a correct claim from a double one, and the first
  investigation could only infer a mechanism it had no way to test.

  The pair is what makes it testable: two racers verifying at *different*
  commits while each sees itself proves a stale verification read directly;
  verifying at the *same* commit and still disagreeing kills that hypothesis.
  `unconditional_fallback` covers a third case nobody has checked — a tier
  supplying no `graph_commit_id` sends the write unconditional, which would
  explain a lost mutual exclusion with no staleness involved.

  Kept at INFO rather than DEBUG on purpose: claims are low-frequency, and this
  is the record of which of two callers was actually granted a task.

## [0.17.0] - 2026-08-17

### Changed

- **`task_claim` is now a real compare-and-swap.** `_update_task(conditional=…)`
  states the `graph_commit_id` its own read saw, so the claim applies only while
  the branch head has not moved (omnigraph #470, witan-core 0.23.0).

  ★ The precondition comes from `_update_task`'s **own** read, not from the
  caller. That is the invariant the whole thing rests on: the merged row is
  built from that snapshot, so "the head has not moved" is exactly "nothing
  changed under the values I am about to write back". A token from any earlier
  read — including `task_claim`'s own status check — would fence the wrong
  interval and be worse than no precondition, because it would look rigorous
  while guaranteeing nothing.

  What this buys is a **truthful** refusal, not a rarer one. Losing was
  previously inferred from a conflict that might have belonged to someone else;
  a 412 is the store saying the write did not apply. Expect MORE conflicts than
  contention, since the precondition is the whole branch head — an unrelated
  `memory_store` invalidates it exactly like a rival claim.

  Degrades rather than fails: a tier supplying no `graph_commit_id` (pre-#470,
  or the CLI path) writes unconditionally, which is the previous best-effort
  claim. The post-write verification therefore STAYS — it is what covers that
  path, and removing it would make correctness depend on a server capability
  this code cannot see from where it runs.

- **Raised the `witan-core` floor to `>=0.23`.** Mandatory, not cosmetic:
  `_update_task` calls `read_with_commit`, added to witan-core in the same
  change, so 0.22 raises AttributeError on every task update.

## [0.16.1] - 2026-08-17

### Fixed

- **Raised the `witan-core` floor to `>=0.22`, so a published install cannot
  ship a `task_claim` that fails to keep its contract.** 0.22 classifies a
  write-authority conflict (HTTP 409) as retryable; `_retry_loop` raises
  `OmnigraphConflict` only for that classification, and `task_claim` catches
  exactly that exception to re-read and answer `{"claimed": false, "reason":
  "lost_race"}`.

  On witan-core 0.21 the classification is `FATAL`, so that branch is
  unreachable and the losers of a race get an opaque `RuntimeError` instead of
  the structured refusal ADR-0003 has parallel agents rely on — measured
  against QA at 6 of 8 racers. Mutual exclusion was never affected; exactly one
  racer wins either way.

  Note this is a **behavioural** floor, the first in this package — every other
  entry in that pin's comment is an import floor, where an older witan-core
  fails loudly at load. This one imports cleanly and is wrong quietly, which is
  why it is documented at length rather than appended to the list.

## [0.16.0] - 2026-08-17

### Fixed

- **witan's `async` tools ran blocking omnigraph subprocesses on the event
  loop, stalling every other request — including `/health`.** FastMCP
  dispatches *sync* tool functions through a threadpool, so they may block
  freely. Six tools are `async def` — solely so they can `await` an elicitation
  helper — and those run *on* the loop. Each then called straight into
  `client.read`/`change`/`change_many`, which shell out to the `omnigraph`
  binary. While that subprocess ran, nothing else could be scheduled.

  ★ **The CPU figure is what identifies it.** Measured in QA at 16 concurrent
  writers: the pod used **0.011 cores** — 1.1% of one CPU, with no limit set —
  while `/health` failed probes at 5s *and* 10s, readiness ejected the only
  replica, and the gateway returned 503 to every caller while all 16 writes
  committed. A loop that is merely *busy* burns CPU. A loop that cannot
  schedule a trivial coroutine for ten seconds at 1% CPU is *blocked*, waiting
  on I/O it never yielded from.

  All 19 store calls across the six tools now route through `_offload`, which
  runs them in a worker thread. Wrapping each call rather than the whole
  handler is deliberate: the loop is released *between* calls too, so a tool
  making several lets other coroutines interleave.

  ★ **`_offload` carries a context, and that is not ceremony.** Redaction
  notices live in a `ContextVar`, which is *copied* into a worker thread rather
  than shared — the property that stops one caller's redactions bleeding into
  another's. `scan.notice` states the resulting invariant plainly: "`record`
  and `annotate` must run in the SAME context." The first version of this fix
  ignored it, and three tools began rewriting content while reporting nothing —
  the exact data-loss path that module exists to close. Caught by the existing
  `test_scan_notice` tests. `_offload` now runs the call inside a `Context` it
  owns and merges the notices back via `notice.adopt`.

  This was never a regression from any recent change: it has been there as long
  as these tools have been `async`, masked by ToolHive's 30s deadline failing
  the call before the pattern could show itself.

### Added

- `test_event_loop_blocking.py`, a structural guard asserting no `async def`
  tool reaches the store without `_offload`. Functional tests cannot catch this
  — the store returns the same rows whether or not the loop was blocked, so it
  was only ever visible in production as probe timeouts. Includes a test that
  the guard itself can fail, since a pure AST walk over a clean file looks
  identical to a broken detector.

## [0.15.0] - 2026-08-16

### Fixed

- **`witan serve` no longer drops in-flight requests two seconds into a
  shutdown.** FastMCP builds its uvicorn config with a hardcoded
  `timeout_graceful_shutdown: 2`, so on SIGTERM the server stopped accepting
  connections, gave running requests two seconds, and dropped the rest. A witan
  write has been measured at **27s** under load, so every one in flight was
  severed by a deploy, an eviction or a node drain — and a severed write is
  precisely the indeterminate outcome a caller cannot safely retry.

  `serve` now passes `uvicorn_config={"timeout_graceful_shutdown": …}`,
  defaulting to 120s to match the request budget the deployment enforces at its
  gateway, and settable via `--shutdown-grace-seconds` /
  `WITAN_MCP_SHUTDOWN_GRACE_SECONDS`.

  ★ **This could not be fixed from the deployment side.** ol-infrastructure sets
  `terminationGracePeriodSeconds: 150` so the kubelet waits — but uvicorn
  declined to use it, so the pod-side setting bought time nothing spent. Both
  halves are required, and the pod-side half is the one that looks sufficient;
  a comment there asserted exactly that until this was found.

  Verified against the real library rather than the test double: with
  `uvicorn.Config` instrumented, `120.0` arrives where fastmcp's `2` would have
  been. stdio runs are unaffected — they pass no uvicorn config at all.

## [0.14.0] - 2026-08-16

### Fixed

- **`witan login` was broken for anyone installing from PyPI**, with an
  `ImportError` before the CLI could start:

  ```
  ImportError: cannot import name 'SessionLife' from 'witan_core.remote.oidc'
  ```

  0.13.0 imports `SessionLife` at module scope in `witan/remote/oidc.py`, but
  its floor was `witan-core>=0.20`, and 0.20.0 does not export it. The symbol
  and its caller landed in the same change (#239) without bumping either the
  library's version or this floor. The workspace resolves witan-core by path,
  so every test passed while a `uv tool install` resolved a pair that cannot
  import each other.

  Floor raised to `witan-core>=0.21`. This is the **fifth** time this exact
  failure has shipped, and the first to reach a user — the pin comment now says
  so, at the place where the next person will be adding a symbol.

  Nothing in CI catches this; only a real install does.

## [0.13.0] - 2026-08-15

### Added

- **An unauthenticated `GET /health` route**, so witan can be probed by a
  kubelet directly and no longer needs a ToolHive proxy in front of it to be
  deployable. Returns `{"status", "service", "version"}`; the version is the
  installed `witan-council` distribution, which makes "did my image actually
  roll out?" a `curl` instead of an exec into the pod.

  **It is deliberately shallow and must stay that way.** The handler answers
  from process state alone and never touches the graph. A probe that checked
  the data tier would be the exact failure that took the deployed service down
  on 2026-08-12: ToolHive's proxy `/health` synchronously pinged its backend,
  a burst of concurrent writes saturated that backend, the ping stopped
  answering, and the kubelet's 5s liveness probe killed a container that was
  working perfectly — turning a slow write queue into ~60s of outage for
  readers too. Depth converts backend *slowness* into frontend *death*, and
  fires precisely when killing the pod is most harmful. A graph outage is real
  and belongs in alerting on the spans witan already emits, where it degrades
  a dashboard instead of a pod. A test asserts the handler cannot reach the
  client at all, so a later "just one quick lookup" fails CI rather than
  production.

  Unauthenticated because the kubelet carries no bearer token. The exemption
  is the route, not the auth provider: fastmcp applies `auth=` to the protocol
  endpoint only, verified against 4.0.0b2 by a test that mounts this same
  handler on a JWT-guarded server and asserts `/health` 200 alongside `/mcp`
  401.

## [0.12.0] - 2026-08-13

### Changed

- **The first call of every agent session costs one Lance commit instead of
  four.** `workflow_session_start` wrote its session row, the project's repo
  set, the CodeBranch upsert and the ForProject edge as four separate `mutate`
  calls; against the deployed store each is a full commit cycle against S3 at
  ~3.5-4s, so the call spent 14-16s of the deployment's 30s deadline before any
  other user contended for anything. `task_update(parent=…)` was three commits
  and is now one. Nothing about what gets written changes
  ([#226](https://github.com/mitodl/agent-kit/pull/226)).
  The CodeBranch helpers now return their mutations for the caller to commit —
  edges may reference a node inserted earlier in the same body, so a node and
  its edges legitimately share a commit. `task_claim` deliberately keeps a
  standalone commit: its claim is a compare-and-swap with a post-write
  verification read, so the branch metadata must only land once the claim is
  known won.
- A write cut off by a gateway (502/504) surfaces as
  `RemoteWriteIndeterminate` rather than "the deployed service could not be
  reached", and the CLI renders it as a sentence. `witan migrate merge` says
  the opposite of the generic advice, because it can: the merge reconciles
  newest-record-wins, so re-running it is the remedy rather than the risk
  ([#225](https://github.com/mitodl/agent-kit/pull/225)).

### Fixed

- `parent_slug` and the `ParentOf` edge are two encodings of one fact and were
  written in separate commits, so a reader landing between them saw a task
  parented one way and not the other. They now land together or not at all —
  and `task_update` against a task that does not exist writes nothing, edge
  included.
- `test_zero_weights_preserve_bm25_order` was flaky, red on main since
  2026-08-10, and wrong in a way that predated the flake: it compared
  `search_all` against `memory_search`, which resolves a repo first and so runs
  `search_by_repo`. Two different queries. Separately, omnigraph 0.9.0 returns
  BM25-**tied** rows in nondeterministic order — identical calls, same process,
  same store, came back one way 6/6 in one store and split 4/2 in the next,
  where 0.8.1 was stable across 15 runs. The re-rank test now supplies its own
  seed, so it asserts the property it is named for; a companion test asserts
  set equality end-to-end, which is the part that is stable.

## [0.11.6] - 2026-08-10

Client-side preparation for omnigraph 0.9.0
([#217](https://github.com/mitodl/agent-kit/pull/217)). The omnigraph pin is
unchanged; this is what has to be true before it can move.

### Fixed

- Every cross-store merge died on omnigraph 0.9.0. Its `export` renders a
  `DateTime` as integer epoch milliseconds where 0.8.x wrote a naive ISO-8601
  string, and `_parse_ts` fed that to `datetime.fromisoformat`, whose
  `except ValueError` does not catch the `TypeError` an int raises. Both
  representations are now read, because `witan migrate merge` accepts a `.jsonl`
  export taken on another machine and old exports outlive the stores that wrote
  them.

  Milliseconds, not microseconds — `omnigraph commit list` reports *its*
  timestamps in microseconds, so the obvious fix by analogy with existing code
  in this repo is wrong, and wrong silently: it dates every row to January 1970
  and inverts which side of a merge wins.
- `merge_store` sent its whole reconciled set through one unchunked
  `load_batch`. That was safe while the only ceiling was the served request
  body; 0.9.0 caps keyed writes at 8,192 rows per table in the engine, local
  stores included, so a merge of more than `LOAD_MAX_ROWS` rows of one type is
  now refused. Chunked via `chunk_records`, which also guarantees the
  nodes-before-edges ordering this path wants. Batches commit independently, so
  a part-way failure leaves the earlier ones applied — recoverable by
  re-running, since reconciliation makes a re-sent row lose to its own
  already-applied copy.
- `migrate_storage_format` aborted instead of finding a usable binary.
  `_find_pre_upgrade_binary` returned the installer's set-aside copy
  unconditionally, but `OmnigraphClient._find_binary` resolves `PATH` *before*
  `~/.local/bin`, so a Homebrew install can be current while an unrelated stale
  backup sits beside it. It now collects candidates — set-aside copies
  newest-first, then `PATH` — and proves each by opening the store with it.
  Which binary can read a given store is not decidable from names or versions.

### Changed

- Requires `witan-core>=0.16`, for `omnigraph_install.preserved_binaries()`.
  The workspace resolves witan-core from the local path, so a stale floor is
  invisible here and only bites a published install — at the worst moment,
  since this code path runs during a format-break recovery.

## [0.11.5] - 2026-08-07

### Fixed

- A server-side tool error on a remote target reads as a sentence instead of a
  traceback. `fastmcp.exceptions.ToolError` is not a `RuntimeError`, so it
  missed every `except RuntimeError` in the CLI: against a local store a failing
  tool gave one red line, and against a deployment the same failure gave ~40
  lines of cyclopts/asyncio/fastmcp internals with the message on the last one.
  Found during the first live cutover on
  `WITAN_TARGET=ci witan migrate merge <store> --dry-run`; the message the user
  needed was real and useful, and simply buried.
- The fix is not merge-specific. Only `witan migrate` has `except RuntimeError`
  of its own, so `main()` now also catches `RemoteToolFailed` — which is what
  covers memory, tasks, projects and traces, where a Cedar denial on the shared
  deployment is the common way to hit this.

### Changed

- Floor bumped to `witan-core>=0.14` for `remote.proxy.RemoteToolFailed`, which
  `witan/remote/proxy.py` re-exports at module scope. Bumped in the same change
  that adds the caller, as the pin comment asks — the workspace resolves
  witan-core by path, so nothing in CI can catch a stale floor.

## [0.11.4] - 2026-08-07

### Fixed

- Floor bumped to `witan-core>=0.13`: `witan/graph.py` re-exports
  `StoreUnavailable` (added to `witan_core.omnigraph` in 0.13.0) at module
  scope. The `>=0.12` floor left in place after that addition resolved a
  published `witan-core` that could not satisfy the import, so a plain
  `pip install`/`uv tool install witan-council` (or the `ol-agent-kit`
  meta-package) failed at startup with
  `ImportError: cannot import name 'StoreUnavailable' from 'witan_core.omnigraph'`.
  No behavior changes in this package; the `store_merge`/`export_to`
  merge-resilience fix that introduced the symbol landed earlier and is
  unaffected — this release only closes the floor gap it left behind.

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

- Content matches *seed* ahead of title-only matches: the two BM25 runs are not
  on a comparable scale, so title hits are appended rather than interleaved by
  score, which gives content hits the higher positional proxy (`_rerank`'s
  `norm_bm25`). This is a seeding order, not a guarantee about the final
  result — the proxy is one weighted term in `_score` alongside recency,
  corroboration and confidence, so a well-corroborated title-only hit can
  finish above a marginal content hit, exactly as it can among content hits.
- `memory_search` applies its 20-row cap *after* supersession pruning rather
  than before. `_search_rows` now returns the full candidate union and the
  caller caps. Capping candidates first meant 20 superseded content hits could
  occupy every slot, discard the title hits behind them, and prune to an empty
  result with a valid title match sitting just past the cut. `recall` already
  capped after pruning and just sees more candidates.

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
