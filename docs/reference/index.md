# Reference

Complete, precise, and generated. Every page in this section is derived from the
code it documents — the registered MCP tool objects, the cyclopts command tree,
the `.pg` schema files — and CI fails if a committed page no longer matches its
source.

That is a deliberate trade: these pages will never be as readable as the
[guides](../guides/index.md), and they will never be out of date.

<div class="grid cards" markdown>

-   **[MCP tools](mcp-tools/index.md)**

    All 60 tools your agent can call, with full parameter schemas. Grouped into
    [memory](mcp-tools/memory.md), [tasks](mcp-tools/tasks.md),
    [workflow](mcp-tools/workflow.md), and [code](mcp-tools/code.md).

-   **[CLI](cli.md)**

    Every `witan` command and flag, including `witan code …`, rendered from the
    live command tree.

-   **[Environment variables](environment.md)**

    All 67 settings, what they do, and their defaults.

-   **[Graph schema](graph-schema.md)**

    Node and edge types: `Memory`, `Task`, `WorkflowProject`, and the rest.
    The [bridge schema](bridge-schema.md) covers cross-repo linking.

</div>

## How configuration resolves

Three sources, highest precedence first:

1. **Environment variable** — always wins.
2. **`~/.config/witan/config.toml`** — including a matched `[targets.<name>]`
   block, which overrides the file's global settings.
3. **Built-in default.**

Both CLIs read the same config file, so configuring a deployment once points
`witan` and `witan code` at it together.

## Conventions in these pages

**Slug prefixes** identify a node's type at a glance:

| Prefix | Type |
| --- | --- |
| `pat-` | pattern memory |
| `pf-` | project fact |
| `les-` | lesson |
| `ctx-` | agent context |
| `tp-` | topic |
| `tk-` | task |
| `wp-` | workflow project |
| `ws-` | workflow session |

**Symbol ids** are `repo#path::Name`, e.g.
`https://github.com/mitodl/agent-kit#witan_core/repo_key.py::canonical`.

**Repo keys** are canonical HTTPS URIs — `https://github.com/mitodl/agent-kit`,
not `git@github.com:mitodl/agent-kit.git`. witan canonicalises what it detects;
pass the HTTPS form when passing one explicitly.

**Types** in parameter tables use `str`, `int`, `bool`, `list[str]`, with `?`
marking optional. A parameter shown as `—` in the Description column has no
docstring in the source yet.
