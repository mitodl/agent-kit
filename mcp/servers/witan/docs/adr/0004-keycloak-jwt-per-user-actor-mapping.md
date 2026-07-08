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

**Resolved** by that follow-up: rather than editing all 129 call sites (or
threading a `client` parameter through every handler and helper), the
module-level `client` name is now bound to a small proxy
(`_ActorScopedClient`) whose `__getattr__` calls `_resolve_client()` on every
access. `_resolve_client()` returns the single `_default_client` unchanged
when `identity_cfg.oidc_issuer` is unset (byte-identical local/stdio
behavior), and otherwise reads the validated JWT via fastmcp's
`get_access_token()`, derives the actor id from its `sub` claim, and
returns a per-actor `OmnigraphClient` built once and cached by actor id. A
request with no access token in scope (an admin/migration CLI command run
inside the deployed container, not an MCP tool call — FastMCP's own auth
already rejects unauthenticated tool requests) also falls back to
`_default_client`. See `witan/server.py` (`_resolve_client`,
`_ActorScopedClient`) and `tests/test_actor_client.py`.

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

### Addendum (2026-07-08) — the "Forces" premise above was about `toolhive_swe`'s config, not a ToolHive platform limitation

A capability audit of upstream `stacklok/toolhive` at `v0.33.0` — the exact
version already pinned as `TOOLHIVE_OPERATOR_CHART_VERSION` in
ol-infrastructure — found that ToolHive natively supports an "External OIDC
provider" auth scenario (`docs/middleware.md`) where **the client's JWT is
forwarded to the backend MCP container unmodified**, plus a pluggable
authorization framework including a Cedar-based authorizer (`docs/authz.md`)
and a real, tested OAuth 2.0 Token Exchange (RFC 8693) implementation
(`pkg/oauthproto/tokenexchange/`).

The "Forces" section above is accurate about what it checked — `toolhive_swe`
specifically uses ToolHive's *other* scenario ("Embedded auth server" →
upstream-token-swap, vMCP-scoped JWT only) — but generalizes that
configuration choice into "ToolHive's embedded broker does not propagate
end-user identity to the backend container," which overstates it: a
*different* toolhive_witan configuration could plausibly get per-user JWT
forwarding, Cedar authz, or RFC 8693 token exchange from ToolHive itself,
narrowing or removing the need for D1's own `JWTVerifier` path. This wasn't
a "future ToolHive release" scenario as line 139 speculated — the capability
was already present in the pinned version at the time this ADR was written.

Whether to actually change course (and where the authz source of truth
should live if witan's own Cedar bundle and ToolHive's authz framework would
otherwise overlap) is tracked as a separate decision, not resolved here:
`tk-revisit-adr-0004-adr-0009-per-user-identity-desi-e9005a`
(project `wp-witan-multi-user-service-deployment-dcf6ee`).

### Resolution (2026-07-10) — keep D1–D4 as designed; fix the ToolHive scenario, not the code

`tk-revisit-adr-0004-adr-0009-per-user-identity-desi-e9005a` is resolved as:
**adopt ToolHive's "External OIDC provider" scenario for `toolhive_witan`'s
auth config; do not adopt ToolHive's Cedar authorizer or RFC 8693 token
exchange; make no code change to D1–D4.**

- **Identity propagation.** The "Forces" section's mistake was inferring a
  platform limitation from `toolhive_swe`'s specific scenario
  ("Embedded auth server" → upstream-token-swap). `toolhive_witan` doesn't
  have to use that scenario. Configuring it instead with ToolHive's
  "External OIDC provider" scenario makes ToolHive forward the client's
  genuine Keycloak-issued JWT to the backend container unmodified — which is
  exactly the input D1's `JWTVerifier(jwks_uri=..., issuer=..., audience=...)`
  was already written to validate. This isn't an alternative to D1, it's the
  ToolHive-side configuration that makes D1 deliverable through ToolHive
  instead of requiring witan to somehow sit outside ToolHive's proxy path.
  `derive_actor_id` and `ActorTokenResolver` (D2/D3) are unaffected — they
  operate on the validated `sub` claim regardless of which scenario delivered
  the JWT. **No changes needed to PR #84 or PR #90.**
- **RFC 8693 Token Exchange — rejected for witan.** Token exchange re-mints a
  token signed by ToolHive's own exchange service, which would make ToolHive
  the identity boundary witan trusts instead of Keycloak directly — the
  opposite of D1's explicit choice. It's a better fit for `toolhive_swe`-style
  fan-out to third-party backends that need scope narrowing per tool, not for
  witan, which wants the original per-user identity intact.
- **ToolHive's Cedar authorizer (`cedarv1`) — not adopted.** It authorizes at
  the MCP transport layer ("can this JWT call tool X at all") with no
  knowledge of witan's domain model (repos, teams, node types). Witan's own
  Cedar bundle (ADR-0002) already does finer-grained, data-aware authorization
  and stays the single source of truth; running a second, coarser Cedar
  policy alongside it would add a policy surface to keep in sync for no
  proven benefit at v1. Revisit only if a concrete need for transport-layer
  pre-filtering (e.g. rate-limiting a tool before it reaches witan at all)
  shows up.
- **Follow-up.** The ol-infrastructure side of this decision — configuring
  `toolhive_witan`'s `MCPServer`/`VirtualMCPServer` with the "External OIDC
  provider" scenario instead of copying `toolhive_swe`'s "Embedded auth
  server" pattern — is recorded in ADR-0009's own resolution addendum and in
  `tk-ol-infrastructure-toolhive-witan-pulumi-stack-e843b3`.
