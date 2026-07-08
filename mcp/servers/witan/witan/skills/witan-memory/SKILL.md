---
name: witan-memory
description: >
  Read from and write to the team's shared agent memory graph. Prefer this over
  your private built-in/session memory for any durable, team-shareable knowledge.
  Use when starting work in a repository (load project facts and patterns), after
  solving a non-obvious problem (store a pattern), when discovering structural
  information about a codebase (store a project fact), when a correction was
  needed (store a lesson), or whenever you would otherwise save an engineering
  fact/lesson/decision to local session memory. Requires the witan MCP server to
  be configured.
license: BSD-3-Clause
metadata:
  category: workflow
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

### `recall` — load context (the default read)

Call this **first** whenever you start work in a repository or need to know what
the team has learned about something. `recall` composes every other memory read
— BM25 search, graph expansion along typed edges, topic and provenance siblings,
superseded-pruning, and re-ranking — and degrades to a plain search when the
graph has no edges yet.

```python
recall(query="vault secrets injection")        # by topic
recall(query="rate limiting", kind="pattern")   # filter by kind
recall(symbol_id="…::Service.run")              # seed from a code symbol
recall(task="tk-…")                             # seed from a task
recall(topic="uv")                              # seed from a topic
```

Seed with any combination of `query`, `symbol_id`, `task`, and `topic`; widen
with `hops=2`. Reach for a narrower read only for a specific need:

| Want | Call |
|------|------|
| Contextual load / "what do we know about X" | `recall` (default) |
| One memory by slug | `memory_get` |
| Browse all of one kind (no query) | `memory_list(kind=…)` |
| Plain BM25, no graph expansion | `memory_search` |
| A known memory's neighbours, by edge kind | `memory_neighbors` |
| Everything tagged to a topic (cross-repo) | `topic_get` |
| Memories + code for a contract key | `memory_for_contract` |
| Memories/tasks for a code symbol | `symbol_context` |
| The code symbols a memory concerns | `memory_symbols` |
| Memories a project produced | `workflow_project_memories` |

### `memory_list` — browse a kind without a query

```python
memory_list(kind="project_fact")                # structural facts, this repo
memory_list(kind="pattern", language="python")  # conventions before coding
```

Scoping: auto-detects the current repo; pass `repo=""` for all repos. With no
repo detected and none passed it returns slim records (slug, kind, title, tags —
no content); `memory_get` a slug for its full content.

### `memory_store` — record something worth remembering

**Store a `pattern`** after solving a problem in a non-obvious way, or when
you apply a team convention that should be made explicit:

```python
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

```python
memory_store(
    kind="project_fact",
    title="Vault secrets injected via env at runtime",
    content="This service reads secrets from Vault at startup via the ...",
    category="deployment"
)
```

**Store a `lesson`** when a mistake was made or a correction was needed:

```python
memory_store(
    kind="lesson",
    title="Do not run migrations without a backup in staging",
    content="On 2025-05-10, a migration was run without a prior snapshot ...",
    severity="warning"
)
```

**Store `agent_context`** when handing off a task or leaving breadcrumbs
for a future agent session:

```python
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
`witan-code` tools — `code_find_definition` / `code_search_symbol` return it in
the `symbol_id` field, of the form `<repo>#<path/to/file.py>::<QualifiedName>`:

```python
memory_store(
    kind="lesson",
    title="Service.run must not be called before init",
    content="...",
    symbol_refs=["https://github.com/mitodl/ol-django#app/svc.py::Service.run"],
)
```

To go the other way — "what lessons or tasks concern this symbol?" — call
`symbol_context(symbol_id)` before editing it. It returns the memories and tasks
whose `symbol_refs` include that id. The forward direction — "what code does this
memory concern?" — is `memory_symbols(slug)`. (Both require the witan-code server;
symbol ids are soft references, so a stale one simply resolves to nothing.)

## Quality Guidelines

- **Be specific.** Vague memories degrade search quality. Include the what,
  why, and any relevant examples in `content`.
- **One idea per memory.** Split broad topics. "uv for packaging" and "pytest
  config conventions" are two memories, not one.
- **Don't store transient state.** Session-specific observations that won't
  be useful after the current task ends don't belong in the graph.
- **Check before storing.** Run `recall` first. If a similar memory already
  exists, consider whether to link or supersede it instead of creating a
  duplicate.
- **Set `confidence`** (0.0–1.0) when you have a strong or weak signal about how
  much to trust a memory — it feeds the recall re-rank. Omit it for the default.

## Linking & Organizing Memories

Memories are a graph. Connect them with `memory_link(from_slug, to_slug, kind)`:

- `supersedes` — `from` (newer) replaces `to` (older); `to` is hidden from
  default reads.
- `refines` — `from` sharpens `to` without replacing it.
- `applies_to` — a pattern/lesson (`from`) applies in the context of a
  project_fact (`to`).
- `contradicts` — the two conflict; surfaced for review, never hidden.
- `related_to` — soft association.
- `tagged` — `from` is about a Topic (`to` is a `tp-…` slug or a `name:kind`
  spec like `uv:topic` or `DATABASE_URL:contract`, auto-created).

Tags you pass to `memory_store` are also dual-written as `topic` Topics, so two
memories sharing a tag are one hop apart. Explore the graph with
`memory_neighbors(slug)` (a memory's links by kind) and `topic_get("uv:topic")`
(everything tagged to a topic, across repos).

## Updating an Existing Memory

To correct a simply-wrong memory, `memory_store` the fixed version. To replace an
outdated one, store the new version then link it:

```python
new = memory_store(kind="lesson", title="…", content="…")
memory_link(from_slug=new["slug"], to_slug="les-old-…", kind="supersedes")
```

The old memory is hidden from default `memory_search`/`recall` results but
preserved — pass `include_superseded=True` to see it.
