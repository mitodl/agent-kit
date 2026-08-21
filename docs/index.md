# witan-context

**Shared memory, work coordination, and a code graph for coding agents.**

A coding agent starts every session knowing nothing. It re-derives the same
facts, rediscovers the same constraints, and repeats the same mistakes — and
when two agents work in parallel, neither knows what the other is doing.

witan is the missing layer: a persistent, team-wide graph that agents read from
and write to. What one session learns, the next one recalls. What one agent
is working on, the others can see.

---

## The three layers

<div class="grid cards" markdown>

-   **Memory**

    Durable, shareable knowledge — patterns, project facts, lessons, decisions.
    Memories link to each other (`supersedes`, `refines`, `contradicts`), so
    recall returns what is *current*, not merely what matched.

    [Memory tools →](reference/mcp-tools/memory.md)

-   **Work coordination**

    Tasks, dependency edges, and multi-session projects. Claiming is a
    best-effort compare-and-swap with a lease — enough for parallel agents to
    divide work without double-doing it.

    [Task tools →](reference/mcp-tools/tasks.md)

-   **Code graph**

    A tree-sitter index for exact symbol lookups, caller graphs, and
    change-impact analysis — plus a cross-repo bridge that traces a shared env
    var, endpoint, or package from provider to consumer.

    [Code tools →](reference/mcp-tools/code.md)

</div>

All three are served by **one MCP endpoint**. A single `witan serve` mounts 60
tools; one entry in your agent's config gets you the whole surface.

---

## Start here

| If you want to… | Go to |
| --- | --- |
| Install it and store your first memory | [Get started](getting-started/index.md) |
| Do a specific thing — index a repo, run against a deployed service, migrate a store | [Guides](guides/index.md) |
| Look up a tool, flag, env var, or node type | [Reference](reference/index.md) |
| Understand *why* it works the way it does | [Explanation](explanation/index.md) |

---

## Quick start

```bash
uv tool install ol-agent-kit
witan setup
```

`witan setup` registers the MCP server and its skills with whichever coding-agent
platforms it finds locally — Claude Code, Pi, GitHub Copilot, OpenCode, and
others. Then, from inside any git repository:

```bash
witan tasks                 # ready work in this repo
witan memory "vault auth"   # what do we know about this?
witan code index            # build this repo's code graph
```

Then ask your agent things like *"who calls `retry_with_backoff`?"* or *"what
breaks if I change this signature?"* — the `code_*` tools answer from the index
rather than from grep.

See [Installation](getting-started/installation.md) for the other install paths
and for pointing witan at a shared, deployed service instead of a local store.

---

## What makes up witan

witan-context covers three published packages, all developed in the
[`mitodl/agent-kit`](https://github.com/mitodl/agent-kit) monorepo:

| Package | What it is |
| --- | --- |
| [`witan-council`](https://pypi.org/project/witan-council/) | The memory, task, and workflow tools, plus the `witan` umbrella CLI |
| [`witan-code`](https://pypi.org/project/witan-code/) | The tree-sitter code graph and cross-repo bridge; mounts as `witan code` |
| [`witan-core`](https://pypi.org/project/witan-core/) | [Shared internals](explanation/witan-core.md): the graph client, OIDC/remote transport, observability |
| [`ol-agent-kit`](https://pypi.org/project/ol-agent-kit/) | Meta-package that installs all of the above in one shot |

Storage is [omnigraph](https://github.com/ModernRelay/omnigraph) — a local file, an
`s3://` bucket, or a shared `omnigraph-server`. The same tools work against all
three; only [`WITAN_MEMORY_URI`](reference/environment.md) changes.
