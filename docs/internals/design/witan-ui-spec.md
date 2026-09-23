# Witan UI spec

Status: spec (proposed)
Project: `wp-cross-platform-witan-gui-11c03d`
Repos: agent-kit, ol-infrastructure
Basis: ADR 0011, `mcp/servers/witan/docs/adr/0011-witan-ui-read-transport.md`
(the read transport, decided in discovery;
[PR #359](https://github.com/mitodl/agent-kit/pull/359)).

Source anchors are against `origin/main` @ `426f4e7` unless noted. Library
anchors are against the versions `uv.lock` resolves: fastmcp 4.0.3, mcp 2.2.0,
starlette 1.6.0. ol-infrastructure anchors are against its `origin/main` @
`04167e970` (2026-09-18).

ADR 0011 settled _how_ the UI reads: it is an MCP client speaking
streamable-http to a witan server on its own origin, bound to an enumerated set
of fourteen read tools. This spec settles _what_ gets built on that: the
server changes, the frontend package, how the bundle is served and shipped, the
views and the reads behind each, and the task breakdown for implementation.

Building it out surfaced eight places where the scope as written cannot be met by
the tools as they are today (§3). Each gets a decision here rather than a
workaround in the browser.

## 1. Goal and non-goals

Done when a person can open one URL and see what is ready, what is blocked, who
is holding a stale claim, where the last two weeks went, and which memories
contradict each other; and a Claude Desktop session renders the board and the
project rollup inline (epic `tk-witan-ui-epic-shared-read-layer-web-app-gantt-an-462f7c`).

Not in this round:

- Writes. No claim, close, comment or inline edit from the UI. Doing them
  through the read layer would duplicate the tools' validation, and doing them
  through the tools widens ADR 0011's bound set to mutations. A later decision.
- Push updates. beads-ui keeps its views live by watching the SQLite file and
  pushing over a websocket. Witan has no change feed and the stateless protocol
  era (ADR 0009) has no server→client channel, so views poll (§6.1).
- A targets view. `witan target list` reads the caller's
  `~/.config/witan/config.toml` (`witan/cli/targets.py:873`) and no MCP tool
  exposes it. A page served by one witan process shows that process's graph;
  choosing a graph is choosing a URL (§5.3).
- Calendar planning. Decided on the epic 2026-09-17: the charts plot elapsed time
  and dependency depth, never estimates.

## 2. Server prerequisite: Host/Origin validation on `/mcp`

ADR 0011 made this the first code change: the local endpoint is
unauthenticated, so the moment `witan ui` serves a bundle, any page the user
visits can POST tool calls to `127.0.0.1`.

The guard already exists in fastmcp and is off. `HostOriginGuardMiddleware`
(`fastmcp/server/http.py:227-339`) returns 421 on a Host outside the allowlist
and 403 on a foreign Origin. `create_streamable_http_app` installs it only when
`host_origin_protection` is not `False` (`http.py:648-661`), which its docstring
says "defaults to False for compatibility" (`http.py:576-579`;
`fastmcp/settings.py:280`). `witan
serve` passes none of `host_origin_protection`, `allowed_hosts` or
`allowed_origins` to `mcp.run` (`witan/cli/__init__.py:210-221`), and nothing in
the repo sets `FASTMCP_HTTP_HOST_ORIGIN_PROTECTION`. The mcp SDK's own
`TransportSecuritySettings` check is explicitly disabled by fastmcp in favour of
this middleware (`http.py:675-681`), so there is no second layer to fall back
on.

Decision: `witan serve` passes `host_origin_protection="auto"` on every HTTP
transport, with no explicit allowlist.

What `"auto"` does, read from the middleware:

- Bound to loopback (`witan ui`, local `serve`): Host must be `127.0.0.1`,
  `localhost` or `::1` (`DEFAULT_HOSTS`, `http.py:38`), which closes DNS
  rebinding. A request carrying an `Origin` must be same-origin or loopback
  (`http.py:322-339`), which closes the cross-site POST. Requests with no
  `Origin` (the CLI, curl, agents) pass, as they should.
- Bound to `0.0.0.0` (the deployment): Host is not validated
  (`_should_validate_host`, `http.py:281-286`), and Origin is checked only when
  the request's Host is itself loopback (`http.py:288-298`). That is close to
  today's behaviour, and it is the right one there.

The deployment deliberately sets no allowlist. What the guard protects against
is a browser attaching an ambient credential to a request a foreign page
started. Deployed `/mcp` has no ambient credential: the token is a bearer
header the page's own code adds, which a foreign page cannot make the browser
send. And an explicit allowlist would break the page. Passing `allowed_hosts`
also switches Origin validation on (`http.py:288-298`); APISIX terminates TLS,
and uvicorn trusts forwarded headers only from `FORWARDED_ALLOW_IPS`
(`127.0.0.1,::1` by default, `uvicorn/config.py:363`), so the server computes the
request origin as `http://<host>` while the browser sends
`Origin: https://<host>`, and every POST from the page gets 403. Reproduced
against a live uvicorn bound to `0.0.0.0` in review. Anyone adding an allowlist
later owns that interaction, and must pass `None` rather than `[]` for "no
allowlist": an empty list still counts as explicit (`http.py:241`).

The loopback same-origin fallback also admits any other page served from a
loopback origin (e.g. a dev server on `localhost:3000`). Accepted: code already
running on the user's machine does not need the browser to reach the store.

Tests (`mcp/servers/witan/tests/`, through the hermetic conftest): a loopback
app rejects `Host: evil.example` with 421 and `Origin: https://evil.example`
with 403, accepts a same-origin POST, and accepts a POST with no `Origin`; an
app bound to `0.0.0.0` accepts `Host: witan.example` with
`Origin: https://witan.example`, which is the deployed page's request and the
one an allowlist would break.

## 3. Gaps between the scope and the tools

Every view reads through ADR 0011's fourteen tools. These are the places where
those tools, as they are, cannot produce what a view needs. Each fix is made in
the tool so agents and the UI keep reading the same thing.

### 3.1 `task_list` truncates unscoped reads at 50 rows

`list_all_tasks` and `list_tasks_by_status` end in `limit 50`
(`queries/read.gq:1044,1057`), and they are what `task_list` uses when no repo
and no project is given. The by-repo, by-project and by-parent queries have no
limit. So "all repos" on the board, and `witan graph --all-repos`, silently show
the 50 most recently updated tasks. No tool in the bound set has an offset or
cursor.

Decision: add `limit: int | None = None` to `task_list`. Omitted, every branch
behaves exactly as today (50 rows unscoped, uncapped when scoped by repo,
project or parent), so no existing caller changes. Given, it applies to every
branch and must be between 1 and 10,000, raising `ValueError` outside that
range. A plain `limit: int = 50` would not work: it either caps the scoped
branches that are uncapped today, or, applied only to unscoped reads, makes an
explicit scoped limit indistinguishable from the default. omnigraph 0.11 cannot
take the limit as a query parameter (`limit $limit` fails `omnigraph lint` with
"expected integer", and no `.gq` query does it), so this follows the memory
reads' pattern: an `_uncapped` variant of each of the two unscoped queries with
a literal `limit 10000`, selected whenever `limit` is given, and the result
sliced to `limit` in Python. No cursor in this round: a cursor over `updated_at desc` would skip rows
that change mid-page, and one bounded read per view is enough until a graph
outgrows it.

### 3.2 Task list rows carry no `created_at` or `closed_at`

The list projections (`read.gq:1015-1017` and siblings) return `slug, title,
repo, type, status, priority, project_slug, parent_slug, blocked_by, assignee,
external_uri, tags, updated_at, claimed_at`. Only `get_task` (`read.gq:996-1008`)
returns `created_at` and `closed_at`. The Gantt and the board's closed column
would each need one `task_get` per task.

Decision: add `created_at` and `closed_at` to every `list_tasks_*` projection.
Two scalar columns per row; the agent-facing cost is small and the rows are
already table-shaped.

### 3.3 Task detail is missing its edges

`task_get` returns the node plus `comments` (`witan/server.py:6009-6027`). The
drill-down also needs the tasks this one blocks, its children, and the
`CodeBranch`es working it. The queries exist: `blocks_to_slugs`
(`read.gq:1222`), `list_tasks_by_parent` (`read.gq:1102`) and
`task_code_branches` (`read.gq:1198`). No Python code calls the last one
today; it is declared and unused.

Decision: `task_get` adds `blocks: [slug]`, `children: [{slug, title, status}]`
and `branches: [{slug, repo, branch, status, updated_at}]`. A failure in any of
these reads propagates like any other `task_get` read failure. `_task_comments`
(`server.py:6050`) swallows only the error for a store that predates the
`TaskComment` type, and these three edges have no such history to tolerate.
`DiscoveredFrom` has no read query at all; it is left out until someone needs
it.

### 3.4 Stale claims are not visible on a task row

"Who is holding a stale claim" is in the done-criterion, and the only rule for
staleness is the server's: `status_pickable` (`readiness.py:68-90`) calls
`lease_expired(claimed_at or updated_at)` against `CLAIM_LEASE_SECONDS`
(`readiness.py:42`). The `updated_at` fallback is deliberate: a legacy or
hand-edited `in_progress` row with no `claimed_at` counts as held until its last
write is older than the lease.
A board could infer it from `task_ready` (an `in_progress` task that shows up as
Ready has an expired lease), but that misses the case that matters most: an
`in_progress` task whose lease expired while it also has an open blocker never
appears in Ready, because `is_ready` requires every blocker closed
(`readiness.py:93-106`). The UI copying the 3600-second constant is the other
option, and it drifts the day the server's changes.

Decision: every task row `task_list`, `task_ready` and `task_get` return for an
`in_progress` task carries `lease_expired: bool`, computed as
`readiness.status_pickable(row)`, which for `in_progress` is exactly the
server's lease rule including the fallback. Other statuses omit it.

### 3.5 No tool lists contradictions without a seed

`recall` reports `contradictions: [{a, b}]` only for pairs where _both_
memories are in its returned, limited set (`server.py:7669-7682`).
`memory_neighbors(slug, kinds=["contradicts"])` is per slug. The graph-wide
edge reads (`contradicts_edges_from`/`_to`, `read.gq:494-501`) feed only the
private `_edge_index`. An inbox of "which memories contradict each other" has
no read that returns it.

Decision: add a read-only tool `memory_contradictions(repo: str | None = None)
-> list[dict]`, returning one row per unordered pair of contradicting memories
with both endpoints' `slug, title, kind, repo, author, updated_at` and the
edge's `confidence, role, author, created_at`, ordered newest edge first. A
pair can be stored in both directions (the edge key allows it, and
`tests/test_edge_properties.py:149-158` links one both ways), so rows collapse
on the unordered pair the way `memory_neighbors` and `recall` already do; when
both directions exist, the newer edge's metadata wins, matching
`memory_neighbors`' newest-wins rule (`tests/test_edge_properties.py:161`). It is a fifteenth tool in ADR
0011 §3's bound set, which that section says requires amending the ADR; the
amendment rides the same PR as the tool. A new tool rather than a `recall` mode,
because `recall` is a ranked, seeded read and this is an unranked enumeration.

Both scope pairs by the same rule: a pair is in when at least one side is in
`repo` (`""` keeps every pair). `recall`'s expansion crosses repos, so without
that rule `recall(repo=X)` could flag a pair with both sides in another repo
that the inbox for `X` omits. With it, every pair `recall` reports is in
`memory_contradictions` for the same `repo`. One helper,
`_contradiction_in_scope`, applies the rule in both tools. The exception is no
repo detected and none passed: `recall`'s query seed then spans every repo, so
it reports every pair among its result, while `memory_contradictions` keeps
only pairs touching an unscoped memory. The UI always passes `repo`, so it
never hits that mode.

### 3.6 The retrospective Gantt's data does not exist

The scope plots "claimed_at/closed_at plus session spans". `claimed_at` cannot
carry that:

- `task_claim` writes `claimed_at = now` on every call, including a renewal by
  the same holder (`server.py:6638`), and the lease is 60 minutes
  (`readiness.py:42`). Any task worked for more than an hour has lost its first
  claim time.
- `task_update(status="in_progress")` overwrites it too (`server.py:6450`).
- `task_release` nulls it (`server.py:6907`).
- There is no status history: `TaskComment` holds author, body and timestamp
  (`schema.pg:309-315`), and the `Closes: WorkflowSession -> Task` edge
  (`schema.pg:294`) has a mutation (`mutations.gq:666`) that no code calls.

`closed_at` is not reliable either, as the code stands. A reopen through
`task_update` keeps the old `closed_at`, because `_update_task` merges over the
current row and only a transition _to_ `closed` touches it
(`server.py:6443-6450`). And `task_release` takes a `status` that may be
`closed` (`server.py:6858`) but writes only status, assignee and `claimed_at`
(`server.py:6907`), so it can produce a closed task with no `closed_at`.
`created_at` holds, and sessions carry `started_at`/`ended_at`
(`read.gq:747-757`).

Decision, in three parts:

1. Make `closed_at` an invariant of status before anything plots it: every
   transition to `closed` stamps it (`task_release` included), and every
   transition out of `closed` clears it. The timeline still reads `closed_at`
   only on rows whose status is `closed`, so rows written before the fix
   cannot draw a bar that ends on a reopened task's old close.
2. Add a nullable `first_claimed_at: DateTime?` to `Task`, set by `task_claim`
   and `task_update(status="in_progress")` only when it is null, and never
   cleared. It is an additive nullable property, the same kind of schema change
   as agent-kit#326, so it carries #326's ordering constraint: omnigraph's
   schema apply lands before the witan image that writes it
   (`les-a-witan-schema-image-ordering-violation-fails-cl-a16abf`). It has no
   backfill. Tasks claimed before it ships have no first-claim time, and the
   chart says so rather than guessing.
3. The Gantt draws what is true for each task: a `created_at → closed_at` span
   (lead time) as the bar, a `first_claimed_at → closed_at` segment inside it
   where that exists (work time), and project session spans as a separate lane.
   `claimed_at` is shown only as "current lease since", on open tasks.

Rejected: reconstructing claim history from omnigraph commits. witan has no
as-of read and exposes no commit history through any tool, so it would be a
second read surface of exactly the kind ADR 0011 rules out.

### 3.7 Session history is read whole

`workflow_session_list` with no `project_slug` reads `list_all_sessions`, which
has no limit and is ordered oldest first (`read.gq:776-785`). A two-week
timeline would pull every session ever recorded on each read, and the response
grows forever while almost all of it falls outside the window.

Decision: `workflow_session_list` gains `since: str | None = None`, an ISO
timestamp; when given, only sessions whose `ended_at` is null or at or after
`since` are returned. `read.gq` has no comparison filter on a `DateTime` today,
so whether omnigraph 0.11 can apply this in the query is unverified; if it
cannot, the filter runs in Python after the read. That still bounds the
response the page receives, though not the store read. The timeline also stops
polling on the 30-second interval (§6.1), since it plots elapsed time.

### 3.8 The project rollup caps and miscounts ready work

`workflow_project_status` calls `task_ready(project_slug=slug, limit=100)` and
reports `counts.ready = len(ready)` (`server.py:4405-4418`). A project with more
than 100 ready tasks gets a truncated list and a wrong count, and a widget
bound to it (§7.1) cannot call another tool to find out.

Decision: count before truncating. `counts.ready` is the exact number of ready
tasks, `ready_tasks` stays capped at 100, and the result gains
`ready_truncated: bool`. The widget and the page both show "100 of N" when it
is set.

### 3.9 `memory_list` prunes superseded rows after its 100-row cap

The capped listings (`list_memories_by_repo` and its three siblings) end in
`limit 100`, and `memory_list` dropped superseded memories from what came back.
A repo with more than 100 memories, some superseded, got a short list with no
sign that more current memories existed past the cap.

Decision: exclude superseded memories inside the query. `list_current_memories*`
add `not { $_ supersedes $m }` and are `memory_list`'s default read; the
original four stay for `include_superseded=True`. Fewer than 100 rows is then
the whole listing, and exactly 100 means there may be more, so the result shape
stays a list. `language` had the same after-the-cap bug and is not
expressible in those queries case-insensitively, so a language-filtered call
reads the unbounded listing, filters, and then slices to 100.

## 4. The read layer

This replaces the premise of `tk-build-the-witan-ui-read-layer-over-queries-read--825c85`,
which is retitled to match (§10).

### 4.1 Transport

`POST /mcp` with a JSON-RPC `tools/call`. The client code is one module,
`src/mcp.ts`, and nothing else in the frontend knows it is talking MCP.

ADR 0011 left open which browser client speaks the 2026-07-28 stateless
envelope. Checked 2026-09-18: `@modelcontextprotocol/sdk` 1.30.0, the v1 line,
does not; its release notes list no 2026-07-28 support. The v2 split packages
do: `@modelcontextprotocol/client` 2.0.0's changelog describes it as the first
release supporting the 2026-07-28 revision, its root entry is runtime-neutral
and runs in browsers, and `StreamableHTTPClientTransport` uses `fetch`.

Decision: `@modelcontextprotocol/client` 2.x with
`versionNegotiation: { pin: "2026-07-28" }`. Pinned rather than `auto`, because
`auto` probes `server/discover` first and falls back to the `initialize`
handshake on any probe failure, so a misconfiguration would quietly downgrade
the era instead of failing. That departs from ADR 0011's Consequences, which
named the handshake era as the fallback: pinning means an era mismatch is an
error the page shows, and the fallback is a different client (below), not a
different era. `@modelcontextprotocol/ext-apps` 2.x (§7) has the
v2 packages as peer dependencies, so the page and the widgets share one client
line.

The SDK is doing real work here. witan sets neither `json_response` nor
`stateless_http` (`witan/cli/__init__.py:210-221`), so a single `tools/call`
may come back as JSON or as SSE: the modern handler commits to
`text/event-stream` if the tool emits a notification or runs past 15 seconds
(`mcp/server/_streamable_http_modern.py:146`). A raw `fetch` client would own
that parsing, the `Mcp-Method`/`Mcp-Name` headers that must match the body
(`mcp/shared/inbound.py:448-470`), and the `params._meta` envelope. That
remains the fallback if the v2 client proves unusable, and `src/mcp.ts` is the
only file that would change. The page sends `clientInfo` in the envelope even
though it is optional: fastmcp's `client_supports_extension` reads
`client_params`, which the server builds only when `clientInfo` is present
(`mcp/server/connection.py:243-257`).

### 4.2 Unwrapping results

Every tool is registered through `@_tool` with no `output_schema=`
(`server.py:535-589`), so fastmcp derives the output schema from the return
annotation (`fastmcp/tools/function_parsing.py:430-500`):

- `-> dict` (`recall`, `memory_neighbors`): `structuredContent` is the dict.
- `-> list[dict]` and `-> dict | None` (the other twelve, and
  `memory_contradictions` once §3.5 adds it): the value is wrapped,
  `structuredContent = {"result": value}`, and the schema carries
  `x-fastmcp-wrap-result: true`. A `None` result also produces an empty
  `content` list, so a client that reads the text block sees nothing at all.

The read layer unwraps in one function, keyed on each tool's
`x-fastmcp-wrap-result` flag, never on a guess from its return shape. The flags
are recorded per bound tool at build time from the fixtures (§4.3), because a
widget (§7) is handed a result with no `tools/list` to consult. The page also
reads `tools/list` once per load and fails loudly if a live flag disagrees with
the recorded one. `task_get` of a missing slug is `{"result": null}` and becomes
`null`, not an error. That empty/not-found distinction is the case discovery
found the CLI getting wrong, so it gets its own tests.

### 4.3 Types

The output schemas say nothing about fields: `{"type": "object",
"additionalProperties": true}` for `recall` and `memory_neighbors`, and a
`result` wrapper around an untyped object or array for the other thirteen. There
is nothing to generate types from. Types are hand-written in `src/types.ts`, one
per tool result, and kept honest by fixtures: a Python test calls each bound
tool against a seeded hermetic store and writes its `structuredContent` to
`ui/fixtures/<tool>.json`, plus one `ui/fixtures/tools-list.json` snapshot of the
bound tools' `tools/list` entries, which is where the wrap flags (§4.2) come
from; the frontend
tests parse every fixture through the unwrapper and the types' runtime guards.
A server change that renames a field then fails the frontend suite in the same
PR. `just ui-fixtures` regenerates them, and CI fails if they are stale, the
same way `just docs-check` gates generated docs.

### 4.4 Bound tools

Exactly ADR 0011 §3's list plus `memory_contradictions` (§3.5). `src/mcp.ts`
exports one typed function per tool and no generic `call(name, args)`, so a
fifteenth read is a visible diff to that file rather than a string somewhere.

## 5. Serving and shipping the bundle

### 5.1 Frontend package

`mcp/servers/witan/ui/`, a TypeScript package built with Vite. It sits beside
the server rather than under `packages/` because it is not independently
versioned: it ships inside the witan wheel and moves with the tools it reads.

- Rendering: lit-html, as beads-ui uses, over Preact or React. Views are
  mostly tables and SVG; there is no component state worth a framework, and
  lit-html has no virtual DOM to reconcile against a vendored vis-network.
- Charts: hand-written SVG for the Gantt and the wave chart. vis-network is an
  npm dependency bundled into the graph tab, not the unpkg `<script>` that
  `witan/visualize.py:308` loads today; a CDN script would need a CSP exception
  on the deployed page and breaks offline.
- Tooling: `tsc --noEmit`, vitest, and biome, which `prek.toml` already runs for
  TypeScript (`prek.toml:138-141`). Node version pinned in `ui/.node-version`,
  `package-lock.json` committed.

### 5.2 Routes on the witan process

The bundle is served from `/ui/`, which collides with nothing witan already
serves (`/mcp`, `/health`, `/.well-known/oauth-protected-resource`):

- `GET /ui/{path:path}` via `@mcp.custom_route`, the public API `/health` already
  uses (`server.py:445`). It resolves `path` inside the bundle directory,
  refuses anything that resolves outside it, and falls back to `index.html` so
  client-side routes survive a reload. fastmcp's `http_app` takes no `routes=`
  (`fastmcp/server/mixins/transport.py:372-385`), and the alternatives are a
  private attribute (`_additional_http_routes`) or replacing `mcp.run` with our
  own uvicorn; a path-parameter custom route needs neither.
- `GET /ui/config.json`: `{"auth": null}` when `identity_cfg.oidc_issuer` is
  unset, otherwise `{"auth": {"issuer", "client_id", "audience"}}`. The SPA reads
  this first, so one bundle serves both modes with no build-time configuration.
  It is registered before the catch-all so the catch-all cannot shadow it.
  `client_id` comes from a new `WITAN_UI_OIDC_CLIENT_ID`; with OIDC on and it
  unset, `/ui/` answers 503 naming the variable rather than serving a page that
  cannot log in.
- Every `/ui/` response sets `Content-Security-Policy: default-src 'self';
  connect-src 'self' <issuer origin>; frame-ancestors 'none'` and
  `X-Content-Type-Options: nosniff`.

These routes are unauthenticated, as `/health` is: fastmcp wraps only the `/mcp`
route in `RequireAuthMiddleware` (`fastmcp/server/http.py:620-631`). That is
correct here. The bundle and `config.json` hold no graph data, and every read
still goes through `/mcp`.

The routes register only when the bundle directory exists, so a source checkout
that never ran the frontend build serves `/mcp` exactly as today.

### 5.3 `witan ui`

`witan/cli/ui.py`, registered like `graph` (`witan/cli/graph.py:10`):

- Local target (the default): runs the same server `witan serve` does, on
  `streamable-http`, bound to `127.0.0.1` on a free port (`--port` to fix one),
  with §2's guard on, and opens `http://127.0.0.1:<port>/ui/` unless
  `--no-browser`. It fails loudly if the bundle is missing, naming the build
  command, rather than starting a server with nothing to show.
- Remote target: `witan serve` refuses to re-serve a remote target over HTTP by
  design (`witan/cli/__init__.py:121-123`), and `witan ui` keeps that boundary.
  It opens `<target server>/ui/` in the browser and exits. The deployed page does
  its own login (§8).

### 5.4 Packaging

The bundle is a build output, not committed. Vite writes it to
`mcp/servers/witan/witan/ui_dist/` (gitignored), and the wheel picks it up
through hatch's `artifacts` setting, which includes gitignored files that
`packages = ["witan"]` would skip (`mcp/servers/witan/pyproject.toml:219-232`).
`artifacts` goes at the `[tool.hatch.build]` level, not only on the wheel
target: `publish-witan.yml` runs `uv build -o dist` (line 74), which builds the
wheel from the sdist, so a bundle the sdist leaves out never reaches the wheel.

- `publish-witan.yml` runs `npm ci && npm run build` in `ui/` before `uv build`,
  and asserts `ui_dist/index.html` is in the wheel, so a release can never ship
  without the UI silently.
- `docker/witan.Dockerfile` gains a node build stage whose output is copied into
  the source tree before `uv sync` (`Dockerfile:106-113`). There is no node
  stage today.
- A new `witan-ui.yml` workflow runs typecheck, lint, vitest and the build on
  PRs touching `mcp/servers/witan/ui/**`. No workflow sets up node today.

## 6. Views

### 6.1 Shell

A top bar with repo and project filters, tabs for each view, and a detail panel
that opens over any view on a task or memory slug. Each live view re-reads on
an interval (30s default) and on window focus, and shows the read time; a failed
read keeps the last good data on screen and marks it stale rather than blanking
it. The URL carries view, filters and the open slug, so a link to a stale claim
can be pasted to whoever holds it. The retrospective timeline (§6.6) is not a
live view: it re-reads on focus and on a manual refresh, not on the interval.

Repo scoping follows the tools: every call passes `repo` explicitly (`""` for
all repos), so the result never depends on the server's working directory.

### 6.2 Projects and project rollup

- List: `workflow_project_list(repo, status, phase)`.
- Rollup for one project: `workflow_project_get` (description, `blocked_by`,
  `blocks`), `workflow_project_status` (ready tasks, last session, counts),
  `task_list(project_slug=…)` for every task, and
  `workflow_session_list(project_slug=…)` for the session history with
  summaries.

### 6.3 Task detail

`task_get(slug)`, with §3.3's edges. Every field it returns is shown, including
`resolution`, `external_uri`, `symbol_refs` and the comment thread, which is
where corrections to a task's premise live. Blocker and dependent slugs link to
their own detail.

### 6.4 Board

Columns and where each comes from:

- Ready: `task_ready(repo, project_slug, limit=…)`. This is `task_ready`'s own
  rule (`readiness.is_ready` with the out-of-set blocker resolver plus
  `status_pickable`, `server.py:6961-6971`), not a re-implementation.
  `task_ready` sorts by priority and then truncates (`server.py:6972-6975`), so
  the board passes a limit at least the size of its `task_list` read; a
  truncated Ready column would push ready tasks into Blocked.
- In progress: `task_list(status="in_progress")`. Each card shows its holder
  and lease age (`now - (claimed_at ?? updated_at)`, the same lease start
  `status_pickable` uses, so the age and the stale mark cannot disagree on a
  row with no `claimed_at`), and a card whose row says
  `lease_expired` (§3.4) is marked as a stale claim, whether or not it also
  has open blockers. The UI never compares lease age to a constant of its own.
- Blocked: open and `blocked` tasks from `task_list` that are not in the Ready
  result, with each card listing its open blockers.
- Closed: `task_list(status="closed")`, newest `closed_at` first (§3.2).

`readiness.filter_ready` is not used in any form. It treats a blocker missing
from its in-memory set as closed, which is wrong for a view scoped to one
project or repo.

### 6.5 Dependency waves

Over `task_list(project_slug=…)`: the x axis is depth in the `Blocks` DAG
(a task with no open in-project blockers is wave 0), rows are tasks, arrows are
`blocked_by`. Blockers outside the project are drawn as external stubs from one
`task_get` each. A cycle is drawn and flagged rather than failing the layout.
This is presentation over one read, so it lives in the frontend.

### 6.6 Retrospective timeline (Gantt)

Per §3.6: lead-time bars, work-time segments where `first_claimed_at` exists,
current-lease marks on open tasks, and session spans in their own lane, over a
selectable window (two weeks by default). Tasks with no `first_claimed_at` are
drawn with a hatched lead-time bar and a legend line saying the first-claim time
predates tracking.

"Where the last two weeks went" is a cross-project question, so the default is
every project: sessions from `workflow_session_list(since=…)` (§3.7) across all
projects, and tasks from `task_list(repo="", limit=…)` filtered to the window
client-side, grouped by project. The project filter narrows both reads to one
project.

### 6.7 Memory and contradictions

- Search: `recall(query, repo, kind)`, which carries superseded-pruning and
  re-ranking. The flat `memory_search`/`memory_list` stay available as a
  "plain" toggle.
- Browse: `memory_list(repo, kind)`. A list of 100 is at the cap (§3.9), and
  the tab says older memories may not be shown.
- Detail: `memory_get` plus `memory_neighbors`, grouped by edge kind.
- Contradictions inbox: `memory_contradictions` (§3.5), each pair side by side.
- Topics: `topic_get` from any tag on a memory.

### 6.8 Graph tab

The `witan graph` view, ported. `witan/visualize.py`'s `build_graph` (lines
59-155) is a pure transform from `workflow_project_list` + `task_list` rows to
nodes and edges, so the tab re-implements that transform in TypeScript over the
same two reads and renders it with the bundled vis-network. The CLI keeps its
Python renderer. The duplication is a presentation transform, not a read, so it
does not fall under ADR 0011's rule against a second read surface.

## 7. MCP Apps widgets

MCP Apps is an official MCP extension (`io.modelcontextprotocol/ui`), stable as
of its 2026-01-26 revision. A tool names its view in `_meta.ui.resourceUri`; the
view is a `ui://` resource of type `text/html;profile=mcp-app`; the host renders
it in a sandboxed iframe and hands it the tool's `content` and
`structuredContent` over `postMessage` (`ui/notifications/tool-result`).
Sources: https://modelcontextprotocol.io/docs/extensions/apps and
https://modelcontextprotocol.io/seps/1865-mcp-apps-interactive-user-interfaces-for-mcp.

fastmcp 4.0.3 already implements the server half. `@mcp.tool(app=...)` takes an
`AppConfig` and writes it into `meta["ui"]` (`fastmcp/server/server.py:1823,
1920-1926`), `@mcp.resource` defaults a `ui://` URI to the MCP Apps MIME type
(`server.py:2048`), and the server always advertises the extension
(`fastmcp/server/low_level.py:565-574`). No library upgrade is needed.

### 7.1 Bound tools

Four widgets, one per tool:

- `task_ready`: the ready column, with priority and project.
- `workflow_project_status`: the project rollup (phase, ready tasks, last
  session, blockers, counts), with the exact ready count and truncation flag
  from §3.8.
- `recall`: ranked memories with contradiction pairs flagged.
- `task_list`: the task table, grouped by status.

The scope named `workflow_project_get` for the rollup. It is replaced by
`workflow_project_status`, because a widget sees only its own tool's result and
`workflow_project_get` returns the project node without its tasks or sessions,
so a widget bound to it could not render a rollup.

### 7.2 Constraints

- A widget renders the result it is handed and calls nothing back. The spec
  allows `tools/call` through the host, but many of our sessions are Claude Code,
  which renders no widgets and shows only the text result
  (https://github.com/anthropics/claude-code/issues/95149;
  `pf-mcp-apps-sep-1865-ui-resources-render-in-claude--7e9cd9`). So the text
  result stays exactly what it is today, and a test asserts each bound tool's
  `content` is byte-identical with and without its `app=` config.
- Each widget is one self-contained HTML file, built from the same Vite package
  with `vite-plugin-singlefile` (the build the ext-apps quickstart uses), one
  entry per widget, importing the page's renderers, types and unwrapper. No
  `_meta.ui.csp` is declared, so hosts apply the spec's default
  (`connect-src 'none'`), which is what a widget that fetches nothing wants.
- The postMessage handshake goes through `@modelcontextprotocol/ext-apps`'s
  `App` class rather than hand-rolled messages.
- `_tool` (`server.py:535`) registers every tool through `mcp.tool(wrapper)`
  and has no way to pass `app=`. It gains an optional `app` parameter; nothing
  else about the wrapper changes.
- Widgets and their `app=` bindings register only when the bundle is present,
  the same rule as the `/ui/` routes (§5.2), so a tool never points at a
  resource the server cannot serve.

Where they render: claude.ai and Claude Desktop through the deployed witan
connector, and Claude Desktop against a local stdio witan in
`claude_desktop_config.json`. There is an unverified user report that Desktop
drops the UI for a streamable-http server added through that config file; the
widgets task checks both local shapes before claiming either.

## 8. Deployed mount

Nothing in witan authenticates a browser today: APISIX routes `/*` to
`witan-server:8000` with `plugins=[]` (ol-infrastructure
`applications/witan/ingress.py:75-90`), and witan's auth is bearer JWT on `/mcp`
only.

Changes:

- ol-infrastructure, `substructure/keycloak/ol_platform_engineering.py`: a new
  `witan-ui` client in the `ol-platform-engineering` realm, `access_type="PUBLIC"`,
  `standard_flow_enabled=True`, `pkce_code_challenge_method="S256"`,
  `valid_redirect_uris=["https://<witan domain>/ui/callback"]`,
  `web_origins=["https://<witan domain>"]`, and an audience mapper for `witan`
  matching `witan-desktop`'s (`ol_platform_engineering.py:519-562`). The
  `web_origins` entry is what lets the browser read Keycloak's token response;
  the repo has no public S256 SPA client yet, so this is the first. A separate
  client rather than reusing `witan-desktop`, whose redirect URIs are Claude's
  and ChatGPT's, so neither can be widened without the other.
- ol-infrastructure, `applications/witan/deployment.py`: set
  `WITAN_UI_OIDC_CLIENT_ID=witan-ui`, and no Host/Origin allowlist (§2 has
  why). The witan domain is `witan.ol.mit.edu` in Production and
  `witan.<env>.ol.mit.edu` elsewhere (`applications/witan/__main__.py:272-275`).
  The APISIX route needs nothing: `/*` already reaches `/ui/`. Its comment about
  narrowing to `/mcp` later (`ingress.py:65-74`) now has to keep `/ui/` too.
- SPA: authorization code + PKCE against the issuer from `/ui/config.json`,
  through the v2 client's `authProvider` hook: an `OAuthClientProvider` that
  returns the static `witan-ui` client id, so there is no dynamic registration
  and no client metadata document. Tokens are held in memory and
  `sessionStorage` (not `localStorage`) and refreshed with the refresh token; a
  401 from `/mcp` restarts the login. Unverified: whether the SDK's flow
  completes against Keycloak 26.7.2, which does not implement RFC 8707 resource
  indicators (the reason `witan-desktop` is a static client rather than a
  client metadata document, `ol_platform_engineering.py:510-518`). The
  deployed-mount task checks this in CI first; if the SDK insists on a
  `resource` Keycloak rejects, the login moves to `oidc-client-ts` and hands the
  token to the transport, with nothing else in the page changing.

This departs from ADR 0011 §2 in one respect: the ADR expected the SPA to find
its authorization server through RFC 9728 discovery, and the page takes the
issuer and client id from `/ui/config.json` instead. Discovery would still have
to be told a client id, and the static client is the only kind Keycloak 26.7.2
supports here.

The `witan`-audience token authorizes every tool, writes included, not only the
read set the page binds. So an XSS in the page is a write exposure, not a read
one. The CSP in §5.2 (`default-src 'self'`, no inline script, no third-party
origins) is the control, and the bundle takes no dependency that injects HTML
from graph content without escaping it: memory and task text render as text.

A reader gets exactly what their agents get: per-actor scoping in
`_resolve_client()` (`server.py:210-275`) and the Cedar bundle apply unchanged,
because the request is the same bearer-authenticated `/mcp` call.

## 9. Deferred

- Tauri shell (scope item 5): stays optional and unscheduled, tracked at p3 as
  `tk-optional-tauri-desktop-shell-wrapping-the-web-ap-e5330f`. When it is
  picked up it wraps the same bundle and points it at a `witan ui` it spawns or
  at a deployed origin. The read layer has no Node or browser-only transport,
  so the shell adds no second one.
- Cross-repo dependency bridge explorer
  (`tk-cross-repo-dependency-bridge-explorer-shown-only-64ca2d`): reads through
  `code_*` tools, which are outside ADR 0011's bound set, and two of them elicit
  (ADR 0011, Consequences). Picking it up starts with amending the ADR.

## 10. Implementation tasks

Under the epic `tk-witan-ui-epic-shared-read-layer-web-app-gantt-an-462f7c`,
in dependency order. Discovery had already filed most of the views; they are
reused here, retitled where the spec changed their premise, with a comment on
each recording the change.

| # | Task | Spec | Blocked by | Priority |
|---|---|---|---|---|
| 1 | `tk-turn-on-fastmcp-s-host-origin-guard-for-witan-s--e9cb9f` | §2 | | p1 |
| 2 | `tk-close-the-read-gaps-the-witan-ui-views-need-task-e6d786` | §3.1 to 3.4, §3.7, §3.8 | | p1 |
| 3 | `tk-scaffold-the-witan-ui-frontend-package-and-its-b-0d4cbe` | §5.1, §5.4 | | p1 |
| 4 | `tk-witan-ui-serve-the-app-shell-locally-and-mount-i-f47561` | §5.2, §5.3 | 1, 3 | p1 |
| 5 | `tk-build-the-witan-ui-read-layer-over-queries-read--825c85` | §4 | 1, 3 | p1 |
| 6 | `tk-drill-down-view-target-to-project-to-task-with-a-b05e5f` (shell, projects, rollup, detail) | §6.1 to 6.3 | 2, 4, 5 | p1 |
| 7 | `tk-board-view-driven-by-task-ready-with-claim-lease-fbe5d7` | §6.4 | 6 | p1 |
| 8 | `tk-add-a-read-only-memory-contradictions-tool-and-a-44e4a1` | §3.5 | | p2 |
| 9 | `tk-memory-view-with-a-contradictions-inbox-160698` | §6.7 | 6, 8 | p2 |
| 10 | `tk-record-first-claimed-at-on-task-so-the-retrospec-da730d` | §3.6 | | p2 |
| 11 | `tk-dependency-wave-chart-critical-path-through-the--91f179` | §6.5 | 6 | p2 |
| 12 | `tk-fold-the-witan-graph-vis-network-html-in-as-a-ta-254599` | §6.8 | 6 | p3 |
| 13 | `tk-retrospective-gantt-from-claimed-at-closed-at-an-260eac` | §6.6 | 6, 10 | p2 |
| 14 | `tk-mount-the-witan-ui-on-the-deployment-witan-ui-ke-0cac57` | §8 | 4, 5 | p2 |
| 15 | `tk-mcp-apps-ui-widgets-so-claude-desktop-renders-th-d1de23` | §7 | 4, 6, 7 | p2 |

`tk-close-the-cli-json-gaps-the-gui-discovery-found--28dad2` stays where ADR
0011 put it: p2, not a UI prerequisite, blocking nothing here.
