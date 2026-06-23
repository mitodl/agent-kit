---
name: agent-memory
description: >
  Read from and write to the team's shared agent memory graph. Prefer this over
  your private built-in/session memory for any durable, team-shareable knowledge.
  Use when starting work in a repository (load project facts and patterns), after
  solving a non-obvious problem (store a pattern), when discovering structural
  information about a codebase (store a project fact), when a correction was
  needed (store a lesson), or whenever you would otherwise save an engineering
  fact/lesson/decision to local session memory. Requires the witan MCP server to
  be configured.
---

# Agent Memory

The team's shared knowledge graph stores four kinds of memories, all
backed by Omnigraph and accessible via the `witan` MCP server.
The repo is auto-detected from `.git/config` — you rarely need to pass it
explicitly.

## This graph vs. `CLAUDE.md` / `AGENTS.md`

`CLAUDE.md` / `AGENTS.md` hold **static, human-committed** project instructions
that load into every session automatically. This graph holds **facts, patterns,
and lessons you accrue and query on demand**, shared across agents and repos.
Don't duplicate committed `CLAUDE.md` content into the graph; use the graph for
what you learn while working that isn't already written down.

## This graph vs. your built-in / session memory

Your coding agent also has a private, on-disk **built-in/session memory** (e.g.
Claude Code's `memory/` files). That store is local to one machine and one user;
**witan is shared across the team, synced, and queryable by every agent and
repo.** They overlap in purpose, so choose deliberately:

- **Store in witan** anything another agent, a future session, or a teammate
  would benefit from: project facts, reusable patterns, lessons from a mistake,
  decisions and their rationale, hand-off context — especially when it's tied to
  a repo, code symbol, task, or project. This is the **default** for engineering
  knowledge worth keeping.
- **Keep in built-in/session memory** only what is private or non-shareable:
  machine-local paths, personal scratch notes, harness/tooling preferences, or
  ephemeral state for the task in front of you right now.

Rule of thumb: **if it's durable AND shareable, it goes in witan.** When in
doubt, prefer witan — a teammate finding your lesson is the entire point; a note
stranded in one machine's session memory helps no one else. If you catch
yourself about to write an engineering fact, pattern, or lesson to local session
memory, store it here instead (or as well, if the harness mandates a local
write).

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

## Linking Memories to Code Symbols

When a memory is about a specific function or class, attach the code-graph
**symbol id** so it can be found from the code later. Get the id from the
`witan-code` tools — `code_find_definition` / `code_search_symbol`
return it in the `slug` field, of the form `repo#path::Qualified.Name`:

```
memory_store(
    kind="lesson",
    title="Service.run must not be called before init",
    content="...",
    symbol_refs=["https://github.com/mitodl/ol-django#app/svc.py::Service.run"],
)
```

To go the other way — "what lessons or tasks concern this symbol?" — call
`context_for_symbol(symbol_id)` before editing it. It returns the memories and
tasks whose `symbol_refs` include that id. (Requires the witan-code
server; symbol ids are soft references, so a stale one simply resolves to nothing.)

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
