# 4. Keycloak JWT → omnigraph per-user actor/token mapping

- Status: Accepted
- Date: 2026-07-08
- Deciders: witan platform owners
- Tracking: task `tk-design-keycloak-jwt-omnigraph-per-user-actor-tok-728f0c`, project `wp-witan-multi-user-service-deployment-dcf6ee`
- Supersedes: ADR-0009 (ol-infrastructure) D3's per-team-token assumption
- Related: `docs/adr/0002-witan-cedar-authorization-bundle.md` (D1 — per-user
  `witan-users` group); ol-infrastructure
  `docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md`;
  `tk-ol-infrastructure-toolhive-witan-pulumi-stack-e843b3`

## Context

ADR-0002 D1 already decided Cedar identity is per-user (`act-<sub>`), not
per-team — Keycloak issues each authenticated human their own JWT, and
omnigraph's own `groups:` are built from individual actors. What that ADR left
open is the actual **mapping mechanism**: given an inbound request to the
deployed witan MCP service, how do we get from "a Keycloak-authenticated
human" to (a) the `act-<sub>` id Cedar rules reference and (b) the omnigraph
bearer token that makes the server resolve the request as that actor.

ADR-0009 (ol-infrastructure) assumed ToolHive's embedded OAuth broker would
be the vehicle for this and flagged direct OIDC/JWT validation against
Keycloak as a fallback if the broker "proves limiting."

### Forces

**ToolHive's embedded broker does not propagate end-user identity to the
backend container.** Confirmed by reading the `toolhive_swe` stack
(ol-infrastructure `src/ol_infrastructure/applications/toolhive_swe/`): the
`VirtualMCPServer`'s `authServerConfig` is ToolHive's *own* embedded
authorization server — it brokers login against Keycloak upstream
(`upstreamProviders[0].oidcConfig`) but then issues **its own** JWT for
`incomingAuth`, scoped to `audience: VMCP_RESOURCE_ID` (the vMCP itself, not
Keycloak). Backend `MCPServer` specs (fetch/grafana/sentry) carry only static
injected secrets/env — no per-request header, no forwarded bearer token, no
end-user claim. Today, "any Keycloak-authenticated user gets full access to
the aggregated tool set" (ADR-0009's own words) because the backend container
cannot tell users apart at all. There is no header to read here; the fallback
is the only option, not a hedge.

**omnigraph-server's bearer-token auth is static, not mintable at
request time.** Per omnigraph `docs/user/operations/server.md` § "Auth model":
tokens are SHA-256-hashed **once at server startup** from one of three
sources — AWS Secrets Manager, a JSON file/env var (`{actor_id: token}`), or
a single legacy token. There is no HTTP endpoint or CLI subcommand to register
a new (actor, token) pair at runtime; the only way to add one is to update the
token source and restart. So witan cannot mint a fresh per-user token on the
fly the way it can derive an actor id — the token for `act-<sub>` has to
already exist in whatever source omnigraph-server was booted against.

**Cedar actor identity is signed-claim-only and matched server-side**
(`docs/user/operations/policy.md` § "Actor identity"): "the server resolves
the token at the auth middleware boundary, looks up the actor it was minted
for" — client-supplied actor ids are never trusted. This is the same
boundary witan's own token resolution must respect: witan cannot tell
omnigraph-server "treat this request as actor X" via anything but the bearer
token itself.

## Decision

### D1 — witan performs its own direct OIDC/JWT validation against Keycloak

Configure witan's FastMCP server with `auth=JWTVerifier(jwks_uri=f"{issuer}/protocol/openid-connect/certs", issuer=issuer, audience=...)`
(fastmcp 3.4's built-in verifier — already a dependency, no new package).
ToolHive still hosts the container (lifecycle, networking, registry) but is
**not** the identity boundary for witan; the fallback ADR-0009 flagged is the
decision, since the broker path was confirmed closed rather than merely
suspect. This only activates for the deployed `streamable-http` transport —
local `stdio` usage (the existing single-user mode) is unaffected and
requires no Keycloak reachability.

`JWTVerifier` populates `AccessToken.claims` with the full JWT claim set,
retrievable per-request via fastmcp's `get_access_token()` dependency — this
is how a request-scoped `sub` reaches witan's tool handlers.

### D2 — actor id is a deterministic, pure function of `sub`

`act-<sanitized-sub>` — lowercase, non-`[a-z0-9-]` characters collapsed to
`-`. Keycloak's `sub` is already a UUID in practice, so this is close to
identity; sanitizing defensively costs nothing and avoids ever shelling out
an unsanitized claim into a CLI arg or file path. Implemented as
`witan.identity.derive_actor_id` — pure, no I/O, unit-testable without a
running server or Keycloak.

### D3 — per-user tokens are pre-provisioned out-of-band; witan looks up, never mints

Because omnigraph-server only reads its token source at boot, the (actor,
token) pairs for every `witan-users` member must already exist there before a
user's first request. The provisioning pipeline (ol-infrastructure,
`tk-ol-infrastructure-toolhive-witan-pulumi-stack-e843b3`) is responsible for
walking the Keycloak `witan-users` group/role membership and writing a
generated token per user into the same source
`omnigraph-server` boots from (`OMNIGRAPH_SERVER_BEARER_TOKENS_FILE`/
`_AWS_SECRET`), keyed by `act-<sub>`. This is the "per-user, not per-team"
shift called out in ADR-0002 D1 and in the pulumi-stack task: instead of one
shared credential per team, each user gets an individually-provisioned token
— but "individually-provisioned" still means *provisioned ahead of the
request*, not synthesized by witan out of thin air.

witan's role is **lookup, not mint**: `witan.identity.ActorTokenResolver`
reads the *same* JSON map (`WITAN_ACTOR_TOKENS_FILE`, matching
`OMNIGRAPH_SERVER_BEARER_TOKENS_FILE`'s shape so both processes can be
pointed at one generated file with no format translation), and resolves
`act-<sub> → token` per request. "Looked-up... at request time" (the
originating task's phrasing) describes this lookup, not dynamic minting.

The resolver reloads the file when a requested actor id is missing from its
current in-memory cache (rather than on a fixed TTL), so a newly-provisioned
user succeeds on their first request without waiting for witan's own process
to restart — as long as the provisioning pipeline has already written their
entry before that request. A `sub` with genuinely no entry (provisioning
hasn't run yet, or the user isn't in `witan-users`) fails closed with a
message naming the missing actor id, not a silent fallback to some default
identity.

### D4 — scope boundary: this ADR ships the mapping primitives, not the full per-request tool wiring

`witan/server.py` currently constructs one process-lifetime
`OmnigraphClient` at import time (`client = OmnigraphClient(cfg.graph_uri,
cfg.graph_token, ...)`) and every one of the ~30 MCP tool functions closes
over that single module-level `client`. Threading a per-request, per-actor
`OmnigraphClient` through every tool handler is a mechanical but wide-surface
refactor (129 call sites at last count) that deserves its own review and test
pass rather than riding on a design ADR. This ADR ships and unit-tests the
two pieces that are independently correct and independently useful —
`derive_actor_id` and `ActorTokenResolver` — and the `JWTVerifier` wiring
into the `FastMCP(...)` constructor (additive, gated on Keycloak config being
present, inert otherwise). Threading a per-actor client through every tool
function is tracked as an explicit follow-up
(`tk-witan-wire-per-actor-omnigraphclient-into-every--f1f787`), not silently
deferred.

## Consequences

- Closes the open question ADR-0009 left as a fallback: witan does its own
  OIDC validation; ToolHive is hosting/lifecycle only for this service. Any
  future ToolHive release that *does* propagate per-user identity to backends
  would let us drop `JWTVerifier` in favor of trusting a forwarded header, but
  nothing in this design depends on that landing.
- New non-negotiable cross-repo contract: ol-infrastructure's Keycloak→token
  provisioning pipeline and omnigraph-server's token source must be the
  *same* generated artifact witan reads — a drift between "who Keycloak says
  is in `witan-users`" and "who has a token in the file" surfaces as a hard
  lookup failure for the affected user, not a silent downgrade.
- Local single-user `stdio` mode is untouched — `JWTVerifier` is only
  constructed when Keycloak issuer/audience config is present, which it never
  is for local use.
- Does not solve self-service/on-demand provisioning for a brand-new Keycloak
  user before the sync pipeline has run — out of scope for v1, same
  limitation the per-team model had, just at finer grain.
