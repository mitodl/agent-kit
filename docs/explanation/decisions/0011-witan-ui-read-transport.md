<!--
  MIRRORED FILE — DO NOT EDIT HERE.
  Edit mcp/servers/witan/docs/adr/0011-witan-ui-read-transport.md instead; `just docs-gen` copies it into the site.
-->

!!! info "This page lives with the code"

    The authoritative copy is
    [`mcp/servers/witan/docs/adr/0011-witan-ui-read-transport.md`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/docs/adr/0011-witan-ui-read-transport.md).

# 11. Witan UI read transport: the UI is an MCP client, same-origin, in both modes

- Status: Accepted, amended 2026-09-22 (`memory_contradictions` joins the
  bound set)
- Date: 2026-09-17
- Deciders: witan platform owners
- Tracking: task `tk-decide-the-witan-ui-read-transport-cli-subproces-3b691b`,
  project `wp-cross-platform-witan-gui-11c03d`
- Related: `docs/adr/0004-keycloak-jwt-per-user-actor-mapping.md` (the
  JWT→actor→token mapping this reuses, and the RFC 9728 discovery surface its
  addendum added); `docs/adr/0005-secure-cli-path-into-deployed-witan.md` (the
  CLI's own remote MCP-client mode, whose identity path this reuses);
  `docs/adr/0009-stateless-mcp-protocol-era.md`
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
(`pf-witan-cli-json-surface-available-to-a-gui-drill--0d161d`): no way to get
task detail; `witan project status` does not honour the global
`--output-format`; an empty list prints prose and exits 0. A fourth
memory (`les-parsing-witan-output-format-json-from-a-subproce-d1ddd1`) measured
the parse contract: branch on exit code, then attempt a JSON parse, then treat a
parse failure at exit 0 as the empty state, and ignore ~1.4KB of structlog noise
on stderr.

Three candidates were on the table: the CLI as a subprocess, an MCP client, or
JSON HTTP routes on the server via FastMCP `custom_route`.

### Three premises this decision inherited are wrong

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

**Task detail is not missing from the CLI.** Discovery recorded that no CLI
command shows a task, and this ADR repeated it. Wrong: `task_app` is built with
`default_command=_task_show` (`witan/cli/tasks.py:196`), so `witan task <slug>`
already prints description, comments, tags, `symbol_refs` and the parent/project
links. Only the named `show` alias is absent.

What is actually missing is a *machine-readable* detail view. `_task_show` is
`console.print` end to end, with no `render_table` call and no structured branch
at all, so it has no `--output-format` mode to honour — unlike the table
commands, which at least emit something. So the gap is narrower than recorded
and worse in kind: the detail exists, as human text only.

One narrower correction in the same vein: `witan project status` does have a
`--json` flag (`witan/cli/projects.py:156`) that prints the raw
`workflow_project_status` payload. What it ignores is the *global*
`--output-format`. The bug is that the global flag loses to a per-command one,
not that JSON is unavailable.

### Forces

- **Task detail has to arrive as data.** It is the original MVP. The CLI can
  already display it, but only as rendered text, so `task_get` is the only
  source that returns it in a shape a view can bind to.
- **Per-actor read scoping must not be bypassed.** Deployed memory reads are
  scoped per actor by `_resolve_client()`, and ADR-0010 is actively narrowing
  code-graph reads. A read surface that authenticates differently from the tools
  is a way to lose that.
- **The composition the views need is in the tools, not the queries.**
  `task_ready` (`witan/server.py:6878`) calls `readiness.is_ready` with a
  resolver that fetches blocker status from outside the candidate set, and
  `readiness.status_pickable` decides lease expiry; `workflow_project_status`
  (`4347`) does the project rollup;
  `recall` (`7482`) composes BM25, graph expansion, superseded-pruning, and
  re-ranking. `queries/read.gq` is 1,376 lines of single-hop reads underneath
  all of that. Note that `readiness.filter_ready` is a *different* rule, not the
  one the board should reproduce: it works over an in-memory set and treats a
  blocker missing from that set as closed, which is right for the context hook
  and wrong for a view that pages.
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

Same-origin serving is what keeps CORS out of the picture: nothing in witan
configures it (no `allow_origin`, no CORS middleware), and a same-origin SPA
never needs it. Moving the bundle to a separate origin later makes CORS a
prerequisite.

★ SAME-ORIGIN IS NOT ORIGIN VALIDATION, AND THE LOCAL MODE IS WHERE THAT BITES.
An earlier draft of this ADR said same-origin serving meant the UI needed no
origin protection at all. That is wrong, and it is wrong in the direction that
matters. CORS governs what a *browser* will let a page read back; it does not
stop the request being sent, and it is not a server-side control. Any page the
user visits can POST to `http://127.0.0.1:<port>/mcp`, and in local mode that
endpoint has no credential in front of it at all — `identity_cfg.oidc_issuer`
is unset, so every tool call is served as the default actor. DNS rebinding is
the same attack with the origin check defeated.

So **server-side `Origin`/`Host` validation on `/mcp` is a prerequisite of this
decision, in both modes**, not a consequence of the topology. It is not
implemented today. Binding to `127.0.0.1` narrows the network path but does not
close this, because the attacker's page runs inside the browser on that host.
The read layer work does not start until `witan ui` serves a `/mcp` that
rejects unexpected origins.

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
(PKCE, no secret, `witan` audience). It needs both a redirect URI *and* the
deployed UI origin in the client's allowed web origins: the SPA exchanges its
authorization code at Keycloak's token endpoint, which is a different origin
from the UI, so without that entry the browser blocks the exchange and the
redirect alone buys nothing. That lives in Keycloak/ol-infrastructure config,
not in witan.

### 3. Read layer shape

The read layer is a thin typed client over the existing tools, unwrapping MCP
content blocks (`structuredContent`, or the JSON text block) in exactly one
place. Every view in scope item 3 composes from those results.

The tool set is enumerated rather than globbed, because the UI is read-only and
several families mix reads with mutations — `memory_*` alone contains
`memory_store`, `memory_update`, `memory_delete` and `memory_link`. The read
layer binds exactly these and nothing else:

- tasks: `task_ready`, `task_get`, `task_list`, `task_search`
- projects and sessions: `workflow_project_get`, `workflow_project_status`,
  `workflow_project_list`, `workflow_session_list`
- memory: `recall`, `memory_get`, `memory_list`, `memory_search`,
  `memory_neighbors`, `topic_get`, and `memory_contradictions` (added by the
  2026-09-22 amendment below)

An implementation that needs a tool outside this list is making a scope change,
not filling in a gap, and amends this ADR.

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

- Give task detail a structured mode. `witan task <slug>` already renders it
  (`task_app` sets `default_command=_task_show`), so the missing piece is not
  the view but a machine-readable one: `_task_show` is `console.print`
  throughout and has no `--output-format` branch. A `show` alias is cosmetic
  next to that.
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

**CLI subprocess.** Rejected, but not for the reason an earlier draft gave. That
draft said the CLI cannot reach a deployed graph, citing ADR-0005. ADR-0005 says
the opposite: it is the ADR that *fixed* this, and `_srv()`
(`witan/cli/_common.py:46`) returns a `RemoteServerProxy` whenever a remote
target resolves, so `witan tasks` against the deployment works today over the
same `/mcp` endpoint with the same per-actor mapping. The draft described
ADR-0005's Context and mistook it for its Decision.

Corrected, the rejection is stronger rather than weaker. The CLI *is* an MCP
client, so routing the UI through it means spawning a subprocess that speaks the
protocol this decision picks anyway, and then recovering data from its rendered
output. Three costs, none of which the MCP path pays:

- **A browser cannot spawn a subprocess at all.** The web app in scope item 2 is
  the primary surface, so this option only ever serves the optional Tauri shell,
  which would then need its own transport. That forks the read layer by itself.
- **The output is a projection, not a record** — the display rows above for the
  table commands, and pure `console.print` for task detail.
- **The parse contract** (exit code, then parse, then parse-failure-as-empty),
  re-implemented in every client.

**JSON HTTP routes via `custom_route`.** Rejected, and the reason is
authentication rather than the duplication the task description anticipated.
FastMCP applies `auth=` to the protocol endpoint only, so a `custom_route` is
reachable with no bearer token. That invariant is pinned by a test rather than
an anecdote: `tests/test_health.py:38-73` mounts witan's own handler on a
FastMCP carrying a verifier that can never succeed, and asserts `GET /health`
200 alongside `POST /mcp` 401. It runs against the fastmcp the repo actually
pins, so a version bump that reversed this would fail the suite. `_resolve_client()` then refuses it outright: a request with no
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

- **MCP framing for plain GETs.** Every read is a JSON-RPC POST. On the
  2026-07-28 era it carries its protocol version and capabilities in
  `params._meta`; on the handshake era fallback below it is an `initialize`
  exchange and a session id instead. Accepted either way: the framing buys
  authentication, per-actor scoping, and the composed reads, none of which we
  then own twice.
- **A browser MCP client dependency.** Unverified which client version speaks
  the 2026-07-28 stateless envelope; FastMCP 4 negotiates the era per request
  (ADR-0009), so the handshake era is a working fallback if it does not. To be
  confirmed when the read layer is built.
- **The stateless era makes this cheap.** No handshake and no `Mcp-Session-Id`
  (ADR-0009), so a read is one POST and any replica can answer it. The lost
  server→client back-channel costs the UI nothing *for the tools enumerated in
  §3*, none of which elicit. That is a property of the selected set, not of
  reads in general: `code_find_definition` elicits on an ambiguous repo and
  `code_symbols_in_file` elicits for a repo URI
  (`witan_code/server.py:448-459,602-612`), both read tools. Adding a view over
  the code graph means either handling `input_required` in the browser client or
  passing `repo` explicitly so the elicitation never fires.
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
3. Add `Origin`/`Host` validation to `/mcp` before `witan ui` serves anything.
   This is first among the code changes, not a hardening pass afterwards: the
   local endpoint is unauthenticated, so the browser-reachable window opens the
   moment the bundle does.
4. Build the read layer against a local `witan ui`, where the auth story is
   "none" — the deployed Keycloak client is not on the critical path for any of
   the views.
5. Add the Keycloak public client, with both the redirect URI and the UI origin
   in its allowed web origins, before the deployment mount rather than before
   the local one.
6. Correct the discovery memories this ADR contradicts, so the next reader does
   not re-derive the CLI premises from them.

## Amendment (2026-09-22): `memory_contradictions` joins the bound set

The memory view's contradictions inbox had no read to sit on. `recall` reports
a pair only when both memories land in its ranked, limited result;
`memory_neighbors` needs a slug to start from; and the graph-wide
`contradicts_edges_from`/`_to` queries feed only the private `_edge_index`, not
any tool. The spec (`docs/internals/design/witan-ui-spec.md` §3.5) settled this
by adding a tool rather than a `recall` mode, because `recall` is a ranked,
seeded read and an inbox is an unranked enumeration.

`memory_contradictions(repo)` is that tool, and §3's list above now names it.
It is read-only, returns one row per unordered pair (newest link wins when a
pair is stored both ways, matching `memory_neighbors`), and scopes a pair in
when either memory is in the requested repo. `recall` applies the same repo
rule to the pairs it reports, so a pair `recall` flags is always in the inbox
for the same `repo`. It sits in the same read allowlists as the other memory
reads (the CLI's local dispatch and the remote proxy), so nothing about the
UI's position as an ordinary MCP client changes.
Tracked as task `tk-add-a-read-only-memory-contradictions-tool-and-a-44e4a1`.
