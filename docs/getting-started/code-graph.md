# Indexing a repository

The code graph is a tree-sitter index of your repository: every symbol, where it
is defined, and what refers to it. It exists so an agent can ask *"who calls
this?"* or *"what breaks if I change this?"* and get an answer that understands
syntax rather than matching text.

Understanding syntax is not the same as being right. Call and reference edges
come from heuristic name resolution, not a resolved call graph, so they can miss
a caller or report one that isn't — see [edge precision
tiers](../explanation/code-graph/edge-precision-tiers.md). They beat grep
because they know a definition from a mention, not because they are ground
truth.

## Build the index

From inside a checkout:

```bash
witan code index
```

The first run walks the repository, parses every supported file, and writes a
per-repo store. Later runs are incremental — `witan code reindex` forces a full
rebuild when you need one.

```bash
witan code repos      # which repos are indexed, and how fresh
witan code branches   # which branch views exist for this repo
```

!!! note "Indexing is a CLI job; querying is not"

    The `witan code` CLI **builds and operates** the index — `index`, `reindex`,
    `optimize`, `branches`, `cleanup`. The questions you actually want answered
    are [MCP tools](../reference/mcp-tools/code.md), called by your agent. There
    is deliberately no `witan code find-definition` for you to type: these
    queries return graph rows meant to be reasoned over, not read in a terminal.

## Ask it something

In an agent session:

> Where is `resolve_target` defined, and who calls it?

The agent calls
[`code_find_definition`](../reference/mcp-tools/code.md#code_find_definition) to
get a `symbol_id`, then
[`code_callers`](../reference/mcp-tools/code.md#code_callers) with that id. A
symbol id looks like:

```
https://github.com/mitodl/agent-kit#packages/witan-core/witan_core/target_config.py::resolve_target
```

— repo, path, and symbol name, which is why it stays meaningful across
checkouts and machines.

The question worth learning is **blast radius**, before you edit something:

> What would be affected if I change the signature of `resolve_target`?

[`code_impact`](../reference/mcp-tools/code.md#code_impact) walks the caller
graph transitively and reports what sits downstream. Doing this *before* an edit
is the single highest-value use of the code graph.

## Across repositories

A service-oriented codebase has contracts that no single repository contains: an
env var one service sets and another reads, an HTTP endpoint one serves and
another calls, a package one publishes and another depends on.

The **bridge store** links repositories by those shared keys, so the graph can
answer questions that span two checkouts:

```
code_interface_providers(key="DATABASE_URL")   # who defines it
code_interface_consumers(key="DATABASE_URL")   # who reads it
code_cross_repo_impact(symbol_id=...)          # blast radius across repos
```

These only work for repositories that are actually indexed — the bridge joins
what it has. `code_indexed_repos` tells you what that is.

Indexing already writes the bridge bindings, so there is no extra build step.
Two read-only commands let you inspect what it produced:

```bash
witan code stitch                    # print the precise cross-repo edges
witan code stitch --unresolved       # external refs with no match yet
witan code symbols --role exported   # this repo's public contract surface
```

`stitch` computes and prints the join; it stores nothing.
[Stage-2 stitching](../explanation/code-graph/stage2-stitching.md) explains what
that join does and why it is a second pass.

## Branches

Each branch gets its own view, so an index built on a feature branch does not
disturb what everyone else reads.

One rule is worth knowing before you meet it on a shared graph: **there, only
a CI indexer may write a repo's default (`main`) view.** A process that has not
declared `WITAN_CODE_INDEX_ROLE=ci` is refused that write and the stale-file
purge that goes with it, so nobody's reindex can clobber the view every reader
falls back to.

It does not apply to the local store this tutorial uses. A local store has one
user, who is its writer, so indexing your checkout on its default branch just
works.

Idle branch views are reaped after 14 days by default. See [Branch
indexing](../guides/branch-indexing.md) and [ADR
0006](../explanation/decisions/0006-code-graph-branch-ownership-and-reaping.md).

## Keeping it current

An index is only as good as its freshness. Two mechanisms keep it current
without you thinking about it:

- **Session hooks** — `witan code session-init` and `reindex-hook` refresh the
  index around agent sessions.
- **CI indexing** — a scheduled job sweeps the repos in
  `WITAN_CODE_CI_REPOS` and writes the shared `main` view for everyone. That is
  the copy your teammates and any deployed witan actually read.

---

That is the tour. From here:

- [Guides](../guides/index.md) — specific tasks, in depth
- [Reference](../reference/index.md) — every tool, flag, and setting
- [Explanation](../explanation/index.md) — why it is built this way
