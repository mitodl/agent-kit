<!--
  MIRRORED FILE — DO NOT EDIT HERE.
  Edit mcp/servers/witan/docs/adr/0005-secure-cli-path-into-deployed-witan.md instead; `just docs-gen` copies it into the site.
-->

!!! info "This page lives with the code"

    The authoritative copy is
    [`mcp/servers/witan/docs/adr/0005-secure-cli-path-into-deployed-witan.md`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/docs/adr/0005-secure-cli-path-into-deployed-witan.md).

# 5. Secure CLI path into the deployed witan/omnigraph store

- Status: Accepted
- Date: 2026-07-14
- Deciders: witan platform owners
- Tracking: task `tk-design-a-secure-cli-path-into-the-deployed-witan-4ce2b2`,
  project `wp-witan-multi-user-service-deployment-dcf6ee`
- Related: `docs/adr/0004-keycloak-jwt-per-user-actor-mapping.md` (the
  server-side JWT→actor→token mapping this reuses); ol-infrastructure
  `docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md`
  (ClusterIP-only omnigraph-server, the `svc-witan-admin` sketch);
  `docs/adr/0009-stateless-mcp-protocol-era.md` (the 2026-07-28 era this path
  now runs on — read it alongside every `streamable-http` reference below,
  which describes the handshake-era shape)

## Context

Once witan is deployed as a shared multi-user service, the only
authenticated, actor-scoped way to touch the graph is an MCP client speaking
the MCP protocol over `streamable-http` — that path runs through
`_resolve_client()` in `witan/server.py`, which validates the caller's
Keycloak JWT, maps its `sub` to an `act-<id>`, and looks up that actor's
pre-provisioned omnigraph bearer token (ADR-0004). Every agent gets its own
Cedar scoping and audit trail.

The `witan` umbrella CLI's non-serve commands (`witan tasks`,
`witan memory search`, `witan projects …`) never take that path. They import
`witan.server` and call its tool functions **in-process**, so
`_resolve_client()` sees no MCP request (`get_access_token()` is `None`) and
falls back to the single static-token `_default_client` built from
`WITAN_GRAPH_TOKEN`. That is exactly the coarse-per-token shape ADR-0004 moved
the MCP path away from — no per-user identity, no per-user audit. And even
that fallback can't reach a deployed graph: omnigraph-server's Service is
deliberately ClusterIP-only (ADR-0009), never exposed outside the cluster, and
the CLI would need `WITAN_GRAPH_URI` pointed straight at it.

So operators and users have **no supported, secure way** to run ad hoc CLI
commands against the deployed store — routine queries (`witan tasks`,
`witan memory search` for a specific actor) *and* maintenance (`witan migrate
topics`, `witan apply-schema`, cross-actor debugging) are both stuck.

These are two different needs with two different identity requirements, so
they get two different answers.

## Decision

Adopt **both** paths, each scoped to the use case whose identity model it fits:

### (a) Primary: `witan` CLI gains a real remote MCP-client mode — `agent-kit`

For routine, per-user ad hoc access the CLI becomes an MCP client of the
deployed endpoint, inheriting the *exact* identity path the agent traffic
already uses:

1. **Auth — OIDC device authorization grant (RFC 8628).** `witan login`
   discovers the Keycloak realm's endpoints from
   `{WITAN_OIDC_ISSUER}/.well-known/openid-configuration`, requests a device
   code, prints the verification URL + user code, and polls the token endpoint
   until the user approves in a browser. The resulting access/refresh tokens
   are cached at `~/.config/witan/tokens.json` (mode `0600`), keyed by
   `(issuer, client_id)` so multiple deployments don't clobber each other.
   `witan whoami` decodes the cached token for display; `witan logout` clears
   it. The device-code grant is the standard flow for CLI tools — no client
   secret, no local redirect listener, works over SSH.

2. **Transport — MCP over `streamable-http`.** When `WITAN_REMOTE_URL` is set,
   `witan.cli._common._srv()` returns a `RemoteServerProxy` instead of the
   in-process `witan.server` module. The proxy mirrors the server-module tool
   interface via `__getattr__`, so **none of the ~40 existing CLI call sites
   change** — each `_fn(s.task_ready)(…)` transparently becomes an MCP
   `call_tool` against `WITAN_REMOTE_URL`, authenticated with a `BearerAuth`
   carrying the cached JWT. The deployed server's `_resolve_client()` then does
   the ADR-0004 JWT→actor→token mapping exactly as it does for agents. One
   identity model for every remote access path; one audit trail; one Cedar
   policy surface (omnigraph's own bundle, ADR-0002).

   Result-shape parity is free: FastMCP's `CallToolResult.data` already
   unwraps the `{"result": …}` output-schema envelope back to the raw
   `list`/`dict` an in-process call returns, so the CLI's existing rendering
   code is untouched.

   Repo scoping is resolved **client-side**: the deployed server has no git
   checkout, so a `repo=None` ("detect current repo") argument is rewritten to
   the *client's* detected repo before the call is sent. `repo=""` (all repos)
   is preserved.

### (b) Break-glass: in-cluster `svc-witan-admin` for maintenance — `ol-infrastructure`

Schema migration, `witan migrate topics`/`migrate storage-format`,
`merge-store`, and cross-actor debugging have **no per-user identity to
scope** — they operate on the store as a whole. Forcing them through a human's
per-user actor would be both wrong (a user actor must not have blanket
read/write over every other actor's data) and impossible (those commands are
plain in-process module functions, deliberately *not* `@mcp.tool`, so they are
unreachable over the MCP path). They keep the in-process path ADR-0004 already
documented, run **inside the cluster** where ClusterIP is reachable, and
authenticate as a narrow, separate `svc-witan-admin` principal:

- Provisioned in the `omnigraph` Pulumi stack, sole writer of the shared
  actor-token Vault source (ADR-0009 sketched it
  alongside `svc-witan-ci` but never provisioned it). Its omnigraph bearer
  token lives in the same actor-token source, and its Cedar policy grants only
  the maintenance verbs it needs — **not** blanket read/write to every actor's
  nodes.
- Invoked via a one-off Kubernetes `Job` (or `kubectl exec` into a bastion
  pod) that runs `witan apply-schema` / `witan migrate …` with
  `WITAN_GRAPH_URI` pointed at the in-cluster omnigraph-server and
  `WITAN_GRAPH_TOKEN=<svc-witan-admin token>`. This is the deliberate
  `_default_client` fallback, now with a purpose-provisioned admin credential
  instead of an accidental one.

The remote MCP proxy from (a) **refuses** these commands: they are not MCP
tools, so `RemoteServerProxy` raises a clear "run in-cluster as
`svc-witan-admin`" error rather than silently doing the wrong thing.

### (c) Writes: witan-code indexes cluster code graphs through the MCP tier — `agent-kit` (2026-08-01)

Path (a) moved witan-code's *reads* onto the deployment and left indexing
local, on the reasoning that indexing needs a git checkout. That is still
true, and it is exactly why (a) was not enough: the indexer is always a local
process, but on the cluster the graph it writes is on the ClusterIP-only
omnigraph-server. A developer's checkout could reach neither. Verified against
the live CI cluster on 2026-08-01: `service/omnigraph-server` has no
HTTPRoute, Ingress, or Gateway; only `witan.<env>.ol.mit.edu` (the MCP tier)
is externally reachable, and the data tier was verified only through
`kubectl port-forward`.

Four options were weighed; **route the writes through the witan MCP tier**
won. Exposing the raw omnigraph endpoint through APISIX was rejected as a
second, policy-unmediated boundary; in-cluster-only indexing was rejected
because it gives up the per-developer branch views ADR-0006 built; and
`port-forward` was rejected as a supported path (cluster credentials per
developer, poor ergonomics). One exposed boundary, already authenticated,
already actor-resolving.

- **Surface.** Seven machine-facing `code_store_*` tools mirroring the store
  operations the write path performs — `read`, `mutate`, `mutate_many`, `load`,
  `open` (fork a branch view), `views`, `graphs`. `mutate_many` was added after
  the rest, because a reindex emits two deletes per changed file and one call
  apiece made both the round trips and the Lance versions scale with the repo;
  it takes the same `(query, name, params)` steps `mutate` takes one at a time
  and splices them server-side, so the surface stays named queries and params.
  Deliberately *not* one bulk-ingest tool:
  a repo index is a hash read, a per-file purge, a bulk load, and then the
  same again against the bridge graph. Modelling each phase as its own tool
  would move indexing policy server-side, where it would have to stay in step
  with clients that can be a release behind. Mediated rather than arbitrary:
  `query` may only name a query file bundled with the server, and the graph is
  resolved from a repo URI against the *server's* configuration — a client
  never sends a store address.
- **Identity and authorization move server-side.** `witan_code/ingest.py`
  resolves the actor from the validated JWT per request (ADR-0004, the same
  mapping witan uses for memory), looks that actor's omnigraph bearer token up
  in the same provisioned map, and runs `check_writable` against it before any
  mutation reaches the store. The client-side guard in `indexer.index_path`
  stays as a fast-fail courtesy check; it is no longer the authority. An
  actor with no provisioned token is refused rather than served under the
  service account.
- **A consequence worth stating plainly:** a write through this boundary can
  never claim the shared default-branch view. That view's single writer is the
  CI indexer, which runs in-cluster over the direct transport (b's network
  position, not its credential). Everything through the tier is a branch view
  owned by the actor whose JWT carried it.
- **Client side.** `code_transport = "mcp"` (env `WITAN_CODE_TRANSPORT`, or
  per `[targets.<name>]`) makes the deployed endpoint the store's address:
  `StoreRef.via_mcp` resolves to a `RemoteStoreClient` that stands in for an
  `OmnigraphClient`, so `indexer`/`bridge` are unchanged. Unlike (a)'s proxy
  it holds one connection open for the process — an index is thousands of
  store calls, not one — and reconnects once on a dropped one.
- **The tools are registered only on a deployment** (`WITAN_OIDC_ISSUER` set,
  overridable with `WITAN_CODE_STORE_TOOLS`). A local stdio server writes its
  own stores directly, so serving them there would add six machine-facing
  tools — one of which runs named mutations — to every agent's tool list to
  serve a caller that cannot exist.
- **`code_server` keeps its meaning** as the in-cluster/direct transport: the
  CI indexer and maintenance jobs share the cluster network and have no reason
  to pay for an extra hop.

## Consequences

- **agent-kit (this repo):** implements (a) in full — `witan/remote/oidc.py`
  (device flow + token cache), `witan/remote/proxy.py`
  (`RemoteServerProxy`), `witan login`/`logout`/`whoami` commands,
  `RemoteConfig` (since 2026-07-31 in `witan_core.remote.config`), and the
  `_srv()` switch. No change to the existing in-process path: with
  `WITAN_REMOTE_URL` unset the CLI behaves exactly as before. witan-code's CLI
  mirrors all of this — see the 2026-07-31 amendment below.
- **ol-infrastructure (follow-up):** provision `svc-witan-admin` (token +
  Cedar policy) and the maintenance-Job/bastion pattern in the
  `witan` stack, plus register `witan-cli` as a public OIDC client
  with the device grant enabled in the `ol-platform-engineering` Keycloak
  realm. Tracked as a spun-off task.
- **ol-infrastructure (follow-up for (c)):** the witan MCP tier's Deployment
  must set `WITAN_CODE_SERVER` (and nothing else new — the store tools
  register themselves off the `WITAN_OIDC_ISSUER` the tier already has, and
  resolve tokens from the `WITAN_ACTOR_TOKENS_FILE` it already mounts). Until
  it does, the tier serves code-graph *reads* from whatever `code_dir` its
  container has and can serve no cluster writes at all. Each repo's
  `code-<repo>` graph must also be declared by the data-tier stack, as today.
- **Config surface:** the CLI's remote mode is opt-in via `WITAN_REMOTE_URL`
  (+ `WITAN_OIDC_ISSUER`, `WITAN_OIDC_CLIENT_ID`, optional
  `WITAN_OIDC_AUDIENCE`). These name the *client's* view of the deployment and
  are distinct from the server-side `WITAN_ACTOR_TOKENS_FILE` /
  `load_identity_config()` triple.
  - **Amendment (2026-07-20):** these four fields are also resolvable per
    named `[targets.<name>]` block in `config.toml` (`remote_url`/
    `oidc_issuer`/`oidc_client_id`/`oidc_audience`), matched the same way as
    the omnigraph `server`/`graph`/`token` fields — env var still wins, then
    the matched target, then a global config.toml value. This lets
    different orgs/repos/checkouts point at different deployed witan
    services, and a single target block can route both the omnigraph store
    and the deployed MCP endpoint together. See `RemoteConfig`/
    `load_remote_config()` in `witan/config.py`.
  - **Amendment (2026-07-31):** witan-code's standalone CLI now takes the same
    path. `witan serve` mounts its `code_*` tools onto this deployment with no
    prefix, so the endpoint already served them — only the client side was
    missing. `RemoteConfig` and the resolution above moved to
    `witan_core.remote.config` (both servers keep just their own target
    selection), and `witan_code/remote/proxy.py` binds the same
    `RemoteMCPProxy` with witan-code's policy. Both CLIs therefore read the
    same four keys off the same target block, and — since the token cache is
    keyed by `(issuer, client_id)` and both default to the `witan-cli` client
    id — one `witan login` authenticates both. witan-code's read commands
    (`symbols`, `deps`, `stitch`, `repos`, `branches`) move; indexing and store
    maintenance stay local, since they need a checkout and the store files that
    a deployed replica does not share. One divergence worth noting:
    witan-code's proxy deliberately does **not** resolve `repo=None`
    client-side. On witan's tools that means "detect the current repo", but on
    witan-code's bridge-wide tools it means "every indexed repo", so injecting
    a detected repo would silently narrow the result.

    **The coupling is deliberate: there is no `WITAN_CODE_REMOTE_URL`.** Both
    CLIs read the one set of keys, so configuring a deployment sends *both*
    remote — you cannot point witan-council at a deployment while keeping
    witan-code's reads local. That follows from the topology (one endpoint
    serving both tool surfaces) and keeps one precedence chain instead of two.
    A `[targets.<name>]` block still discriminates by repo or checkout path,
    just not by tool surface. Decided 2026-07-31 for the joint case, which is
    how these are deployed and run today; revisit if a real "remote memory,
    local code graph" need appears, since a per-server override would be
    purely additive and break no existing config.
- **Known v1 limitation:** `RemoteServerProxy` opens a fresh MCP connection per
  tool call, so a single CLI command that fans out to several tools pays
  several MCP handshakes. Acceptable for interactive CLI use; a persistent
  per-process session is deferred and tracked with the subprocess-overhead
  spike (`tk-spike-subprocess-per-call-overhead-for-remote-om-d6ceac`).
  - **Amendment (2026-07-30):** largely moot against a 2026-07-28 deployment.
    That era has no `initialize` handshake and no session id, so a fresh
    connection per call costs a connection, not a negotiation — see
    `docs/adr/0009-stateless-mcp-protocol-era.md`. The proxy also gained an
    elicitation handler, so a prompt the deployment raises now reaches the
    human at the terminal instead of degrading to the tool's default.
