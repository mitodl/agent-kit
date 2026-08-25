---
name: witan-code
description: >
  Tree-sitter code graph for symbol lookups, caller graphs, change-impact
  analysis, and cross-repo contract tracing. Use instead of grep/Explore when
  you need exact symbol definitions, who-calls-what, blast radius before an
  edit, or which repo provides/consumes a shared env var, endpoint, package, or
  service. `/witan-code` reports whether the current repo is indexed and lists
  the available `code_*` tools; `/witan-code reindex` forces a full rebuild.
  Backed by the witan-code MCP server (code_* tools).
license: BSD-3-Clause
metadata:
  category: workflow
---

# Code Graph

A per-repo, tree-sitter-derived graph of symbols (functions, methods, classes,
modules) and their relationships (`Calls`, `References`, `Imports`,
`Inherits`), plus a cross-repo bridge linking services through shared
contracts (env vars, HTTP endpoints, packages, deployments). Indexing is
automatic — a `SessionStart` hook seeds/refreshes the whole repo in the
background, and a `PostToolUse` hook incrementally reindexes each file you
edit — so you rarely need to invoke the CLI yourself.

## When to use this vs. grep / the `Explore` agent

Reach for `code_*` tools when the question is **structural**, not textual:

- "Where is `Service.run` defined?" → `code_find_definition` / `code_search_symbol`
- "What calls this function, anywhere in the repo?" → `code_callers` / `code_find_references`
- "If I change this, what breaks?" (transitive blast radius) → `code_impact`
- "What symbols does this file define?" → `code_symbols_in_file`
- "Which other repo reads this env var / calls this endpoint / imports this package?" → `code_interface_consumers` / `code_interface_providers` / `code_cross_repo_impact`

Grep is still the right tool for literal string/comment searches, one-off text
matches, or a repo with no code graph yet. The two are complementary: grep
finds text; `code_*` tools resolve **symbols** and their **relationships**,
which grep cannot do (it can't tell you a function's transitive callers or
which service consumes an endpoint another service serves).

**Caveat:** `Calls`/`References`/`Imports`/`Inherits` edges are heuristic
(syntactic name resolution, not a true call graph) — treat impact/caller
results as a high-recall starting point, not a verified answer. `Defines` and
`Contains` are exact.

## On invocation

- No args → **Check readiness**, then show the tool reference below.
- `reindex` → call the `code_reindex` MCP tool (or `witan-code reindex` if
  running the CLI directly) to force a full rebuild of the current repo,
  ignoring content hashes. Use when the graph looks stale and you don't want
  to wait for the incremental hooks to catch up.

## Check readiness

Before relying on results, confirm the current repo has a graph: call
`code_symbols_in_file` on a file you know exists, or `code_search_symbol` with
a term you expect to match. An empty result on a file/symbol you know exists
usually means indexing hasn't finished yet (the `SessionStart` hook runs
detached in the background on a fresh repo) — wait a few seconds and retry
before concluding the graph is broken.

## Tool reference

| Question | Tool |
|---|---|
| Find a symbol by name | `code_find_definition(name)` / `code_search_symbol(query)` (BM25) |
| What's in this file? | `code_symbols_in_file(path)` |
| Who references/calls this symbol? | `code_find_references(symbol_id)` (References+Calls) / `code_callers(symbol_id)` (Calls only) |
| Blast radius of a change | `code_impact(symbol_id, max_depth=5, max_nodes=200)` — transitive callers via BFS |
| Force a reindex | `code_reindex(path=None)` |

Cross-repo (reads the shared bridge store; `kind` is one of `env_var` /
`endpoint` / `package` / `service`):

| Question | Tool |
|---|---|
| Who provides this contract? | `code_interface_providers(kind, key)` |
| Who consumes it? | `code_interface_consumers(kind, key)` |
| Every binding for this symbol's contracts, across repos | `code_cross_repo_impact(symbol_id)` |
| Search bindings by key | `code_interface_search(query, kind=None)` |
| What does this repo export / expect? | `code_repo_symbols(repo=None, role=None)` |
| Which repos depend on which? | `code_repo_dependencies(kind=None, repo=None)` |

Coverage — ask these before concluding "nothing uses X", since an unindexed
repo looks identical to an unused one:

| Question | Tool |
|---|---|
| Which repos are indexed, and how fresh? | `code_indexed_repos()` |
| Can the stores actually be read at all? | `code_store_health()` |
| Which branch views does a repo's store have? | `code_indexed_branches()` |

`code_store_health()` is the one to reach for when `code_interface_*` or
`code_cross_repo_impact` come back empty or erroring. Those four tools read one
shared *bridge* graph that belongs to no repo, so it shows up in no repo
listing — a bridge that cannot be opened breaks all of them while
`code_indexed_repos()` still lists every repo happily. In `code_indexed_repos`
output, `files: null` with a non-null `unreadable` means the same thing per
repo: that store errors, it is not merely sparse.

Every tool resolves the per-repo store from the current working directory and
returns `[]`/`null` gracefully when nothing is indexed yet — an empty result
is not necessarily an error, see **Check readiness** above.

## Linking to witan (tasks and memories)

`code_find_definition` / `code_search_symbol` return a `symbol_id` of the form
`<repo>#<path/to/file.py>::<QualifiedName>`. Attach it to a witan task
(`task_create(..., symbol_refs=[...])`, see `/witan-task`) or memory
(`memory_store(..., symbol_refs=[...])`, see `/witan-memory`) so the work or
lesson is discoverable from the code later. Going the other way,
`symbol_context(symbol_id)` (a witan tool) lists the tasks and memories
already attached to a symbol — call it before editing code that might have
open work or known gotchas attached.
