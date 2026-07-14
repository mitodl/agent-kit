# 5. Secure CLI path into the deployed witan/omnigraph store

- Status: Accepted
- Date: 2026-07-14
- Deciders: witan platform owners
- Tracking: task `tk-design-a-secure-cli-path-into-the-deployed-witan-4ce2b2`,
  project `wp-witan-multi-user-service-deployment-dcf6ee`
- Related: `docs/adr/0004-keycloak-jwt-per-user-actor-mapping.md` (the
  server-side JWT→actor→token mapping this reuses); ol-infrastructure
  `docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md`
  (ClusterIP-only omnigraph-server, the `svc-witan-admin` sketch)

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

- Provisioned in the `toolhive_witan` Pulumi stack (ADR-0009 sketched it
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

## Consequences

- **agent-kit (this repo):** implements (a) in full — `witan/remote/oidc.py`
  (device flow + token cache), `witan/remote/proxy.py`
  (`RemoteServerProxy`), `witan login`/`logout`/`whoami` commands,
  `RemoteConfig` in `config.py`, and the `_srv()` switch. No change to the
  existing in-process path: with `WITAN_REMOTE_URL` unset the CLI behaves
  exactly as before.
- **ol-infrastructure (follow-up):** provision `svc-witan-admin` (token +
  Cedar policy) and the maintenance-Job/bastion pattern in the
  `toolhive_witan` stack, plus register `witan-cli` as a public OIDC client
  with the device grant enabled in the `ol-platform-engineering` Keycloak
  realm. Tracked as a spun-off task.
- **Config surface:** the CLI's remote mode is opt-in via `WITAN_REMOTE_URL`
  (+ `WITAN_OIDC_ISSUER`, `WITAN_OIDC_CLIENT_ID`, optional
  `WITAN_OIDC_AUDIENCE`). These name the *client's* view of the deployment and
  are distinct from the server-side `WITAN_ACTOR_TOKENS_FILE` /
  `load_identity_config()` triple.
- **Known v1 limitation:** `RemoteServerProxy` opens a fresh MCP connection per
  tool call, so a single CLI command that fans out to several tools pays
  several MCP handshakes. Acceptable for interactive CLI use; a persistent
  per-process session is deferred and tracked with the subprocess-overhead
  spike (`tk-spike-subprocess-per-call-overhead-for-remote-om-d6ceac`).
