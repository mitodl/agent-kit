<!--
  GENERATED FILE — DO NOT EDIT BY HAND.
  Regenerate with `just docs-gen` (or `./bin/gen_docs.py`).
  Source of truth: source scan + docs/_data/environment.toml
-->

# Environment variables

Every setting witan reads from the environment. Environment variables take
precedence over `~/.config/witan/config.toml`, which takes precedence over the
built-in default — so an env var always wins.

Most of these have a config-file equivalent and you will never set them by hand.
The ones worth knowing on day one are
[`WITAN_MEMORY_URI`](#store-and-attribution) (where the graph lives) and
[`WITAN_AUTHOR`](#store-and-attribution) (whose name is on what you write).
Everything below that is deployment, tuning, or operations.

## Store and attribution

Where the graph lives and whose name goes on the nodes you create. These are the
only settings a local, single-user install normally needs.

| Variable | Default | Description |
| --- | --- | --- |
| `WITAN_AUTHOR` | — | Attribution written to every node you create. Falls back to `git config user.name`, then `$USER`. |
| `WITAN_CONFIG` | `~/.config/witan/config.toml` | Path to the config file. Both `witan` and `witan code` read the same file. An empty or whitespace-only value counts as unset, so an unexpanded `$SOME_UNSET_VAR` does not silently redirect you to a file named `$SOME_UNSET_VAR`. |
| `WITAN_MEMORY_GRAPH` | `council` | Which graph to address on an `http(s)://` omnigraph-server — one server hosts many. Ignored for local paths and `s3://` stores, which name the graph in the URI itself. |
| `WITAN_MEMORY_TOKEN` | — | Bearer token for an `http(s)://` store. Required for a deployed server, meaningless for a local one. |
| `WITAN_MEMORY_URI` | `~/.local/share/witan/graph.omni` | Graph store location: a local path, an `s3://` URI, or the base URL of a deployed `omnigraph-server`. This is the single setting that decides whether you are running against your own laptop or a shared service. |
| `WITAN_OUTPUT_FORMAT` | `txt` | Default CLI output format: `txt`, `json`, `toml`, or `yaml`. Equivalent to passing `--output-format`. |

## Repository and target scoping

Which repo a call is about, and which store answers it. witan auto-detects the
repo from `.git/config`; these override that when detection is wrong, absent, or
too slow.

| Variable | Default | Description |
| --- | --- | --- |
| `WITAN_AGENT` | `claude` | Default coding-agent CLI for `witan run`: `claude`, `pi`, `copilot`, `opencode`, or `kilo`. |
| `WITAN_MODEL` | — | Default `--model` passed through to the agent by `witan run`. |
| `WITAN_REPO` | — | Canonical repo URI for the current call, overriding git detection. Setting it also skips git entirely, which is why hooks use it. Set it to the **empty string** to suppress repo detection and operate across all repos. |
| `WITAN_TARGET` | — | Name of the `[targets.<name>]` config block to use, overriding auto-detection by repo or checkout path. Lets one machine route work repos and personal repos at different stores. |

## Client: reaching a deployed witan

Set these to point the local CLI at a shared witan service instead of running
the graph in-process. They configure the *client's* view of a deployment — a CLI
user never sets the server-side identity variables in the next section.

`witan login` performs an OIDC device grant against the issuer and caches the
token; both `witan` and `witan code` share one cache, so you log in once.

| Variable | Default | Description |
| --- | --- | --- |
| `WITAN_MERGE_WATERMARKS` | `~/.config/witan/merge-watermarks.json` | Where `witan migrate merge` records what each pair of stores looked like when they last agreed, so the next merge can name the nodes both sides have written since. Beside the token cache, and per-machine — losing it costs one merge's divergence report. |
| `WITAN_OIDC_AUDIENCE` | — | Audience/resource to request, matching the deployment's own `WITAN_OIDC_AUDIENCE`. Sent on the device-auth and token requests so an issuer with an audience mapper stamps the right `aud` claim. |
| `WITAN_OIDC_CLIENT_ID` | — | OIDC client id presented during the device grant. |
| `WITAN_OIDC_EXPIRY_SKEW_SECONDS` | `90` | How long before nominal expiry a cached token is treated as already expired and refreshed. Sized so a refresh happens before a long write starts rather than partway through one. |
| `WITAN_OIDC_ISSUER` | — | OIDC issuer URL used for the device-authorization grant behind `witan login`. |
| `WITAN_REMOTE_CALL_BUDGET_SECONDS` | `0` | Deadline for a single remote graph call, used to decide whether to honour a server's retry hint or give up. `0` means no client-side deadline — obey the server's hints. |
| `WITAN_REMOTE_URL` | — | Base URL of the deployed witan MCP endpoint. Setting it routes CLI reads and writes through the service rather than opening a store locally. A `[targets.<name>]` block's `remote_url` overrides it. |
| `WITAN_REMOTE_WRITE_MAX_INFLIGHT` | `4` | How many remote writes may be in flight at once. The gate that keeps a burst of concurrent writes from stranding on the data tier. |
| `WITAN_REMOTE_WRITE_QUEUE_SECONDS` | `10.0` | How long a write waits for a slot at the in-flight gate before failing fast rather than queueing indefinitely. |
| `WITAN_TOKEN_CACHE` | `~/.config/witan/tokens.json` | Where both CLIs cache OIDC tokens. Shared on purpose, next to the shared config file. |

## Server: running `witan serve`

Deployment and operations config for a shared, network-facing witan. A local
stdio install needs none of it.

`WITAN_OIDC_ISSUER`, `WITAN_OIDC_AUDIENCE`, and `WITAN_ACTOR_TOKENS_FILE` must be
set **together** — witan refuses to start with a partial identity configuration
rather than serving unauthenticated.

| Variable | Default | Description |
| --- | --- | --- |
| `WITAN_ACTOR` | — | Overrides the OIDC-derived identity. Intended for service accounts and the CI indexer, which authenticate as themselves rather than as a person. |
| `WITAN_ACTOR_TOKENS_FILE` | — | Path to a mounted `{actor_id: token}` map. The server-side half of identity: it maps an authenticated caller to the actor recorded on the nodes they write. |
| `WITAN_MCP_HOST` | `127.0.0.1` | Interface to bind for HTTP transports. Use `0.0.0.0` inside a container. |
| `WITAN_MCP_PATH` | `/mcp` | URL path the MCP endpoint is served on. HTTP transports only. |
| `WITAN_MCP_PORT` | `8000` | Port to bind for HTTP transports. |
| `WITAN_MCP_SHUTDOWN_GRACE_SECONDS` | `120.0` | How long uvicorn waits for in-flight requests after `SIGTERM`. **FastMCP's own default is 2 seconds**, which silently truncates any rollout — a witan write has been measured at 27s under load, and a severed write is an indeterminate outcome the caller cannot safely retry. Set this to the deployment's termination grace period. |
| `WITAN_MCP_TRANSPORT` | `stdio` | MCP transport: `stdio` for local per-user use, or `streamable-http` (alias `http`) to bind a network listener. The legacy HTTP+SSE transport is deliberately not offered. |
| `WITAN_OMNIGRAPH_HTTP` | `1` | Use the direct HTTP transport for reads against a deployed omnigraph-server instead of shelling out to the `omnigraph` binary. Set to `0`/`false`/`no`/`off` to revert. Kept as a one-variable revert so a transport-specific production problem is an env change rather than an image rebuild — the CLI path beneath it stays fully maintained and is still the only way to reach `load`, `branch`, and `optimize`. |

## Code graph (`witan code`)

Settings for the tree-sitter code index and its cross-repo bridge. These mirror
the store settings above but address the *code* graph, which is a separate store
from the memory/task graph.

| Variable | Default | Description |
| --- | --- | --- |
| `WITAN_CODE_DIR` | — | Directory holding the per-repo code-graph stores. |
| `WITAN_CODE_GRAPH` | — | Graph id to address on `WITAN_CODE_SERVER`. |
| `WITAN_CODE_INDEX_ROLE` | — | Declares what the indexing process is entitled to write **on a shared graph**. There, only `ci` may write a repo's default (`main`) view and run the stale-file purge that goes with it, so no developer's reindex can clobber the view all readers fall back to. A local store has a single user, who is its writer — this setting does not restrict it, and a local default-branch reindex works with no role declared. |
| `WITAN_CODE_OPTIMIZE_INTERVAL` | `86400` | Minimum seconds between throttled background `optimize` runs on the code stores. `0` disables. |
| `WITAN_CODE_SERVER` | — | Base URL of an omnigraph-server hosting the code graphs, for a shared index. |
| `WITAN_CODE_STORE_TOOLS` | — | Force the low-level store tools on (`1`) or off (`0`), overriding the default. These expose raw graph reads and mutations alongside the curated `code_*` tools. |
| `WITAN_CODE_TOKEN` | — | Bearer token for `WITAN_CODE_SERVER`. |
| `WITAN_CODE_TRANSPORT` | `direct` | How the `witan code` CLI reaches the index: `direct` opens the store in-process; `mcp` proxies through a deployed witan endpoint. |
| `WITAN_CODE_VIEW_MAX_IDLE_DAYS` | `14` | Reap per-branch views idle at least this long. `0` (or negative) disables reaping entirely. |

## CI code-graph indexer

Read by `witan-ci-index`, the script that keeps each repo's shared code graph
current. It runs as a Kubernetes CronJob from the same `witan` image with a
different entrypoint. Nothing else should set these.

| Variable | Default | Description |
| --- | --- | --- |
| `WITAN_CODE_CI_ALLOW_LOCAL_STORE` | — | Set to `1` to waive the `WITAN_CODE_SERVER`/`WITAN_CODE_TOKEN` requirement and index into local stores instead. For development runs of the indexer only. |
| `WITAN_CODE_CI_ALLOW_PRIVATE_REPOS` | — | Set to `1` to let the CI indexer write a private repo into a shared code graph. Refused by default: code graphs have no per-repo read scoping, so every witan user could read it (ADR-0010). |
| `WITAN_CODE_CI_REPOS` | — | **Required.** Whitespace-separated canonical repo URIs to sweep and index. |
| `WITAN_CODE_CI_WORKDIR` | `/tmp/witan-ci-index` | Scratch directory for checkouts. Rejected unless it is an absolute path at least two components deep with no `..` or empty components — the guard that keeps a misconfigured value from pointing the cleanup at something important. |
| `WITAN_CODE_GH_TOKEN` | — | Clone credential. Normally minted per-repo from the GitHub App above rather than set by hand. |
| `WITAN_CODE_GITHUB_API_URL` | `https://api.github.com` | GitHub API base URL. Only meaningful against GitHub Enterprise. |
| `WITAN_CODE_GITHUB_APP_ID` | — | GitHub App id used to mint short-lived clone credentials. Set all three `_APP_` variables, or none. |
| `WITAN_CODE_GITHUB_APP_INSTALLATION_ID` | — | Installation id of the GitHub App, identifying which org's repos it may clone. |
| `WITAN_CODE_GITHUB_APP_KEY_FILE` | — | Path to the GitHub App's private key, used to sign the App JWT. |

## Write-path content scanning

witan scans everything written to the graph for secrets and PII. It ships
**enabled**, and fails closed: a scanner that raises blocks the write rather than
silently opening the gate.

`enabled_detectors`, `disabled_detectors`, `plugins`, `allowlist`, and
`allowlist_hashes` each accept a comma-separated string here (or a TOML list in
the config file). An empty `enabled_detectors` means every registered detector is
active; naming any detector switches to an explicit allowlist.
`disabled_detectors` always wins.

| Variable | Default | Description |
| --- | --- | --- |
| `WITAN_SCAN_ALLOWLIST` | — | Regexes whose matches are downgraded to audit-only, for false-positive suppression. Tested against each finding's own matched span with `re.fullmatch`. |
| `WITAN_SCAN_ALLOWLIST_HASHES` | — | Salted SHA-256 digests (hex) of specific approved values, downgraded to audit-only without ever putting the plaintext in config. Computed as `sha256(salt + matched_span)`. Normalized to lowercase at load, so a hand-typed uppercase digest still matches. |
| `WITAN_SCAN_ALLOWLIST_SALT` | — | Salt for `WITAN_SCAN_ALLOWLIST_HASHES`. Empty means the hash allowlist is inert — set a deployment-specific value before relying on it. |
| `WITAN_SCAN_DISABLED_DETECTORS` | — | Detectors to switch off. Always wins over `WITAN_SCAN_ENABLED_DETECTORS`. |
| `WITAN_SCAN_ENABLED` | `true` | Master switch. When false the write path is not scanned at all. |
| `WITAN_SCAN_ENABLED_DETECTORS` | — | Explicit allowlist of detectors to run. Empty means all registered detectors. |
| `WITAN_SCAN_ON_ERROR` | `block` | What to do when a scanner itself raises: `block` or `warn`. Fail-closed by default so a broken detector cannot silently open the gate. |
| `WITAN_SCAN_PII_ACTION` | `redact` | Enforcement for `pii` findings. Mask-and-proceed by default. |
| `WITAN_SCAN_PLUGINS` | — | Dotted import paths of external scanners to load, in addition to those discovered through the `witan.scanners` entry-point group. |
| `WITAN_SCAN_SECRET_ACTION` | `block` | Enforcement for `secret` findings. Fail-closed by default. |

## Recall ranking

Tuning knobs for the composite re-rank `recall` applies on top of BM25. Ranking
is always on; these change its shape, they do not switch it off. Set every `W_*`
weight to `0` to reproduce the raw BM25 order.

| Variable | Default | Description |
| --- | --- | --- |
| `WITAN_RANK_DEFAULT_CONF` | `0.6` | Confidence assumed for a memory that carries none. Must be between 0 and 1. |
| `WITAN_RANK_HALFLIFE_DAYS` | `90.0` | Half-life of the recency decay, in days. Must be greater than zero. |
| `WITAN_RANK_PEN_CONTRADICTED` | `0.25` | Score penalty for a memory another contradicts. Deliberately mild — a contradiction is surfaced for review, never hidden. |
| `WITAN_RANK_PEN_SUPERSEDED` | `1.0` | Score penalty applied to a memory something else supersedes. At the default it is effectively removed from results. |
| `WITAN_RANK_W_BM25` | `1.0` | Weight of the BM25 text-relevance term. |
| `WITAN_RANK_W_CONF` | `0.2` | Weight of the author-set confidence score. |
| `WITAN_RANK_W_CORROB` | `0.2` | Weight of corroboration — how much other memories back this one up. |
| `WITAN_RANK_W_HOP` | `0.5` | Per-hop distance penalty in graph-aware recall, so direct hits (hop 0) outrank expanded neighbours (hop ≥ 1). |
| `WITAN_RANK_W_RECENCY` | `0.3` | Weight of the recency term, decayed by `WITAN_RANK_HALFLIFE_DAYS`. |

## Maintenance and observability

| Variable | Default | Description |
| --- | --- | --- |
| `WITAN_CONTEXT_TTL` | `30.0` | How long the rendered session-context block is cached on disk, in seconds. Only the first prompt in the window pays to build it; the rest read one small file. `0` disables the cache. The content is advisory, so a few seconds of staleness is fine. |
| `WITAN_LOG_FORMAT` | — | Log rendering: `console` or `json`. Defaults to `console` when stderr is a TTY and `json` when it is not — a deployed pod gets structured logs and a developer gets colours, neither having to pass a flag. |
| `WITAN_LOG_LEVEL` | `INFO` | Log level. Takes precedence over the bare `LOG_LEVEL`, which is also honoured for deployments that set it org-wide. |
| `WITAN_OPTIMIZE_INTERVAL` | `86400` | Minimum seconds between throttled background `optimize` runs on the memory store. `0` disables. The `Stop` hook spawns a detached run at most this often, so compaction never blocks a session. |
