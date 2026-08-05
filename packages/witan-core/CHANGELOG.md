# Changelog

All notable changes to `witan-core` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (pre-1.0:
a MINOR bump may include breaking changes).

## [Unreleased]

### Changed

- **`ActorTokenResolver`'s unprovisioned-actor error no longer names a
  `witan-users` Keycloak group** — there isn't one, and the message was
  sending operators off to check a group membership that does not exist. The
  deployed pipeline provisions every enabled human user of the Keycloak realm
  (ol-infrastructure `applications/omnigraph/token_sync.py`), so the message
  now points at the two things actually worth checking: whether the account is
  disabled, and whether it is in the realm at all. `witan-users` remains the
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
