# omnigraph-codegraph

A tree-sitter-based **code graph** MCP server (Layer 2) for coding agents,
backed by [Omnigraph](https://github.com/ModernRelay/omnigraph). It indexes a
repository's symbols (functions, methods, classes, modules) and their
relationships, then exposes definition / reference / caller / impact queries to
the agent.

It is a self-contained sibling of `omnigraph-memory` (Layer 1) and shares its
subprocess/CLI conventions, but stores a **separate, per-repo, local-only**
graph.

## Two-layer composition

| Layer | Server | Stores | Scope | Synced |
|------|--------|--------|-------|--------|
| 1 | `omnigraph-memory` | patterns, project facts, lessons, workflow traces | team-wide | yes (S3) |
| 2 | `omnigraph-codegraph` | code symbols + edges | per-repo | **no — local only** |

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
matching `omnigraph-memory`'s `repo.py`), falling back to the git-root directory
name when there is no remote.

## Supported languages

Python first; TypeScript / TSX / JavaScript supported. Adding a language means
adding one `LanguageSpec` in `indexer.py` (extensions + grammar name + a
`queries_ts/<lang>.scm` capture file + a capture→kind map). Grammars are
provided prebuilt by `tree-sitter-language-pack` (no compilation needed).

## Install

```bash
./install.sh                      # verify omnigraph binary, prep code-store dir
```

Per-repo stores are created **lazily** on the first index — the indexer runs
`omnigraph init --schema code-schema.pg <store>` when the store is missing.

Build the graph for the current repo:

```bash
uvx --from . omnigraph-codegraph-index index        # incremental
uvx --from . omnigraph-codegraph-index reindex       # force rebuild
omnigraph-codegraph-index index path/to/file.py      # single file/subpath
```

Add the MCP server to your agent (snippets in `config/`):

- pi: `config/pi.json` → `~/.pi/agent/mcp.json`
- Claude: `config/claude.json` → `claude_desktop_config.json`
- Copilot: `config/copilot.json` → `.vscode/mcp.json`

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `OMNIGRAPH_CODEGRAPH_DIR` | `~/.local/share/omnigraph-memory/code` | directory of per-repo `<slug>.omni` stores |
| `OMNIGRAPH_CODEGRAPH_AUTHOR` / `USER` | `unknown` | attribution string |
| `OMNIGRAPH_CODEGRAPH_REPO` | — | override the detected repo slug |

The store URI for a repo is `<dir>/<sanitized-slug>.omni`, where the slug's `/`
and `:` are replaced with `_`.

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

## CLI

`omnigraph-codegraph-index` (cyclopts):

- `index [PATH]` — incremental; skips files whose content hash is unchanged.
- `reindex [PATH]` — force rebuild a path.

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
