# Changelog

All notable changes to `witan-core` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (pre-1.0:
a MINOR bump may include breaking changes).

## [0.20.0] - 2026-08-14

### Fixed

- **Tool-call spans now really do join the caller's trace.** 0.19.0 claimed this
  and did not deliver it; see the correction under that release below.

  ToolHive injects W3C trace context into **HTTP headers** — both hops, and only
  there (`transparent_proxy.go`, `vmcp/client/client.go`). FastMCP reads the MCP
  `_meta` object (`server/telemetry.py:_get_parent_trace_context`). ToolHive's
  `_meta` injector exists but nothing calls it. So the context crossed the
  boundary intact and neither side picked it up, and every span here started a
  rival root trace.

  Fixed with an ASGI middleware
  (`witan_core.observability.asgi.TraceContextASGIMiddleware`) that adopts the
  request's `traceparent` and creates **no span of its own**. FastMCP uses
  ambient context when `_meta` is empty, so attaching is sufficient and its
  `tools/call` span becomes the joined root. Wired in `witan serve` through
  FastMCP's `http_app(middleware=...)` hook.

  `opentelemetry-instrumentation-starlette` was the obvious alternative and was
  measured: it also joins, but emits a SERVER span plus four `http
  receive`/`http send` children per request (~3.5x span volume), and its
  `exclude_spans` option is not honoured through the global instrumentor. The
  attach-only middleware gets the same join at one span per call with no added
  dependency.

### Removed

- `ObservabilityMiddleware`'s `_meta` parent extraction, added in 0.19.0. It
  duplicated what FastMCP already does one layer up, against the same empty
  `_meta`. Its span still nests correctly — now because the ASGI layer has
  adopted the context before FastMCP builds anything.

## [0.19.0] - 2026-08-14

> **Correction (0.20.0):** the fix below did not work, and its description of
> the mechanism is wrong. ToolHive propagates over HTTP headers, not `_meta`;
> its `InjectMetaTraceContext` is dead code with no callers. Extracting `_meta`
> in the middleware duplicated FastMCP's own extraction and changed nothing.
> Verified against QA on witan-core 0.19.0: witan spans were still separate
> roots (`serviceStats {qa-witan: 3}`, ToolHive absent).

### Fixed

- **Tool-call spans now join the caller's trace instead of starting their own.**
  ToolHive propagates W3C trace context through the MCP `_meta` object rather
  than an HTTP header (`InjectMetaTraceContext`,
  `pkg/telemetry/propagation.go`), and nothing in this process reads headers —
  there is no ASGI instrumentation. `ObservabilityMiddleware` started every span
  against ambient context, so every one of them was a parentless root.

  That broke two things at once. witan's spans never appeared in ToolHive's
  trace, leaving the proxy → witan boundary unmeasurable per request — the exact
  capability the ToolHive telemetry rollout (ol-infrastructure#5414) was
  justified by. And `OTEL_TRACES_SAMPLER=parentbased_traceidratio` quietly
  degraded to its *root* sampler, re-rolling the ratio locally instead of
  inheriting the decision already made upstream.

  Measured in QA on 2026-08-14 before the fix: six authenticated `memory_search`
  calls produced a 12-span trace spanning `qa-witan-vmcp` and
  `qa-witan-mcp-proxy` with witan in none of it, zero traces under `qa-witan`
  over a full hour, and yet `witan_tool_calls_total` and six `mcp.tool_call` log
  lines from that same middleware — metrics exporting while traces were sampled
  away for want of a parent.

  Local CLI and stdio use are unaffected: no `_meta` means no parent, which is
  the correct root-span behaviour there.

## [0.18.0] - 2026-08-13

Makes write admission answer the question that decides the outcome: *will this
write finish before the caller stops waiting?*

### Changed

- **Write admission is now predictive.** The gate previously bounded concurrency
  (4 in flight) and how long a write would wait for a slot (10s), and neither
  predicts how long the work takes once admitted. Watched live at 24 concurrent
  writers, with the gate active and holding the deadline it needed: 56 handlers,
  durations climbing 3s to 73s, 26 of them past the caller's 30s budget, and
  ZERO refusals — a slot always freed inside the wait cap, so it admitted write
  after write into a system that could not serve them. It let through exactly
  the writes that strand, and a stranded write is the indeterminate one:
  committed while its caller is told it failed
  ([#232](https://github.com/mitodl/agent-kit/pull/232)).

  The gate now measures how long an *admitted* write actually takes against each
  graph and refuses when that no longer fits the call's remaining budget. Queue
  time needs no separate accounting — the deadline is absolute, so every second
  waited shrinks what the estimate is checked against.

  Three properties are load-bearing and should survive any rewrite: the estimate
  measures EXECUTION and not the wait (folding queue time back in double-counts
  it, and one burst leaves the gate convinced every write costs a minute); an
  idle graph is never refused (otherwise a graph slower than the budget declines
  every write forever and never gathers a faster sample); and a cold gate admits
  (a process must not refuse the writes it needs in order to learn what a write
  costs).

  This does **not** raise the ceiling. The same writes succeed and throughput is
  unchanged. What changes is that the ones which cannot are refused before
  anything is sent — an ordinary retryable error instead of a write whose
  outcome nobody can determine.

- A doomed write is refused on arrival rather than after the queue timeout, and
  the wait is bounded by the last moment admission could still be viable —
  `Condition.wait` does not wake when the budget runs out, so an unbounded wait
  sleeps past its own deadline and is then refused for having waited.

## [0.17.0] - 2026-08-13

Makes a saturated deployment answerable. Measured against the CI deployment:
concurrent writes 502 at exactly 30s, and counting the rows afterwards showed
two runs disagreeing. The first — bursts of 4, then 8, then 16 writers — had
committed every one of its 28 writes despite 502ing on most of them; the
second, a standalone 16-writer burst, committed only 14 of 16. So the deadline
cuts the *response*, the backend usually finishes the write anyway, and
nothing in the reply distinguishes the two —
while the client reported all of it as "could not be reached", which is wrong
twice over and invites the retry that duplicates the row
([#225](https://github.com/mitodl/agent-kit/pull/225)).

### Added

- `WriteQueueFull` and a process-wide, **per-graph** in-flight bound on remote
  writes, applied around the whole call in `OmnigraphClient._http_execute`.
  Measured: 4 concurrent single-row writes take 15.5s wall, 6 take 31.2s, 8
  take 51.1s — against a 30s deadline — so past ~4 in flight the queue provably
  cannot drain in time. Per graph because the serialisation is: writes to two
  different graphs each finish in their solo time. Reads are ungated; they hold
  flat at ~5 req/s from 8 to 36 concurrent readers.
  The refusal says the one thing a 502 never can: nothing was written.
- `REMOTE_WRITE_MAX_INFLIGHT_ENV_VAR` (`WITAN_REMOTE_WRITE_MAX_INFLIGHT`,
  default 4) and `REMOTE_WRITE_QUEUE_WAIT_ENV_VAR`
  (`WITAN_REMOTE_WRITE_QUEUE_SECONDS`, default 10), read **at call time** so
  `kubectl set env` moves them on a live pod. An unusable value logs and falls
  back rather than raising — a typo in a Deployment env var must not turn every
  write into a crash. `0` is legal and refuses every remote write, which is a
  real incident answer.
- `REMOTE_CALL_BUDGET_ENV_VAR` — how long the caller's own deadline allows.
  Unset means unbounded, which is right for a CLI; a deployment that knows it
  is cut at 30s sets it and gets the fail-fast below.
- `remote.proxy.RemoteWriteIndeterminate` and `gateway_failure()`. A 502/504 on
  a **write** now reports its outcome as unknown and says to re-read before
  retrying, instead of claiming the service was unreachable. A read cut the same
  way stays `RemoteUnreachable` — nothing was dispatched — but stops asserting
  the service was down when it demonstrably answered. 503 is deliberately not in
  that set: no upstream was tried, so "did not happen" is still exact.
- `omnigraph_http.parse_retry_after()` and `Outcome.retry_after`.

### Changed

- The admission-cap backoff prefers the server's own `Retry-After` over its
  blind schedule, where the HTTP transport can see one (the CLI's error path
  discards response headers, so the subprocess path is unchanged). A hint longer
  than the call's remaining budget fails immediately, quoting the number, rather
  than sleeping through a deadline it cannot meet — a sleep that outlives the
  connection turns a late write into an unknowable one.
- Gateway status is classified **before** transport-exception type. An APISIX
  502 arrives as an `httpx2.HTTPStatusError`, which *is* an `httpx2.HTTPError`,
  so asking about the transport first buried every one of them under "could not
  be reached". Same ordering trap the 413 handling already carried a note about.

## [0.16.0] - 2026-08-10

Prepares for omnigraph 0.9.0, which changes the on-disk storage format and —
undocumented — the export wire format. Nothing here bumps the omnigraph pin;
this is the client-side work that has to land first
([#217](https://github.com/mitodl/agent-kit/pull/217)).

### Changed

- **BREAKING:** `omnigraph_install.preserved_binary()` is now
  `preserved_binaries()`, returning `list[Path]` newest-first instead of
  `Path | None`. There is no single "previous binary": witan-code keeps one
  `<slug>.omni` per repository, each migrated only when that repo is next
  opened, so a machine holds stores at several formats at once and the caller
  has to try candidates in turn. Callers should probe — open the store with
  each until one works — rather than trust the first.
- The installer no longer prunes older set-aside binaries. It kept only the
  newest, on the reasoning that a store has one writer; true per store and
  irrelevant, because there are many. Crossing two format versions while a repo
  lay untouched deleted the only binary able to export it.
- `chunking.chunk_records()` takes `max_rows` (default `LOAD_MAX_ROWS`, 8,000)
  and bounds batches by rows per table as well as by bytes. omnigraph 0.9.0
  caps a keyed write at 8,192 rows per table — enforced by the engine, on local
  stores too, not just over HTTP. 20,000 small rows is ~4.5 MiB, inside every
  byte budget here, and was refused outright.

### Added

- `omnigraph_install.preserved_binaries()` (above) and
  `default_install_path()`.
- `omnigraph_install.reported_internal_schema()` — the on-disk format version a
  binary reads, from `omnigraph version`. Raises rather than returning a
  sentinel: every caller is comparing against a declared value, and a
  comparison against "unknown" that quietly passes is the failure this exists
  to prevent.
- `_OMNIGRAPH_INTERNAL_SCHEMA`, declaring the storage format the pinned release
  is expected to read. Renovate cannot write it, so a format-breaking bump
  leaves it disagreeing with the binary — which is what
  `bin/check_omnigraph_format.py` turns into a failing check.
- `chunking.LOAD_MAX_ROWS`.

### Fixed

- An upgrade destroyed the only binary that could rescue a store it had just
  orphaned. `install_omnigraph` replaced `~/.local/bin/omnigraph` in place and
  kept no copy, so after a format bump `witan migrate storage` — whose first
  step is to export with the *old* binary — asked for something the same tool
  had deleted. Every previous version is now set aside as
  `omnigraph-<version>`.

## [0.15.0] - 2026-08-07

### Fixed

- `DeviceAuth._write_cache` no longer writes through a fixed `tokens.json.tmp`
  it unlinks first. Every agent process on a machine shares one token cache, so
  that fixed name meant concurrent writers deleted each other's in-flight temp:
  a synchronised burst of 24 refreshes lost 58–83% of its writes to
  `FileExistsError` (`O_EXCL` lost the race) or `FileNotFoundError`
  (`os.replace` after someone else's unlink), and every lost write was a token
  refresh that never landed. The temp name is now unique per writer
  (`<cache>.<pid>.<uuid>.tmp`) and there is no pre-unlink, so `O_EXCL` still
  guarantees we created — and therefore own the `0600` mode of — the file we
  write. `os.replace` was already atomic, so no cache was ever corrupt; the
  writes simply failed. The unlink the unique name replaces also did one useful
  thing — it reclaimed a temp left by a hard-killed writer — so `_store_token`
  now sweeps `<cache>.*.tmp` files older than a minute while it holds the lock,
  rather than accumulating `0600` fragments in the config directory forever.
- `DeviceAuth._store_token` and `DeviceAuth.logout` now take an exclusive
  `flock` on `<cache>.lock` around their read-modify-write of the whole cache
  file. Two processes refreshing *different* deployments concurrently each
  wrote back a snapshot taken before the other's, so one deployment's entry was
  silently dropped and that target reported "Not logged in".
- `DeviceAuth.get_valid_token` single-flights the refresh across processes: it
  takes the same lock and **re-reads the cache after acquiring it**, because the
  holder before it has very likely just stored a fresh token. A fleet of agents
  started together re-converges on the ~5-minute access-token expiry forever, so
  without this every one of them refreshed at the same instant *and spent the
  same refresh token*, which a rotating IdP reads as replay — this is what put
  401s in front of the fleet during the live concurrency probe against
  `witan.ci.ol.mit.edu`. N simultaneous refreshes now collapse into one refresh
  and N-1 cache hits.

  Unlike the graph store — where the advisory flock is skipped for remote
  stores because it cannot span hosts — the token cache is a genuinely local
  file, so flock coordinates every process that shares it. The lock is
  re-entrant per `(thread, lock path)` for the same reason
  `acquire_store_flock` is: `get_valid_token` → `_refresh` → `_store_token`
  nests, and `flock` conflicts between two file descriptors even within one
  process.

## [0.14.0] - 2026-08-07

### Added

- `RemoteToolFailed` in `witan_core.remote.proxy`: the deployment ran the tool
  and the tool refused — a Cedar denial, a missing slug, a schema mismatch, a
  bad argument. A `RuntimeError` subclass, which is the entire point: in-process
  a refusing tool raises `RuntimeError` and every CLI command's
  `except RuntimeError` renders one red line, but over MCP the identical refusal
  came back as `fastmcp.exceptions.ToolError`, which is **not** a `RuntimeError`
  (`ToolError -> FastMCPError -> Exception`). It sailed past every one of those
  handlers, so any server-side refusal on a deployed target printed ~40 lines of
  cyclopts -> asyncio -> fastmcp internals with the real message on the last one.
  Observed during the first live cutover on
  `WITAN_TARGET=ci witan migrate merge <store> --dry-run`.
- `tool_failure()` in `witan_core.remote.proxy`: the `ToolError` inside an
  exception, or `None`. Walks the chain (anyio re-raises through an
  `ExceptionGroup`, so the `ToolError` is not always outermost) for the same
  reason `_transport_failure` does.

### Changed

- `RemoteMCPProxy._reclassifying` now converts a server-side refusal into
  `RemoteToolFailed` instead of letting the raw `ToolError` propagate. This does
  not walk back its "a tool that raises server-side must keep its own error"
  rule: that rule is about not relabelling a refusal as `RemoteUnreachable` —
  about *where* the fault was, not which class carries it. The refusal keeps its
  own distinct type, the server's own words, and the original `ToolError` as its
  `__cause__`.
- The refusal branch is asked **last**, after size and transport. ToolHive's
  vMCP relays an upstream 413 as a `ToolError` too, so asking it first would
  file every relayed 413 under "the tool refused" and lose the one reading that
  tells the caller to send less rather than to fix their call. Pinned by
  `test_a_relayed_413_is_still_a_size_refusal_though_it_arrives_as_a_tool_error`.

## [0.13.0] - 2026-08-07

### Added

- `acquire_store_flock()` / `release_store_flock()` in `witan_core.omnigraph`: a
  re-entrant, thread-keyed wrapper around the advisory `<store>.lock` flock.
  `flock` is held by the open file description rather than the process, so a
  caller that takes the lock across a merge and then takes it again for a load
  inside that merge would otherwise self-deadlock; re-entrancy is now tracked
  per `(thread, lock path)` so nested acquisition within one thread is a no-op
  while two threads still exclude each other.
- `StoreUnavailable` in `witan_core.omnigraph`: raised when a store could not be
  reached for the whole `_UNAVAILABLE_MAX_WAIT` retry budget. A `RuntimeError`
  subclass, so existing callers keep catching it unchanged; it exists so a
  caller that can say something useful about an unreachable store (transient,
  safe to retry) does not have to string-match the generic failure message.
  `OmnigraphClient.export_to` and the `merge_store` source/target/load paths
  now raise it instead of letting a bare subprocess connect-refusal (e.g. a
  data-tier pod restart) escape outside the retry policy.

## [0.12.0] - 2026-08-07

### Added

- `RemotePayloadTooLarge` and `payload_too_large()` in `witan_core.remote.proxy`:
  a request body the deployment refuses for its size (HTTP 413) is now its own
  classification, with a message naming the call, the endpoint, and the fact
  that retrying cannot help because the payload itself is what was rejected.
  The message is deliberately **operation-neutral** — it fires for every tool
  call, so it claims nothing about batching or partial writes. Callers that are
  genuinely mid-batch add that context themselves, where the numbers are real.
- `describe_budget()` in `witan_core.chunking`: renders a byte budget as "2 MiB"
  for the constants and an exact byte count for anything else, so a `load` given
  a non-default `max_bytes` cannot be told about a limit that is not the one
  that refused it.
- `payload_too_large()` is public because witan-code's store session holds its
  own connection and classifies for itself; one definition keeps the two
  transports agreeing on what "refused for its size" means.

### Fixed

- A 413 from a **direct** connection is an `httpx2.HTTPStatusError`, which is an
  `httpx2.HTTPError` — so the existing transport guard reported a deployment
  that was up and answering as one that "could not be reached", sending the
  reader to check DNS for a payload they needed to shrink. Size is now asked
  before transport, and `test_a_direct_413_is_too_large_and_NOT_unreachable`
  pins the ordering.
- A 413 **relayed** by ToolHive's vMCP arrives as a `ToolError` — the HTTP
  exchange with us succeeded, so only the words carry the 413 — and escaped as
  the raw `fastmcp.exceptions.ToolError` traceback that the first live
  `witan migrate merge` against CI died with.
- Matching is by phrase, not by a bare `413`: a tool error relays the server's
  own text, which can quote the caller's data.

## [0.11.0] - 2026-08-07

### Added

- **`MCP_LOAD_MAX_BYTES` — a second byte budget, for the MCP hop.** The
  existing `LOAD_MAX_BYTES` (8 MiB) was bisected against *omnigraph's* buffered
  request body. Records that travel through an MCP session never reach omnigraph
  directly: they ride as a JSON tool parameter, and the MCP Python SDK rejects
  request bodies over 4 MiB (`DEFAULT_MAX_REQUEST_BODY_SIZE`) in ASGI middleware
  ahead of parsing, answering `413 Request body too large`. FastMCP exposes no
  way to raise it, so the client must stay under it.

  One name for two different ceilings is what hid this — a real
  `witan migrate merge` against the deployed service failed on its first call.
  2 MiB rather than 4: the packer counts JSONL framing while the wire carries a
  JSON-RPC envelope (~1.03x measured), so a budget set at the cap overflows it,
  and the cap belongs to a deployment this client cannot interrogate.

## [0.10.0] - 2026-08-07

> Also covers 0.8.0 and 0.9.0, which were published from the workspace without
> their own entries — their content is the "Changed"/"Added" items below that
> predate this release's own. Version bumps had been running ahead of this
> file; this entry closes the gap rather than reconstructing boundaries that
> were never recorded.

### Added

- **`witan_core.chunking` and `OmnigraphClient.load_batch`, moved up from
  witan-code.** Both now serve two callers: a code index and a memory merge hit
  the same buffered-body ceiling on the way to the server, so the split rule —
  and the every-node-before-any-edge ordering it has to preserve — lives in one
  place instead of being reimplemented per package. witan-code imports them
  from here now; `witan.remote.proxy` uses them to batch `migrate merge`
  against a deployment.

- **`resolve_config_path()`**, split out of `load_toml()`. Readers already
  resolved `WITAN_CONFIG` internally; now writers (`witan target add`) can
  target the very file the readers read rather than re-deriving the rule and
  drifting from it.

  An empty or whitespace-only `WITAN_CONFIG` now counts as **unset**. Taken
  literally it resolves to `Path("")` — the current directory — so a reader
  reported "failed to read config file ." and a writer would have tried to
  rewrite a directory. Nobody means that by it; it is what an unexpanded
  `WITAN_CONFIG=$SOME_UNSET_VAR` looks like.

### Fixed

- **`load_batch` wrote its temp file in the platform locale encoding.** witan
  rows are prose and routinely non-ASCII, so this could fail or corrupt on the
  way to the store. Now explicitly UTF-8.

### Changed

- **`ActorTokenResolver`'s unprovisioned-actor error no longer names a
  `witan-users` Keycloak group** — there isn't one, and the message was
  sending operators off to check a group membership that does not exist. The
  deployed pipeline provisions every enabled, non-service-account user of the
  Keycloak realm (ol-infrastructure `applications/omnigraph/token_sync.py`), so
  the message now points at the three things actually worth checking: whether
  the account is disabled, whether it is in the realm at all, and whether it is
  a service account (which the realm does contain, and which the pipeline
  deliberately skips). `witan-users` remains the
  name of the *Cedar* group in `policy/`, which is what it always was — see
  the 2026-08-05 addendum to witan ADR-0004.

### Added

- **Connect-failure retry for a restarting omnigraph-server** — a remote call
  that cannot establish a connection now backs off and retries against a
  150s wall-clock deadline, jittered, instead of failing the MCP tool call
  outright.
  Restarts of the deployed data tier are routine, not exceptional: it hashes
  its bearer-token map once at boot, so provisioning a new user bounces
  the Deployment (ol-infrastructure wires that through the Vault Secrets
  Operator's `rolloutRestartTargets`), and that Deployment is `replicas=1` +
  `strategy=Recreate` — a hard gap with no endpoint, not a rolling one.

  Only connection-*establishment* failures (`tcp connect error`, `dns error`)
  are retried. Those provably never reached the server, so re-running is as
  safe for a `mutate` as for a `query`. Mid-flight failures — connection
  reset, timeout, 5xx — are deliberately excluded: the write may already have
  committed, and silently re-applying it is worse than surfacing the error.
  The budget is independent of both `_MAX_ATTEMPTS` and `surface_conflict`;
  there is no conflict to surface when the request never left the process.

  The budget is a wall-clock deadline rather than an attempt count. It shipped
  as an attempt count first, and that framing is what made it wrong: 12
  attempts of capped backoff sums to ~42s, and its test asserted that sum was
  `>= 40` — a threshold with no provenance, which passed while being shorter
  than the thing it was supposed to outlast. Validating against the CI
  deployment measured two real restarts at **52s and 61s**, so the client was
  giving up mid-restart. The deadline is now 150s and the test asserts against
  the measured outage, driven through `_execute`, rather than against the
  schedule's own arithmetic.

  For anyone tuning this: of that ~60s, 30s is the full
  `terminationGracePeriodSeconds` — the server does not exit on `SIGTERM` and
  is `SIGKILL`ed at the deadline, every time, exactly — and the rest is the
  binary opening its S3-backed graphs (the port is still unbound ~19s in)
  before the readiness probe can pass.

## [0.7.0] - 2026-08-01

### Added

- **`schema_apply` / `schema_apply_if_changed` / `schema_stamp_path` in
  `witan_core.omnigraph`** — the mtime-stamped schema re-apply, lifted out of
  witan-code so witan-council can use the same one. Both servers face the same
  problem (an existing local store must pick up additive schema changes without
  paying a subprocess on every hot-path call) and both need the same failure
  behavior: no `check=True`, stamp only on success, so a failed apply degrades
  and is retried rather than taking down a server that runs its ensure at
  import time. `witan_code.store`'s private `_schema_apply*` helpers are gone;
  they had no callers outside that module.

- **`witan_core.identity`** — `derive_actor_id()`, the ADR-0004 Keycloak
  `sub` → `act-<id>` mapping, moved up from witan-council. witan maps the
  claim server-side off a validated JWT to route a request to the caller's
  omnigraph client; witan-code maps the same claim client-side off its cached
  OIDC token to name the code-graph branch views it owns. A view named by one
  derivation and authorized against another is a bug with no symptom until
  two users collide on it, so there is one function. witan-council's
  `witan.identity` re-exports it.
- **`witan_core.identity.ActorTokenResolver`** — the actor id → provisioned
  omnigraph bearer token half of the same mapping, moved up from
  witan-council for the same reason as the id derivation: witan-code now
  resolves it too. Under ADR-0005 path c the deployed MCP tier performs a
  remote indexer's code-graph writes as the *caller*, so both servers look up
  tokens in the same file, in the same process, when `witan serve` mounts
  witan-code's tools into witan's own server. It stays server-side either way
  — a CLI never reads the provisioned token map. `witan.identity` re-exports
  it unchanged.
- **`DeviceAuth.cached_claims()`** — the claims of the cached access token,
  offline and expiry-blind. Callers that need to *call* a deployment use
  `get_valid_token()`; a caller that only needs to know who the user is
  (witan-code, naming its branch views) must not pay a network round trip, or
  block, to learn it — and `sub` does not change when a token expires.

## [0.6.0] - 2026-07-31

### Added

- **`witan_core.remote.config`** — `RemoteConfig` plus `resolve_remote_config()`,
  the "which deployment, and how do I authenticate to it" half of ADR-0005 path
  a. It had stayed behind in witan-council even though it carried nothing
  witan-council-specific, so witan-code could not reach a deployment at all
  without copying it. Both servers now keep only their own target *selection*
  and delegate the env > target > config.toml resolution here, so the two CLIs
  read the same `WITAN_REMOTE_URL` / `WITAN_OIDC_*` keys off the same
  `[targets.<name>]` block — one deployment, one configuration.
- **`remote.oidc.cache_path()` / `device_auth()` / `DEFAULT_CACHE_PATH`** — the
  shared `~/.config/witan/tokens.json` location (and its `WITAN_TOKEN_CACHE`
  override), previously a private copy in witan-council. Entries are keyed by
  `(issuer, client_id)` and both CLIs default to the `witan-cli` client id, so
  one `witan login` authenticates both.

### Changed

- `RemoteConfig` is a frozen dataclass rather than a pydantic model. Every field
  arrives as a string from the environment or TOML, so there was nothing to
  coerce, and `witan_core.remote` keeps its dependency surface honest
  (`httpx2` + `fastmcp`, no pydantic). Construction and attribute access are
  unchanged; `.model_dump()` and friends are gone.

## [0.5.0] - 2026-07-30

### Added

- **MRTR elicitation.** `elicit.confirm` / `elicit.text` now pick their wire
  mechanism per request: multi-round-trip (MCP 2026-07-28, SEP-2322) on a
  connection whose client advertises elicitation, `ctx.elicit` on the handshake
  eras, and the caller's default when neither is possible. This fixes
  elicitation being silently dead on 2026-07-28 — that era removed the
  server→client back-channel, so `ctx.elicit` raises there and the previous
  blanket `except Exception` turned every prompt into its default. A server must
  register the new `MRTRElicitationMiddleware` for the MRTR path to work.
- **`witan_core.caching`** — the shared `ttlMs`/`cacheScope` hint a server
  declares on its list results (SEP-2549). 300s at `private` scope; see the
  module docstring for why `public` is the wrong default when a server holds
  per-actor data.
- **`RemoteMCPProxy` answers elicitation prompts.** New `_elicitation_handler()`
  hook, defaulting to `console_elicitation_handler`, which prompts on the
  terminal. Previously a prompt raised by a deployment could never be answered
  over the CLI.

### Changed

- **The proxy honors the server's `ttlMs`** for its cached tool list, instead of
  holding it for the whole process lifetime.

### Notes

- Still `fastmcp>=3.4.2,<5`. The 4.x-only features above degrade on 3.4.x rather
  than requiring it: MRTR is selected per connection, and the cache hint is
  omitted when the installed FastMCP has no `cache_ttl` argument. Requiring
  FastMCP 4 outright is deferred until 4.0 leaves pre-release, because
  `uv tool install` will not resolve a transitively-pulled pre-release.

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
