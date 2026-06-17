---
name: agent-memory
description: >
  Read from and write to the team's shared agent memory graph. Use when
  starting work in a repository (load project facts and patterns), after
  solving a non-obvious problem (store a pattern), when discovering
  structural information about a codebase (store a project fact), or when
  a correction was needed (store a lesson). Requires the omnigraph-memory
  MCP server to be configured.
---

# Agent Memory

The team's shared knowledge graph stores four kinds of memories, all
backed by Omnigraph and accessible via the `omnigraph-memory` MCP server.
The repo is auto-detected from `.git/config` — you rarely need to pass it
explicitly.

## This graph vs. `CLAUDE.md` / `AGENTS.md`

`CLAUDE.md` / `AGENTS.md` hold **static, human-committed** project instructions
that load into every session automatically. This graph holds **facts, patterns,
and lessons you accrue and query on demand**, shared across agents and repos.
Don't duplicate committed `CLAUDE.md` content into the graph; use the graph for
what you learn while working that isn't already written down.

## When to Use Each Tool

### `memory_get_project_facts` — load context at session start

Call this **first** whenever you start working in a repository you haven't
used in this session:

```
memory_get_project_facts()
```

Returns all structural facts for the current repo: architecture, deployment
topology, testing conventions, known dependencies, environment quirks. Read
these before writing code, choosing a library, or making deployment decisions.

### `memory_list_patterns` — check conventions before writing code

Before implementing something non-trivial, check what patterns the team has
already documented:

```
memory_list_patterns()                          # all patterns in this repo
memory_list_patterns(language="python")         # filtered by language
```

### `memory_search` — find relevant context by topic

When you need to know if the team has encountered something similar before:

```
memory_search("vault secrets injection")
memory_search("database migration rollback strategy")
memory_search("rate limiting approach", kind="pattern")
```

### `memory_store` — record something worth remembering

**Store a `pattern`** after solving a problem in a non-obvious way, or when
you apply a team convention that should be made explicit:

```
memory_store(
    kind="pattern",
    title="Always use uv, never pip",
    content="All Python work in this repo uses uv for environment management. ...",
    language="python",
    tags=["tooling", "environment"]
)
```

**Store a `project_fact`** when you learn something structural about a
codebase that a future agent would need to know:

```
memory_store(
    kind="project_fact",
    title="Vault secrets injected via env at runtime",
    content="This service reads secrets from Vault at startup via the ...",
    category="deployment"
)
```

**Store a `lesson`** when a mistake was made or a correction was needed:

```
memory_store(
    kind="lesson",
    title="Do not run migrations without a backup in staging",
    content="On 2025-05-10, a migration was run without a prior snapshot ...",
    severity="warning"
)
```

**Store `agent_context`** when handing off a task or leaving breadcrumbs
for a future agent session:

```
memory_store(
    kind="agent_context",
    title="Ticket 1234 — approach taken",
    content="Chose to use the existing TaskQueue infrastructure rather than ...",
    tags=["ticket-1234"]
)
```

## Quality Guidelines

- **Be specific.** Vague memories degrade search quality. Include the what,
  why, and any relevant examples in `content`.
- **One idea per memory.** Split broad topics. "uv for packaging" and "pytest
  config conventions" are two memories, not one.
- **Don't store transient state.** Session-specific observations that won't
  be useful after the current task ends don't belong in the graph.
- **Check before storing.** Run `memory_search` first. If a similar memory
  already exists, consider whether to update it instead of creating a
  duplicate.

## Updating an Existing Memory

Use `memory_get` to fetch the current content, decide what to change, then
`memory_store` a corrected version. There is no in-place update or `Supersedes`
tool yet, so for a superseded (rather than simply wrong) memory, store the new
version and reference the old slug in its content for now.
