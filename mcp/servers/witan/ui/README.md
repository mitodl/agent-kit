# witan UI

The web UI for the witan graph: tasks, projects, sessions, memory and
contradictions, rendered for a person rather than for an agent.

Design: `docs/internals/design/witan-ui-spec.md`. Transport: ADR 0011,
`mcp/servers/witan/docs/adr/0011-witan-ui-read-transport.md` — the page is an
MCP client speaking streamable-http to a witan server on its own origin, bound
to an enumerated set of read tools. (Both are cited by path rather than linked:
they land in their own changes, and a relative link would be dead here until
those merge.)

Every tab is built. Projects has the project list, one project's rollup and
the task detail panel (spec §6.2, §6.3). Board has Ready, In progress, Blocked
and Closed columns (spec §6.4). Waves lays one project's open tasks out by
depth in the `Blocks` graph, with the longest chain and any cycle marked (spec
§6.5). Timeline draws each task's lead time and work time, and the sessions,
over a 7 to 90 day window (spec §6.6). Memory has the contradictions inbox,
browse and search, topics, and the memory panel (spec §6.7). The panel opens
whichever the slug is, on its prefix, so a memory linked from a task opens as
a memory. Graph is `witan graph`'s project and task graph, drawn with
vis-network, which is bundled and loaded only when the tab is opened (spec
§6.8). Its transform is a port of `witan/visualize.py`, held to the Python by
`fixtures/graph.json`: the generator runs the real transform over the recorded
list results and `views/graph.test.ts` compares. Above 400 nodes each project,
and each repo's projectless tasks, is drawn as one node that opens on click,
because a graph-wide scope (about 1,150 live tasks) never finished laying out
drawn whole. A List view under the canvas has every node as a link, since the
canvas itself is pointer-only.

## Layout

It sits here rather than under `packages/` because it is not independently
versioned. It ships inside the witan wheel and moves with the tools it reads,
so its version is witan's.

```
ui/
  src/
    mcp.ts        The read layer. The ONLY file that knows it speaks MCP.
    unwrap.ts     Takes a result out of its envelope, keyed on the wrap flag.
    types.ts      Hand-written result types, kept honest by the fixtures.
    app.ts        The wiring: route in, reads out, one render.
    shell.ts      The app frame: filters, tabs, the detail panel.
    route.ts      The URL fragment IS the UI state. Parse and format it.
    live.ts       Polling with last-good retention and staleness.
    chrome.ts     The loading, empty, error and stale states.
    format.ts     Timestamps, repo labels, linkability.
    views/        One module per tab.
    widgets/      One MCP Apps widget per bound tool, plus the shared host.
  widgets/        The widgets' HTML entries, one per tool.
  fixtures/       GENERATED (`just ui-fixtures`). Real tool results.
  vite.config.ts  The build. Writes ../witan/ui_dist, the wheel picks it up.
  build-widgets.js  Builds each widget into ui_dist/widgets/<tool>.html.
  vitest.config.ts
  .node-version   The exact node pin, read by both GitHub workflows.
```

## State, polling and staleness

Every filter, the active tab and the open slug live in the URL fragment
(`route.ts`), so a link to a stale claim can be pasted to whoever holds it,
and the back button and the panel's close link cannot disagree about what is
open. The fragment rather than the path: it never reaches the server, so no
route here can 404 against a deployment whose catch-all is configured
differently from `witan ui`.

Witan has no change feed and the stateless protocol era (ADR 0009) has no
server→client channel, so views poll: every 30 seconds, and on window focus.
`live.ts` owns that, and owns the rule that makes it safe — **a failed refresh
keeps the last good data on screen and marks it stale rather than blanking
it**. A board that empties itself because one poll hit a restarting server
reads as "nothing is ready", which is a lie a person acts on.

Two distinctions in there are load-bearing and easy to collapse by accident:

- **"No result yet" is not "the result was null".** `task_get` of a missing
  slug returns `null` as its value, so a panel keyed on the data rather than on
  `hasResult` says "Reading…" forever for every stale link. This is the same
  empty/not-found confusion discovery found in the CLI.
- **A zone-less timestamp is UTC.** The graph stores most of them without an
  offset, and `new Date()` reads that form as local time, so `format.ts` parses
  them explicitly. Left to the browser, every stored instant renders shifted
  and a claim lease age with it.

## The read layer

Every view calls a named function in `src/mcp.ts`; none of them sees a
transport, an envelope or a tool name. That is what lets the MCP Apps widgets
and a later Tauri shell be presentations of one read layer rather than three
clients that drift.

There is no generic `call(name, args)`, deliberately. ADR 0011 binds the UI to
an enumerated set of read tools, and a generic entry point would make a
fifteenth read a string somewhere instead of a visible diff to that file.
Writes are not here at all.

`unwrap.ts` keys on each tool's `x-fastmcp-wrap-result` flag rather than
guessing from the shape of a value. fastmcp wraps a `list` or `dict | None`
return as `{"result": ...}` and leaves a bare `dict` alone, so a wrapped list
and an unwrapped dict with a `result` key are indistinguishable at runtime,
and `{"result": null}` looks like an absent value rather than a present one.
`task_get` of a slug that does not exist **is** `null`, and a page that treats
that as an error shows a crash for a stale link.

The flags are recorded at build time because an MCP Apps widget is handed a
result with no `tools/list` to consult. `assertFlagsMatchServer` checks that
recording against the live server once per page load, so a server whose return
annotations have moved is a loud failure rather than a view that renders
nothing.

## Fixtures

`fixtures/` holds real results from real tool calls against a seeded store,
recorded by `just ui-fixtures`. They exist because the result types are
hand-written and have to be: every bound tool is registered without an
`output_schema`, so fastmcp derives `{"type": "object", "additionalProperties":
true}` from the return annotation and there is nothing to generate from.

`all-fixtures.test.ts` walks every fixture in the directory through the
unwrapper, so one added by `just ui-fixtures` is covered the moment it lands;
`fixtures.test.ts` asserts the specific shapes the views lean on. A server
change that renames a field fails the frontend suite in the same PR that made
it. `just ui-fixtures-check` is the CI gate, and it runs on server changes as
well as frontend ones, since a server change is what makes a fixture stale.

Note that the tools do **not** all return the same projection. `task_search`
is narrower than `task_list` (no timestamps, plus `description`), and
`workflow_project_list` is narrower than `workflow_project_get`
(`github_issue`, no `github_pr`). `types.ts` splits those rather than
declaring fields half the results do not carry.

Slugs and timestamps are normalized to stable stand-ins when recorded.
Without that the files would differ on every run and the gate would fail
against files nobody touched. biome does not format them (`prek.toml` and the
`lint` script both exclude the directory) for the same reason.

## Commands

```bash
npm ci            # install; `npm install` if you are changing dependencies
npm run dev       # vite dev server
npm run build     # -> ../witan/ui_dist
npm run typecheck # tsc --noEmit
npm run lint      # biome check   (`npm run format` writes the fixes)
npm test          # vitest
```

Tests import `describe`/`it`/`expect` from `vitest` explicitly. Globals are not
enabled, so a test written without those imports typechecks and then fails at
runtime.

Formatting follows biome's defaults, with no config file here: `prek.toml`
already runs biome across the repo, and a second config would be a second
answer to the same question.

`@biomejs/biome` is pinned to an exact version, matching `prek.toml`'s hook
rev. A caret range resolved a newer biome than the commit hook runs, and the
two format the same file differently, so the hook wrote one shape and CI's
`npm run lint` demanded another on a file nobody had edited. Bump the two
together.

`npm ci` needs an npm new enough for the pinned node. If a stale global npm
shadows the one that node ships (`npm --version` well below node's major),
`npx npm@11 ci` runs the right one without changing anything on the machine.

## MCP Apps widgets

Four tools carry a widget (spec §7): `task_ready`, `workflow_project_status`,
`recall` and `task_list`. The server names each in the tool's
`_meta.ui.resourceUri` (`witan/ui_widgets.py`), and a host that renders MCP
Apps (Claude Desktop, claude.ai) loads it into a sandboxed iframe and hands it
the tool's result. Claude Code renders none and shows the text result, which
the binding does not change.

A widget is a presentation of the one result it is handed. `src/widgets/host.ts`
does the ext-apps handshake, unwraps the result with the same flags the page
uses, and draws it with the page's own renderers. It calls nothing back to the
server, since a widget that needs a second read shows nothing in every client
that cannot make one. For the same reason each widget draws only what its
result carries: the `task_ready` widget is the Ready column alone, not a board
with empty In progress and Blocked columns it never read.

Route links inside a widget are inert and styled as text, because there is no
router in the iframe. An external link, e.g. a project's PR, goes to the host
through `openLink`, the only way a sandboxed iframe can open one.

`npm run build` builds the page and then runs `build-widgets.js`, which builds
each `widgets/*.html` into one self-contained file with
`vite-plugin-singlefile`. Self-contained because the host's default CSP for a
widget is `connect-src 'none'`, and no `csp` is declared to widen it. Each is
about 256 KB (68 KB gzipped), most of it the ext-apps client and zod. The
server binds a tool only when its widget file exists, so a build that skipped
the frontend leaves the four tools exactly as they were.

## Where the bundle goes

`npm run build` writes `../witan/ui_dist/`, inside the Python package, and
`artifacts` in `../pyproject.toml` puts it in the sdist and the wheel. That
setting sits at the `[tool.hatch.build]` level rather than on the wheel target
alone, because `uv build` builds the wheel from the sdist: a path the sdist
drops never reaches the wheel.

Four places build it, and none of them may be dropped:

- `witan-ui.yml` on every PR touching this directory. Its `packaging` job also
  builds the wheel with a stubbed bundle and asserts the bundle is in it, so a
  packaging change that would ship a UI-less witan fails on the PR rather than
  at release;
- `publish-witan.yml` before `uv build`, then asserts the same thing on the
  real wheel;
- `docker/witan.Dockerfile`'s `ui-builder` stage, copied into the source tree
  before `uv sync`. `witan-ui.yml`'s `image` job builds that Dockerfile and
  asserts the bundle resolves from `witan.__file__`, which is the only check
  that looks at the deployed artifact rather than at a wheel.

**The checks are presence-only, not freshness.** On the release runner that is
enough: fresh checkout, `npm ci`, and vite's `emptyOutDir` mean the bundle in
the wheel was built in that job. Anywhere else it is not. A local `uv build` or
`pip install .` in a checkout holding an old `ui_dist` ships the old one
silently, and `vite build` overwrites `index.html` without deleting orphaned
hashed assets from an earlier build. Run `npm run build` before building a
wheel you intend to keep.

A source install that never ran the frontend build (`uv tool install`,
`pip install git+…`) ships no bundle at all. That is intended: the `/ui/`
routes register only when the directory exists, so such an install serves
`/mcp` exactly as before.
