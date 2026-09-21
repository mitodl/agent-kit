# witan UI

The web UI for the witan graph: tasks, projects, sessions, memory and
contradictions, rendered for a person rather than for an agent.

Design: `docs/internals/design/witan-ui-spec.md`. Transport: ADR 0011,
`mcp/servers/witan/docs/adr/0011-witan-ui-read-transport.md` — the page is an
MCP client speaking streamable-http to a witan server on its own origin, bound
to an enumerated set of read tools. (Both are cited by path rather than linked:
they land in their own changes, and a relative link would be dead here until
those merge.)

This is a scaffold. It builds, typechecks, lints and tests, and it renders the
app frame; the views behind the tabs are separate tasks (spec §6).

## Layout

It sits here rather than under `packages/` because it is not independently
versioned. It ships inside the witan wheel and moves with the tools it reads,
so its version is witan's.

```
ui/
  src/            The app. `shell.ts` is the frame; views land beside it.
  vite.config.ts  The build. Writes ../witan/ui_dist, the wheel picks it up.
  vitest.config.ts
  .node-version   The exact node pin, read by both GitHub workflows.
```

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

`npm ci` needs an npm new enough for the pinned node. If a stale global npm
shadows the one that node ships (`npm --version` well below node's major),
`npx npm@11 ci` runs the right one without changing anything on the machine.

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
  before `uv sync`.

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
