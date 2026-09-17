# 11. Witan UI read transport: the UI is an MCP client, same-origin, in both modes

- Status: Accepted
- Date: 2026-09-17
- Deciders: witan platform owners
- Tracking: task `tk-decide-the-witan-ui-read-transport-cli-subproces-3b691b`,
  project `wp-cross-platform-witan-gui-11c03d`
- Related: `docs/adr/0004-keycloak-jwt-per-user-actor-mapping.md` (the
  JWT→actor→token mapping this reuses, and the RFC 9728 discovery surface its
  addendum added); `docs/adr/0005-secure-cli-path-into-deployed-witan.md` (why
  the CLI cannot reach a deployed graph); `docs/adr/0009-stateless-mcp-protocol-era.md`
  (no handshake, no session id — what makes a browser client cheap);
  `docs/adr/0010-private-code-graph-read-scoping.md` (the read scoping a second
  read surface would have to honour); ol-infrastructure
  `src/ol_infrastructure/applications/witan/ingress.py`

## Context

Witan's graph has no human-readable surface, and the Witan UI project sets out
to build one served two ways: `witan ui` against the local store, and mounted on
the deployment for the shared graph. Scope item 1 requires **one read layer
shared by every client**, so the transport has to work in both modes or the
layer forks on day one.

Discovery measured the CLI as that transport and found three gaps
(`pf-witan-cli-json-surface-available-to-a-gui-drill--0d161d`): `witan task` has
no `show`, so task detail is MCP-only; `witan project status` does not honour
the global `--output-format`; an empty list prints prose and exits 0. A fourth
memory (`les-parsing-witan-output-format-json-from-a-subproce-d1ddd1`) measured
the parse contract: branch on exit code, then attempt a JSON parse, then treat a
parse failure at exit 0 as the empty state, and ignore ~1.4KB of structlog noise
on stderr.

Three candidates were on the table: the CLI as a subprocess, an MCP client, or
JSON HTTP routes on the server via FastMCP `custom_route`.

### Two premises in the task description are wrong, and both change the answer

**There is no existing OIDC session to mount behind.** The project scope assumed
the deployed UI could sit behind one. It cannot, because none exists. APISIX
routes `/*` to witan's Service with `plugins=[]` and its own module docstring
says so outright: "APISIX does not participate in authentication." Witan's
`_auth` is a `RemoteAuthProvider` wrapping a `JWTVerifier` (`witan/server.py:190`),
which is bearer-token verification on the MCP protocol endpoint. There is no
cookie session anywhere in the path. So **every** candidate owes an answer for
how a browser gets a credential; that is not a cost unique to one of them.

**`--output-format json` emits a view, not a record.** This is new to this
decision and neither discovery memory records it. `render_table`
(`witan/cli/_common.py:216`) dumps the display rows the command already built,
so `witan tasks --output-format json` returns `repo` already shortened by
`_short_repo`, `blocked_by` as a comma-joined string, and every `None`
normalized to `""` because TOML has no null. `witan projects` does the same to
`repos`. A subprocess client therefore parses a presentation projection and has
to reverse it — and closing the three known gaps would not change that, because
the lossiness is in the rendering path every table command shares.

One narrower correction: `witan project status` does have a `--json` flag
(`witan/cli/projects.py:156`) that prints the raw `workflow_project_status`
payload. What it ignores is the *global* `--output-format`. The bug is that the
global flag loses to a per-command one, not that JSON is unavailable.

### Forces

- **Task detail has to work.** It is the original MVP, and `task_get` is the
  only thing that returns description, comments, tags, `symbol_refs`, and the
  parent/project links.
- **Per-actor read scoping must not be bypassed.** Deployed memory reads are
  scoped per actor by `_resolve_client()`, and ADR-0010 is actively narrowing
  code-graph reads. A read surface that authenticates differently from the tools
  is a way to lose that.
- **The composition the views need is in the tools, not the queries.**
  `task_ready` (`witan/server.py:6878`) applies `readiness.filter_ready` plus
  lease expiry; `workflow_project_status` (`4347`) does the project rollup;
  `recall` (`7482`) composes BM25, graph expansion, superseded-pruning, and
  re-ranking. `queries/read.gq` is 1,376 lines of single-hop reads underneath
  all of that.
- **Local and deployed must run the same client code**, or scope item 1 is lost.

## Decision

**The UI is an MCP client. It speaks MCP over streamable-http to a witan server
on its own origin, in both local and deployed mode.** The read layer sits on the
tools, not on `queries/read.gq`.

### 1. Topology

One process serves both the app bundle and `/mcp`, so the UI never makes a
cross-origin call:

- **Local.** `witan ui` runs witan with `--transport streamable-http` bound to
  `127.0.0.1` and serves the static bundle from the same process. The SPA points
  at that origin's `/mcp`.
- **Deployed.** The bundle is served by the witan tier the agents already call,
  and the SPA points at the same origin's `/mcp` — the URL clients use today
  (`https://witan.<env>.ol.mit.edu/mcp`).

★ SAME-ORIGIN IS LOAD-BEARING, NOT INCIDENTAL. Nothing in witan configures CORS
(grepped `server.py` and `cli/__init__.py`: no `allow_origin`, no CORS
middleware), and the MCP spec asks servers to validate `Origin` against DNS
rebinding. Serving the bundle from the process that serves `/mcp` means the UI
needs neither, in either mode. Moving the bundle to a separate origin later
turns both into prerequisites.

### 2. Auth story

- **Local:** `identity_cfg.oidc_issuer` is unset, so `_resolve_client()` returns
  the single `_default_client` (`witan/server.py:246`) — byte-identical to what
  `witan tasks` does today. No credential, one user, `127.0.0.1` only. The trust
  model is the existing CLI's.
- **Deployed:** the SPA runs browser OIDC (authorization code + PKCE) against
  Keycloak for the `witan` audience and sends `Authorization: Bearer` on every
  `/mcp` request. `RemoteAuthProvider` already serves RFC 9728
  protected-resource metadata and a `WWW-Authenticate` 401 pointing at it
  (`witan/server.py:182`) — the discovery surface ADR-0004's addendum added for
  MCP clients that guess `/authorize`. A discovery-capable browser MCP client
  consumes it unchanged.

Per-actor scoping and the Cedar bundle then apply to a UI reader exactly as to
an agent, through the code path that already enforces them. No new
authorization plane.

The one new piece of configuration is a **Keycloak public client for the UI**
(PKCE, no secret, `witan` audience, redirect URI at the deployed host). That
lives in Keycloak/ol-infrastructure config, not in witan.

### 3. Read layer shape

The read layer is a thin typed client over the existing tools —
`task_ready`, `task_get`, `task_list`, `workflow_project_status`,
`workflow_project_get`, `recall`, `memory_*` — unwrapping MCP content blocks
(`structuredContent`, or the JSON text block) in exactly one place. Every view
in scope item 3 composes from those results.

It does **not** read `queries/read.gq`. The sibling task
`tk-build-the-witan-ui-read-layer-over-queries-read--825c85` is named for that
altitude and its premise is superseded by this ADR: a layer over the raw
queries would re-implement lease expiry, the project rollup, and
superseded-pruning, which are precisely the parts the board and memory views
depend on and the parts most likely to drift from the tools.

### 4. What happens to the CLI gaps

They are real bugs regardless of this decision, and they stay with the sibling
task `tk-close-the-cli-json-gaps-the-gui-discovery-found--28dad2` at p2. What
changes is their framing: that task is **not** a UI prerequisite and should not
be scoped as one. Because `--output-format json` emits a display projection,
closing the three gaps still would not make the CLI a viable GUI transport. The
work is worth doing for humans and shell scripts:

- `witan task show <slug>` — `_task_show` already exists as a private helper
  (`witan/cli/tasks.py:136`) and is not exposed as a subcommand.
- Make the global `--output-format` win for `project status` rather than
  requiring its own `--json`.
- Emit an empty structured result (`{"title": …, "rows": []}`) instead of prose
  when `--output-format` is not `txt`. The early return on an empty row set
  (`witan/cli/tasks.py:86`) fires before `render_table` is ever reached, which
  is why the format flag has no say today.

A fourth, larger item falls out of the display-projection finding and is worth
recording even though nothing in this project blocks on it: structured output
should serialize the tool records, not the table cells. That is a breaking
change to every table command's JSON shape, so it needs its own decision.

## Alternatives considered

**CLI subprocess.** Rejected: it cannot be the shared-graph transport at all,
so choosing it forks the read layer. ADR-0005 establishes the mechanism — the
CLI's non-serve commands call the tool functions in-process, `get_access_token()`
is `None`, and they fall back to the static-token `_default_client`; and
omnigraph-server's Service is deliberately ClusterIP-only, never exposed outside
the cluster. So a subprocess client reaches the local store and nothing else.
On top of that it inherits the parse contract (exit code, then parse, then
parse-failure-as-empty), the display projection above, and a missing task
detail command.

**JSON HTTP routes via `custom_route`.** Rejected, and the reason is
authentication rather than the duplication the task description anticipated.
FastMCP applies `auth=` to the protocol endpoint only — verified against fastmcp
4.0.0b2 in the `/health` docstring, where a JWT-guarded server answers
`GET /health` 200 and `POST /mcp` 401 — so a `custom_route` is reachable with no
bearer token. `_resolve_client()` then refuses it outright: a request with no
access token raises, deliberately, rather than borrowing `_default_client`
(`witan/server.py:249`). A JSON read route would therefore have to re-implement
JWT verification, `derive_actor_id`, and `actor_token_resolver.resolve` in a
second place. Getting that wrong yields an unauthenticated read surface over
per-actor-scoped memory and over the code graphs ADR-0010 is narrowing. Keeping
a second read surface in step with the tools is the smaller of the two costs,
and it was the only one the task weighed.

**In-process tool calls behind a local HTTP API.** This is the shape `witan ui`
would take most naturally, and it is the previous option's problem wearing the
CLI's clothes: it works locally, has no deployed story, and so forks the read
layer.

## Consequences

- **MCP framing for plain GETs.** Every read is a JSON-RPC POST carrying
  `params._meta`. Accepted: it buys authentication, per-actor scoping, and the
  composed reads, none of which we then own twice.
- **A browser MCP client dependency.** Unverified which client version speaks
  the 2026-07-28 stateless envelope; FastMCP 4 negotiates the era per request
  (ADR-0009), so the handshake era is a working fallback if it does not. To be
  confirmed when the read layer is built.
- **The stateless era makes this cheap.** No handshake and no `Mcp-Session-Id`
  (ADR-0009), so a read is one POST and any replica can answer it. The lost
  server→client back-channel costs the UI nothing: elicitation only fires on
  writes, and the UI is read-only.
- **No second read surface.** The tools stay the only way into the graph, so
  read scoping has one implementation and the views cannot silently diverge from
  what an agent sees.
- **The MCP Apps widgets (scope item 4) are unaffected.** A `ui://` widget is
  handed its tool's result by the host and needs no client of its own, so it
  reuses the read layer's types and renderers without its own transport.
- **`/*` on the APISIX route now matters.** `ingress.py` already flags that the
  wildcard makes `/health` publicly reachable and suggests narrowing to `/mcp`
  later. Serving the UI bundle from this origin adds paths that must stay
  reachable, so that follow-up has to account for them rather than assume
  `/mcp` is the whole surface.

## Rollout

1. This ADR, recorded as a decision memory linked to the project.
2. Re-scope `tk-build-the-witan-ui-read-layer-over-queries-read--825c85` to sit
   on the tools (a comment records the premise change; the title is stale).
3. Build the read layer against a local `witan ui` first, where the auth story
   is "none" — the deployed Keycloak client is not on the critical path for any
   of the views.
4. Add the Keycloak public client before the deployment mount, not before the
   local one.
