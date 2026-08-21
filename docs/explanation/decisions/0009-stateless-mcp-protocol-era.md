<!--
  MIRRORED FILE — DO NOT EDIT HERE.
  Edit mcp/servers/witan/docs/adr/0009-stateless-mcp-protocol-era.md instead; `just docs-gen` copies it into the site.
-->

!!! info "This page lives with the code"

    The authoritative copy is
    [`mcp/servers/witan/docs/adr/0009-stateless-mcp-protocol-era.md`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/docs/adr/0009-stateless-mcp-protocol-era.md).

# 9. Serving the stateless MCP protocol era (2026-07-28)

- Status: Accepted
- Date: 2026-07-30
- Deciders: witan platform owners
- Tracking: project
  `wp-mcp-2026-07-28-spec-adoption-across-witan-packag-723f64`
- Related: `docs/adr/0004-keycloak-jwt-per-user-actor-mapping.md` (the per-user
  actor mapping this leaves intact); `docs/adr/0005-secure-cli-path-into-deployed-witan.md`
  (the CLI-into-deployment path, whose per-connection handshake cost this
  removes); <https://blog.modelcontextprotocol.io/posts/2026-07-28/>

## Context

ADR-0004 and ADR-0005 both describe the deployment in terms of the MCP
`streamable-http` transport as it stood in 2025: a client opens a connection,
performs an `initialize`/`initialized` handshake, and is handed an
`Mcp-Session-Id` it must return on every subsequent request. Everything after
that is scoped to the session.

The 2026-07-28 revision removes that. There is no handshake and no session id:
each request carries its own protocol version and client capabilities in
`params._meta`, and a server answers it in full without reference to anything
that came before. FastMCP 4 negotiates the era per request, so both shapes are
served side by side from one process — a client that sends the 2026-07-28
envelope is answered statelessly, and one that opens with `initialize` still
gets the handshake era.

Three consequences land on this deployment specifically.

**Load balancing.** A session id is affinity: every request in a session has to
reach the replica holding it. Without one, any replica can answer any request,
so the `witan` stack can scale past a single pod behind plain round-robin with
no sticky sessions and no shared session store.

**Server-initiated requests are gone.** The back-channel that carried
`elicitation/create`, `sampling/createMessage` and `roots/list` from server to
client does not exist in a stateless request/response model. Witan uses
elicitation (confirm a claim steal, confirm a supersede, offer to index, ask for
a repo URI); sampling and roots it never used. Anything that needs input now
returns an `input_required` result and the client retries the same call with the
answer — multi round-trip requests, SEP-2322.

**Session state has to travel in the request.** Witan's own notion of a
workflow session is unrelated to the protocol's, but it was previously inferred
from the connection. A stateless replica cannot infer it, so the handle travels
as a tool argument.

### Forces

- FastMCP 4.0 is still a beta (`4.0.0b1`) at the time of writing; the `mcp` SDK
  underneath it is 2.0.0 stable. The pins were widened to
  `fastmcp>=3.4.2,<5` rather than moved to 4.x, so the packages resolve to
  3.4.5 for anyone who has not opted into prereleases, while our own lock — and
  therefore the container image — runs the beta. Requiring 4.x outright was
  tried and backed out; see the last entry under Consequences for why.
- Both witan servers are also used locally over `stdio`, where none of the
  above matters. Nothing here may make the local path worse.
- The elicitation contract established when it was added is *additive*: a
  client that cannot answer gets the caller's default and the tool proceeds as
  it did before elicitation existed. Changing wire mechanism must not change
  that.

## Decision

**D1. Serve both eras from one process; do not pin one.** FastMCP 4 negotiates
per request, and there is no flag to set — the "stateless mode" this was
originally scoped as does not exist as a switch. Verified by hand against
`witan serve --transport streamable-http`: a `tools/list` carrying
`MCP-Protocol-Version: 2026-07-28` plus the `_meta` envelope returns the full
tool list with no handshake and no `Mcp-Session-Id`, while the same request
without that header is served as handshake-era.

**D2. Elicitation picks its mechanism per request.** `witan_core.elicit` asks
over MRTR on a 2026-07-28 connection whose client advertises elicitation, over
`ctx.elicit` on the handshake eras, and not at all — returning the caller's
default — when neither is possible. The third arm is load-bearing: under MRTR an
ask a client cannot dispatch fails the *whole tool call*, so the additive
contract only holds if the capability is checked before asking.

**D3. Anything that must survive a retry travels in the request.** Two things
do. The workflow-session handle is a tool argument, supplied by whichever
process is client-side (`RemoteMCPProxy._resolve_session_slug`). Answers already
collected by a multi-round-trip ask ride in the protocol's `request_state`, and
only once there is more than one — a single-ask tool emits none, which keeps it
independent of the replica that minted it.

**D4. List results declare a cache TTL.** `tools/list` and friends carry
`ttlMs`/`cacheScope` from 2026-07-28. Both servers declare 300s at `private`
scope (`witan_core.caching`), and the CLI proxy holds its cached tool list for
exactly that long instead of for the process lifetime.

## Consequences

- **Multi-replica is unblocked, not enabled.** Nothing here scales the
  deployment on its own; it removes the protocol reason it could not. The
  remaining per-replica state is the per-actor `OmnigraphClient` cache
  (`witan/server.py`), which is keyed by JWT `sub` and rebuilt on a miss — a
  cost, not a correctness problem, when requests spread across pods.
- **`request_state` is sealed per-process by default.** The SDK seals it under
  an ephemeral key unless the server is constructed with a shared-key
  `RequestStateSecurity`. No witan tool asks twice in one call today, so none
  emits the field; the first one that does will need that key configured before
  it can be served by more than one replica.
- **Background tasks are per-process too.** `code_reindex` accepts
  task-augmented execution via the optional `witan-code[tasks]` extra, whose
  Docket backend defaults to in-process `memory://` — a task created on one
  replica cannot be polled from another. Moot while indexing needs a git
  checkout the deployment does not have; a shared `FASTMCP_DOCKET_URL` is the
  fix if that changes.
- **The deprecation offramp is 12 months.** `roots`, `sampling`, MCP `logging`
  and the legacy HTTP+SSE transport are deprecated as of 2026-07-28. Witan uses
  none of the first three. HTTP+SSE was removed from what
  `agent-config-kit` advertises; anything still speaking it has until
  2027-07-28.
- **CLI latency improves incidentally.** ADR-0005 records that
  `RemoteServerProxy` opens a fresh MCP connection per tool call, so a command
  fanning out to several tools pays several handshakes. On a 2026-07-28
  connection there is no handshake to pay for. The deferred persistent-session
  spike is correspondingly less urgent.
- **Revisit when FastMCP 4.0 goes GA.** The beta is what the lock and image
  currently resolve; CI therefore only exercises the 4.x end of the published
  pin range, and the 3.4.5 end has been verified locally only.
- **The straddle stays until GA, and the reason is distribution, not code.**
  Everything above needs FastMCP 4, so supporting 3.4.x costs real
  version-sniffing shims — `inputSchema` vs `input_schema`, `nextCursor` vs
  `next_cursor`, a conditional `mcp_types` import, a signature check before
  passing `cache_ttl` — guarding a path CI never exercises, since resolution
  only ever installs one major. Requiring `fastmcp>=4.0.0b1` was implemented and
  reverted anyway: `uv tool install` and `uvx --from` both refuse to resolve a
  pre-release pulled in transitively (fastmcp pins `fastmcp-slim` to its own
  exact version) without `--prerelease=allow`, and those are the documented
  install paths. `ol-agent-kit` is caught too without being touched, since it
  floors `witan-council`/`witan-code` open-ended so new releases are picked up
  automatically — publishing would have broken a fresh
  `uv tool install ol-agent-kit`. Nor can it be fixed from the publishing side:
  `[tool.uv] prerelease` is project-local and never travels in wheel metadata.
  `pip install` is unaffected. Tracked in
  `tk-move-the-fastmcp-floor-to-4-when-4-0-goes-ga-454f78`.
