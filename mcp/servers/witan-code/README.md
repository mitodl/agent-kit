# witan-code

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
failure never corrupts a per-repo store. A full-repo index purges bindings by
repo and runs the repo-level provider extractors (OpenAPI / Pulumi / service /
`package.json`); a narrow target (single file via the reindex hook) only
refreshes the files it touched, leaving sibling bindings intact.

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

**From the published git repo:**

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

Per-repo stores are created **lazily** on the first index — the indexer runs
`omnigraph init --schema code-schema.pg <store>` when the store is missing.
The `./install.sh` script only verifies the omnigraph binary and prints a hint;
it is not required when installing via uvx/uv.

To add witan-code as a standalone MCP server (without the witan memory/task
tools), copy the appropriate snippet from `config/` into your agent's config:

- pi: `config/pi.json` → `~/.pi/agent/mcp.json`
- Claude: `config/claude.json` → `claude_desktop_config.json`
- Copilot: `config/copilot.json` → `.vscode/mcp.json`

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `WITAN_CODE_DIR` | `~/.local/share/witan/code` | directory of per-repo `<slug>.omni` stores |
| `WITAN_AUTHOR` / `USER` | `unknown` | attribution string |
| `WITAN_REPO` | — | override the detected repo slug |

The store URI for a repo is `<dir>/<sanitized-slug>.omni`, where the slug's `/`
and `:` are replaced with `_`. The shared cross-repo bridge lives alongside them
at `<dir>/_bridge.omni` and is created lazily on the first index that yields any
bindings.

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
an empty shape when it does not exist yet):

| Tool | Returns |
|------|---------|
| `code_interface_providers(kind, key)` | repos that **provide** a contract (`env_var`/`endpoint`/`package`/`service`) |
| `code_interface_consumers(kind, key)` | repos that **consume** it (`endpoint` keys are normalized from raw paths) |
| `code_cross_repo_impact(symbol_id)` | the symbol's own bindings + every binding for those same contracts in **other** repos |
| `code_interface_search(query, kind=None)` | BM25 search over interface bindings by normalized key |

## CLI

`witan-code` (cyclopts); also available as `witan code …` when witan-code is installed alongside witan:

- `index [PATH]` — incremental; skips files whose content hash is unchanged.
- `reindex [PATH]` — force rebuild a path.
- `repos` — list all indexed repos with file count, symbol count, and store size.
- `deps [--kind K] [--repo SUBSTR] [--html PATH] [--open-browser]` —
  visualize cross-repo dependencies from the bridge store. Prints a Rich
  summary of "repo A depends on repo B" links (A consumes a contract B
  provides; for `service`, the deploying repo depends on what it deploys) and,
  with `--html`, writes a self-contained interactive force-directed graph
  (click an edge to list the individual linkages — env vars, endpoints,
  packages, deployed repos — behind it). Defaults to a repos-only view;
  `--kind` filters to one contract kind and `--repo` keeps only links touching
  a matching repo.

Both print a summary: files scanned/indexed/skipped, symbols, edges, errors. A
parse failure on one file logs to stderr and continues.

## Reindex hook

`configs/hooks/codegraph-reindex.sh` is a `PostToolUse` hook (matcher
`Edit|Write`) that incrementally re-indexes a single changed source file. It is
best-effort and non-blocking — it always exits 0 and silences output, so a
missing binary or parse failure never interrupts the agent.

Install: symlink to `~/.claude/hooks/codegraph-reindex.sh` and register under
`hooks.PostToolUse` with matcher `Edit|Write`.

## Incremental indexing

Each `CodeFile` stores a sha256 `contentHash`. On reindex, if the hash is
unchanged the file is skipped. Otherwise its `Symbol`s and `CodeFile` are
deleted (as separate `delete.gq` calls — Omnigraph cannot mix deletes with
inserts in one query) and then re-parsed and re-inserted.
