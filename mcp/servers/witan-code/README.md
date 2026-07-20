# witan-code

> "witan" is pronounced `WIT-ən` (/ˈwɪtən/) — rhymes with "written" minus the r.

A tree-sitter-based **code graph** MCP server (Layer 2) for coding agents,
backed by [Omnigraph](https://github.com/ModernRelay/omnigraph). It indexes a
repository's symbols (functions, methods, classes, modules) and their
relationships, then exposes definition / reference / caller / impact queries to
the agent.

It is a self-contained sibling of `witan` (Layer 1) and shares its
subprocess/CLI conventions, but stores a **separate, per-repo, local-only**
graph.

> **Wiring it into your agents locally** (MCP server, the `PostToolUse` reindex
> hook, and the indexer CLI, run straight from your checkout): see
> [Local Development Setup](../../../docs/agent-memory.md#local-development-setup).

## When to use this vs. grep / the `Explore` agent

Reach for `code_*` tools when the question is **structural**, not textual:
exact symbol definitions, who calls/references a symbol (and transitively —
change-impact / blast radius before an edit), what a file defines, or which
other repo provides/consumes a shared contract (env var, endpoint, package,
service). Grep still wins for literal string/comment searches and one-off text
matches — it finds *text*; this finds **symbols and their relationships**,
including things grep structurally cannot answer (a function's transitive
callers, or which service consumes an endpoint another service serves). See
the `/witan-code` skill for a full tool-selection guide and reference table.

`Calls`/`References`/`Imports`/`Inherits` edges are heuristic (syntactic name
resolution, not a true call graph) — see
[Heuristic edges](#heuristic-edges-important) below.

## Two-layer composition

| Layer | Server | Stores | Scope | Synced |
|------|--------|--------|-------|--------|
| 1 | `witan` | patterns, project facts, lessons, workflow traces | team-wide | yes (S3) |
| 2 | `witan-code` | code symbols + edges | per-repo | **no — local only** |

The two layers compose through **soft symbol-ID references**. A Layer-1 node
(e.g. a `lesson` or `agent_context`) can record symbol ids of the form:

```
repo#relative/path.py::QualifiedName
e.g. https://github.com/mitodl/ol-django#app/svc.py::Service.run
```

in a `symbolRefs`-style field. There is **no hard cross-store edge**: the id is
just a string that resolves in the code graph via `code_find_definition` /
`get_symbol`. This keeps the team-synced memory graph independent of any
machine's local code index.

## Documentation

- [User guide](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan-code/docs/USER_GUIDE.md) — task-oriented walkthrough: install,
  first index, definition/caller/impact queries, cross-repo bridge basics,
  troubleshooting.
- [CLI reference](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan-code/docs/CLI_REFERENCE.md) — every `witan-code` command with
  its full flag table and an example invocation.

## CLI structured output

`witan-code` table-producing commands can emit machine-readable output instead
of Rich tables:

```bash
witan-code --output-format json repos
witan-code --output-format yaml symbols --role exported
witan --output-format toml code stitch --unresolved
```

Supported formats are `txt` (default), `json`, `toml`, and `yaml`. The same
setting is available through `WITAN_OUTPUT_FORMAT`. It currently applies to
`repos`, `symbols`, and `stitch`; free-text commands such as `index` and hook
commands keep their existing output.

## Cross-repo context bridge (Layer 2.5)

The per-repo graph stops at a repo boundary, but service-oriented architectures
couple repos through **shared contracts**: an env var that infra sets and an app
reads, an HTTP endpoint one service serves and another calls, a package one repo
publishes and others import. The bridge records these as **interface bindings**
in a single shared, local-only store (`_bridge.omni`, a sibling of the per-repo
stores) so linkages can be queried across every indexed repo.

It is **zero-config**: every `index`/reindex of a repo also extracts that repo's
bindings into the bridge store. Linkages are then computed by grouping bindings
on `(kind, key_norm)` — no registry of repos to maintain, no link edges.

Four binding kinds (all extraction is **heuristic/syntactic**, like the symbol
edges):

| Kind | Provider | Consumer | Join key |
|---|---|---|---|
| `env_var` | Pulumi `<app>:env_vars` keys in `Pulumi.*.yaml` | `get_string("NAME",…)` / `os.environ` / `process.env.X` / `env("NAME")` | the `UPPER_SNAKE` name |
| `endpoint` | drf-spectacular OpenAPI spec (`paths.<path>.<method>`) | `/api/…` path literals in TS (generated client / fetch) | normalized path (params → `{}`) |
| `package` | a repo whose `package.json` name is `@mitodl/*` | `from mitol.*` / `include("mitol.*")` / `@mitodl/*` imports | the package name |
| `service` | `applications/<svc>/__main__.py` (repo / image / service name) | the deployed repo itself (key_norm = its canonical URI) | `repo:<uri>` / `image:…` / `name:…` |

Generic env names (`DEBUG`, `PORT`, `SECRET_KEY`, …) are flagged `generic` and
excluded from cross-repo impact fan-out so a trivial edit doesn't appear to touch
every repo.

The bridge is written in a **separate phase after the per-repo store write**, so
the two stores' advisory write locks never nest (no deadlock) and a bridge
failure never corrupts a per-repo store. A full-repo index runs the repo-level
provider extractors (OpenAPI / Pulumi / service / `package.json`) and clears
bindings for files deleted from disk; all purging is per-file, so unchanged
(skipped) files — and, for a narrow target like the reindex hook, sibling
files — keep their bindings.

Every binding also carries a **canonical symbol string**
(`{scheme}:{manager}:{package}:{version}:{descriptor}`, SCIP-inspired) — the
join key for the precise cross-repo linking tier. Provider symbols are
qualified by the repo's declared package identity from an optional
`witan-code.toml` at the repo root; repos without one get a fallback identity
derived from the repo URI. See [docs/SYMBOL_FORMAT.md](docs/SYMBOL_FORMAT.md)
and [docs/PACKAGE_MAP.md](docs/PACKAGE_MAP.md).

Each bridge write also rebuilds the repo's **symbol table** — one `RepoSymbol`
row per (repo, role, symbol): `exported` rows are the repo's public contract
surface, `external` rows are unresolved references to other repos' contracts.
This deduplicated table is the Stage-2 read-time join artifact; inspect it
with `witan code symbols`. See [docs/SYMBOL_TABLE.md](docs/SYMBOL_TABLE.md).

Stage 2 itself — joining external symbol references against other repos'
exported symbols by canonical string, entirely at read time, never stored —
is `witan_code.stitch.resolve()`. Inspect it with `witan code stitch`
(`--unresolved` for indexing-coverage gaps) or the `code_precise_edges` /
`code_unresolved_symbols` MCP tools. See
[docs/STAGE2_STITCHING.md](docs/STAGE2_STITCHING.md).

Precise (Stage 2) and heuristic (Stage 3) edges are merged into one **typed,
precision-tiered** result by `witan_code.edges.cross_repo_edges()`. Every
tool that produces cross-repo links — `witan code deps`,
`code_interface_providers`/`_consumers`/`_search`, `code_cross_repo_impact` —
accepts `min_precision` (`"precise" | "heuristic" | "fuzzy"`, default
`"heuristic"` = current behavior unchanged). See
[docs/EDGE_PRECISION_TIERS.md](docs/EDGE_PRECISION_TIERS.md).

## Heuristic edges (important)

`Defines` and `Contains` are exact (derived from the syntax tree). But:

- **`Calls`**, **`References`**, **`Imports`**, and **`Inherits`** are
  **HEURISTIC**. They are produced by syntactic name resolution: a call/base/
  import identifier is matched to a known `Symbol` name, preferring same-file
  definitions. Dynamic dispatch, re-exports, shadowing, and cross-file
  resolution beyond name matching are **not** modeled precisely.

Treat caller/impact results as a high-recall starting point for investigation,
not a verified call graph.

## Symbol id scheme

- `CodeFile.id` = `repo#relative/path.py`
- `Symbol.id` = `repo#relative/path.py::QualifiedName` (e.g. `Class.method`)

The repo key is the canonical HTTPS remote URI (`https://github.com/org/repo`,
matching `witan`'s `repo.py`), falling back to the git-root directory
name when there is no remote.

## Supported languages

| Language | Extensions | Symbols extracted |
|---|---|---|
| Python | `.py` `.pyi` | functions, classes, methods, module |
| TypeScript / JS / JSX / TSX | `.ts` `.tsx` `.mts` `.cts` `.js` `.jsx` `.mjs` `.cjs` | functions, arrow consts, classes, methods (incl. arrow fields), interfaces, types, enums |
| Bash / Zsh | `.sh` `.bash` `.zsh` | functions |
| YAML | `.yaml` `.yml` | mapping keys as dotted paths (e.g. `jobs.build.steps`) |

Each symbol also carries **richer attributes** for agent context: the full
`signature` (name + multi-line params + return/param types), a `docstring`
(Python docstrings and TS/JS `/** … */` JSDoc), and `decorators`
(`@app.route(...)`, `@task`, `@Input()`, … — framework semantics that also feed
the cross-repo bridge). These are returned by `code_find_definition` /
`code_search_symbol` / `code_symbols_in_file`.

All JS/TS variants are parsed with the `tsx` grammar (a superset of TS, JS, and
JSX) — the plain `javascript` grammar rejects the TS node types in the shared
query. YAML indexes every mapping key as a navigable `key` symbol, so large
config trees (CI, k8s, Pulumi) can be searched by path; expect many such symbols.

Grammars come from **individual `tree-sitter-<lang>` wheels** (e.g.
`tree-sitter-python`), loaded as standalone `tree_sitter.Language`s — not
`tree-sitter-language-pack` (whose 1.9 line returns an incompatible binding and
downloads grammars on demand). Adding a language means: add its
`tree-sitter-<lang>` wheel to `pyproject.toml` (tight-pinned), add an entry to
`indexer._GRAMMAR_MODULES` (grammar name → module + capsule factory), add one
`LanguageSpec` (extensions + grammar name + a `queries_ts/<lang>.scm` capture
file + a capture→kind map), and any new node types to `_DEF_NODE_TYPES`.

### Not indexed yet (from a file-type survey of ol-infrastructure + mit-learn)

- **HCL / Terraform / Packer** (`.hcl`, `.tf`) — the strongest candidate to add
  next (~50 files in ol-infrastructure); grammar is available and blocks map to
  symbols (`variable.x`, `source.amazon-ebs.caddy`, `resource.<type>.<name>`).
- **VCL** (Varnish/Fastly, `.vcl`) — no published `tree-sitter-vcl` wheel, so it
  can't be added without vendoring/building one.
- **Markdown** (`.md`/`.mdx`) — could index headings as a doc outline, but adds
  low-signal symbols; left out for now.
- Config/markup (`.json`, `.toml`, `.ini`, `.scss`, `.html`, `.j2`) is not
  symbol-bearing enough to index. Go/Rust grammars exist but neither repo uses them.

## Install

witan-code is usually installed as part of the `witan` umbrella — `witan setup`
wires both servers together via `uvx --from … --with …`. See the
[witan README](../witan/README.md) for the one-step setup.

To use witan-code **standalone** (code graph only, no memory/task tools):

**From PyPI:**

```bash
# One-shot (uvx):
uvx --from witan-code witan-code index

# Persistent CLI install:
uv tool install witan-code
witan-code index
```

**From the published git repo** (to track pre-release/unreleased code):

```bash
# One-shot (uvx):
uvx --from git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan-code \
    witan-code index

# Persistent CLI install:
uv tool install git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan-code
witan-code index
```

**From a local checkout (inside `mcp/servers/witan-code/`):**

```bash
uvx --from . witan-code index          # incremental
uvx --from . witan-code reindex        # force rebuild
uvx --from . witan-code index path/to/file.py   # single file/subpath

# Or via uv run:
uv run witan-code index
```

**Full standalone install:** `witan-code setup` installs everything a
witan-code-only deployment needs — the `omnigraph` binary, the `/witan-code`
skill, all four hooks (or the Pi extension), and the MCP server entry — for
one agent or all detected ones:

```bash
witan-code setup                    # Claude Code (default)
witan-code setup --agent pi         # Pi
witan-code setup --agent all        # every detected agent
witan-code setup --dry-run          # preview without writing
witan-code setup --author "Jane Doe"  # attribution (default: git config user.name)
```

If `witan` is *also* installed and witan-code is importable in that same
environment (e.g. via the `--with` in the `uv tool install`/MCP server's
`uvx` invocation), `witan setup` folds this same bundle in automatically —
skill and hooks (registered as `witan code …`, so only `witan` needs to be on
`PATH`), but **not** a second MCP server entry, since `witan serve` already
mounts witan-code's tools in-process. One `witan setup` then covers both
packages, and a separate `witan-code setup` isn't required (though re-running
it afterwards is harmless — `apply()` is an idempotent read-merge-write, and
it *will* add its own standalone MCP entry/`witan-code …` hooks alongside
witan's). Run `witan-code setup` on its own for a witan-code-only install, or
when witan-code isn't importable from witan's environment. See
[Hooks](#hooks) and [Skill](#skill).

This downloads the omnigraph release pinned by `_OMNIGRAPH_VERSION` in the
shared installer
[`witan_core.omnigraph_install`](../../../packages/witan-core/witan_core/omnigraph_install.py)
— the same pin `witan` uses (both import it from `witan-core`), bumped by
Renovate (see the repo-root `renovate.json`'s `omnigraph-version`
customManager). There is no build-time bundling of the binary into the wheel —
`witan-code setup` (or `witan setup`) is the only source of the binary, and
re-running it is how you pick up a version bump.

Per-repo stores are created **lazily** on the first index — the indexer runs
`omnigraph init --schema code-schema.pg <store>` when the store is missing.
The `./install.sh` script only verifies the omnigraph binary (installing the
latest upstream release directly if missing, independent of the
`witan-code setup`/`_OMNIGRAPH_VERSION` pin) and prints a hint; it is not
required when installing via uvx/uv.

**Manual / unsupported-agent install:** for an agent `witan-code setup`
doesn't cover, copy the appropriate MCP snippet from `config/` into your
agent's config, and see [Hooks](#hooks)/[Skill](#skill) for the manual
symlink alternative:

- pi: `config/pi.json` → `~/.pi/agent/mcp.json`
- Claude: `config/claude.json` → `claude_desktop_config.json`
- Copilot: `config/copilot.json` → `.vscode/mcp.json`

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `WITAN_CODE_DIR` | `~/.local/share/witan/code` | directory of per-repo `<slug>.omni` stores |
| `WITAN_AUTHOR` / `USER` | `unknown` | attribution string |
| `WITAN_REPO` | — | override the detected repo slug |
| `WITAN_CODE_OPTIMIZE_INTERVAL` | `86400` (daily) | throttle window (seconds) for `checkpoint`'s opportunistic store compaction; `0` disables it |
| `WITAN_CONFIG` | `~/.config/witan/config.toml` | config file path (see below) |
| `WITAN_TARGET` | — | force a named `[targets.<name>]` block instead of auto-detecting one |

The store URI for a repo is `<dir>/<sanitized-slug>.omni`, where the slug's `/`
and `:` are replaced with `_`. The shared cross-repo bridge lives alongside them
at `<dir>/_bridge.omni` and is created lazily on the first index that yields any
bindings.

### config.toml

witan-code reads the same `config.toml` as witan (witan-council) — a global
`code_dir`/`author`, plus named `[targets.<name>]` blocks that override them
and are selected by `WITAN_TARGET`, an explicit `--target`, or auto-detection
against the current repo/checkout (`match_paths` > `match_repos` >
`match_hosts` > `match_orgs` — see witan's README/`witan/config.py`
docstring for the full precedence). Because the file is shared, one target
block can carry witan's `server`/`graph`/`token` alongside `code_dir` under
the same name — each server reads only the fields it knows:

```toml
[targets.work]
server = "http://witan.internal:8080"  # witan (witan-council)
code_dir = "/mnt/work/witan-code"      # witan-code
match_orgs = ["myorg"]
```

Environment variables always win over `config.toml`.

## MCP tools

Each tool resolves the per-repo store from the current working directory and
returns `[]` / `null` gracefully when the store does not exist yet (run the
indexer first).

| Tool | Returns |
|------|---------|
| `code_find_definition(name)` | symbols whose name / qualified name matches |
| `code_find_references(symbol_id)` | incoming `References` + `Calls` |
| `code_callers(symbol_id)` | incoming `Calls` |
| `code_impact(symbol_id, max_depth=5, max_nodes=200)` | transitive callers via BFS |
| `code_symbols_in_file(path)` | symbols defined in a file |
| `code_search_symbol(query)` | BM25 search over symbol qualified names |
| `code_reindex(path=None)` | index/re-index the repo or a subpath |

Cross-repo bridge tools (resolve the shared `_bridge.omni` store; return `[]` /
an empty shape when it does not exist yet). When the current checkout is on a
non-default git branch with in-flight bindings, these auto-detect and read
that repo's bridge branch overlay instead of `main` — see
[docs/BRANCH_INDEXING.md § Bridge store](docs/BRANCH_INDEXING.md#bridge-store):

| Tool | Returns |
|------|---------|
| `code_interface_providers(kind, key)` | repos that **provide** a contract (`env_var`/`endpoint`/`package`/`service`) |
| `code_interface_consumers(kind, key)` | repos that **consume** it (`endpoint` keys are normalized from raw paths) |
| `code_cross_repo_impact(symbol_id)` | the symbol's own bindings + every binding for those same contracts in **other** repos |
| `code_interface_search(query, kind=None)` | BM25 search over interface bindings by normalized key |

## CLI

`witan-code` (cyclopts); also available as `witan code …` when witan-code is installed alongside witan:

- `setup [--dry-run]` — fetch the pinned omnigraph binary to `~/.local/bin/`
  for standalone use (see [Install](#install)).
- `index [PATH]` — incremental; skips files whose content hash is unchanged.
- `reindex [PATH]` — force rebuild a path.
- `repos` — list all indexed repos with file count, symbol count, and store size.
- `branches [--prune]` — list omnigraph branches per store; `--prune` deletes
  the current repo's store branches whose git branch is gone (plus
  `_detached`). Non-default git branches index onto same-named omnigraph
  branches so in-flight work never overwrites the shared `main` view — see
  [docs/BRANCH_INDEXING.md](docs/BRANCH_INDEXING.md).
- `deps [--kind K] [--repo SUBSTR] [--html PATH] [--open-browser]` —
  visualize cross-repo dependencies from the bridge store. Prints a Rich
  summary of "repo A depends on repo B" links (A consumes a contract B
  provides; for `service`, the deploying repo depends on what it deploys) and,
  with `--html`, writes a self-contained interactive force-directed graph
  (click an edge to list the individual linkages — env vars, endpoints,
  packages, deployed repos — behind it). Defaults to a repos-only view;
  `--kind` filters to one contract kind and `--repo` keeps only links touching
  a matching repo.
- `session-init` — seed/refresh the whole repo's code graph in the background
  (see [Hooks](#hooks)); registered as the `SessionStart` hook, not usually
  run by hand.
- `reindex-hook` — incrementally reindex the file named in stdin's hook JSON
  (see [Hooks](#hooks)); registered as the `PostToolUse` hook, not usually
  run by hand.
- `inject-context` — print the `UserPromptSubmit` status block (see
  [Hooks](#hooks)); registered as the `UserPromptSubmit` hook, not usually
  run by hand.
- `optimize [--store PATH] [--bridge]` — compact a store's Lance fragments
  (non-destructive; see [Store compaction](#store-compaction)). Defaults to
  the current repo's store; `--bridge` targets the shared bridge store
  instead.
- `cleanup [--store PATH] [--bridge] [--keep N] [--older-than DUR] --yes` —
  reclaim disk by GC'ing old Lance versions (**destructive**; requires
  `--yes`).
- `checkpoint` — opportunistically compact the current repo's store and the
  bridge store if due (see [Hooks](#hooks)); registered as the `Stop` hook,
  not usually run by hand.

Both print a summary: files scanned/indexed/skipped, symbols, edges, errors. A
parse failure on one file logs to stderr and continues.

## Store compaction

Every `load()`/`mutate()` call (indexing) appends a new tiny Lance fragment +
manifest version to a store; left uncompacted, it bloats until *opening* the
store dominates query latency, regardless of row count — the same failure
mode witan's own store hit (PR #98; see
[`witan/witan/maintenance.py`](../witan/witan/maintenance.py)). Two
mechanisms keep this in check, mirroring that module (deliberately duplicated
— no cross-package import):

- **Opportunistic**: the `witan-code checkpoint` Stop hook spawns a
  throttled, detached `witan-code optimize` for the current repo's store and
  the shared bridge store, at most once per `WITAN_CODE_OPTIMIZE_INTERVAL`
  (default daily; `0` disables) each — see [Hooks](#hooks).
- **Scheduled**: `witan-code optimize [--store PATH | --bridge]` /
  `witan-code cleanup --yes` for cron/systemd-timer driven maintenance on a
  busy store. `optimize` is non-destructive and safe to run repeatedly;
  `cleanup` GCs old Lance versions to reclaim disk and is destructive, so it
  requires `--yes`.

## Skill

[`witan_code/skills/witan-code/SKILL.md`](witan_code/skills/witan-code/SKILL.md)
is a `/witan-code` entry point covering tool selection (vs. grep/Explore), a
quick tool reference, and linking symbol ids into witan tasks/memories.
Installed automatically by `witan-code setup` (to `~/.claude/skills/`, or
`~/.pi/agent/skills/` under `--agent pi`); to install it manually instead,
symlink or copy the directory into the equivalent path for your agent.

## Hooks

Four hooks — all bare CLI commands, no wrapper scripts, so they're portable
to any platform witan-code installs on (Windows included — no bash/setsid
dependency). Named `witan-code <command>` below (standalone install via
`witan-code setup`); when `witan setup` folds this bundle in instead (see
[Install](#install)), they register as `witan code <command>` so only
`witan` needs to be on `PATH`. Register manually per
[configs/hooks/README.md](../../../configs/hooks/README.md) for either form.
A Pi equivalent of all four lives in one extension,
[`witan_code/extensions/pi/codegraph.ts`](witan_code/extensions/pi/codegraph.ts)
(see [configs/pi/README.md](../../../configs/pi/README.md)):

- **`witan-code session-init`** (`SessionStart`) — seeds/refreshes the whole
  repo in the background on session start (first run builds the full index;
  later runs re-hash and skip unchanged files). Detaches a background child
  and returns immediately; never delays session start. A per-repo lock
  (`${TMPDIR:-/tmp}/codegraph-init-<sha256(project_dir)[:16]>.lock`, released
  by the detached child when it finishes) prevents overlapping sessions from
  indexing at once — hashed rather than a sanitized path so two distinct
  paths can't collide on the same lock and a long checkout path can't exceed
  a filesystem's filename length limit.
- **`witan-code reindex-hook`** (`PostToolUse`, matcher `Edit|Write`) — reads
  the tool payload from stdin and incrementally re-indexes the changed file
  in the foreground (fast — one file). Best-effort — a missing/malformed
  payload or parse failure is a silent no-op, never interrupting the agent.
- **`witan-code inject-context`** (`UserPromptSubmit`) — prints a short status
  block: whether the current repo is indexed (file count, last-updated time),
  or that a background index from `session-init` is still running (checking
  the same lock path above), plus a nudge to prefer `code_*` tools over grep.
  Independent of `witan`'s own `inject-context` hook (no cross-package
  coupling) — register it alone for a witan-code-only install. Prints
  nothing when the repo has neither a store nor an index in flight.
- **`witan-code checkpoint`** (`Stop`) — spawns a throttled, detached
  `witan-code optimize` for the current repo's store and the shared bridge
  store if either is due (see [Store compaction](#store-compaction)).
  Independent of `witan`'s own `session-checkpoint` hook (no cross-package
  coupling). Best-effort and non-blocking.

Manual install: register the bare commands directly under the matching event
in `settings.json` (see the linked README for the exact JSON) — there are no
scripts to symlink.

## Incremental indexing

Each `CodeFile` stores a sha256 `contentHash`. On reindex, if the hash is
unchanged the file is skipped. Otherwise its `Symbol`s and `CodeFile` are
deleted (as separate `delete.gq` calls — Omnigraph cannot mix deletes with
inserts in one query) and then re-parsed and re-inserted.
