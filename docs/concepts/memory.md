# Memory and its four kinds

A memory is one durable fact worth keeping past the end of the session that
learned it — not a transcript, not a to-do, a thing that will still be true
and still useful later. It is the unit witan is built around: everything else
(tasks, projects, code symbols) exists so memories can be attached to it.

## Why "kind" instead of one big note pile

Every memory carries a `kind`, and that single field decides *when* it should
resurface:

| Kind | Answers | Read when |
| --- | --- | --- |
| `pattern` | "How do we do X here?" | About to write similar code |
| `project_fact` | "What is true about this repo/service?" | Orienting in an unfamiliar codebase |
| `lesson` | "What went wrong last time?" | Something has broken, or is about to |
| `agent_context` | "What should the next session on *this task* know?" | Picking up someone else's in-flight work |

A note with no kind gets read at none of those moments — nobody's query
happens to land on it. Picking a kind is what makes a memory findable by the
person who actually needs it, rather than only by someone who already knew it
was there.

`agent_context` is the odd one out: it is scoped to a specific piece of
work rather than to a repo or topic in general, and it is the only kind
meant to age out once that work is done.

## Memories are a graph, not a table

A pile of typed notes is still just a search index. What makes it a *graph*
is that memories point at each other, with the edge meaning something:

| Edge | Meaning | What happens on read |
| --- | --- | --- |
| `supersedes` | This replaces that | The old one stops appearing by default |
| `refines` | This sharpens that, without replacing it | Both appear; the newer one ranks higher |
| `applies_to` | This pattern/lesson applies in that project's context | Following it pulls in the context |
| `contradicts` | These two disagree | **Both** appear, flagged — nothing is auto-resolved |
| `tagged` | This memory is about that topic | Everything else tagged the same way expands with it |

The one to internalize first is **superseding is not deleting**. When
something you knew changes, you don't edit the old memory in place — you store
a new one and link it `supersedes` the old. The old memory is hidden from
normal reads but never destroyed, so "why did we think that, before?" always
has an answer. Editing in place would erase the fact that the knowledge ever
changed.

Two memories that genuinely conflict are never silently resolved either — both
keep surfacing, flagged, because a heuristic guessing which one is "right" is
usually wrong about exactly the cases that matter.

The full rationale for that design — including when to `memory_update`
instead of superseding — lives in [The memory
model](../explanation/memory-model.md).

## Topics: how unrelated memories end up connected

A free-text tag like `"vault"` on two different memories doesn't connect them
— they share a string, not a link. So a tag is promoted to a `Topic` node the
first time it's used, and `tagged` becomes a real, traversable edge. Two
memories tagged `vault` are then one hop apart in the graph, and asking "what
do we know about vault?" is a graph query, not a grep.

One topic kind is worth calling out: a `contract` topic's name is a bridge
key — an environment variable, an HTTP endpoint, a package. That's the join
between the memory graph and the [code graph](../getting-started/code-graph.md):
`memory_for_contract("DATABASE_URL")` returns both what's been *written down*
about that env var and what code *actually provides or consumes it*.

## How you actually read this back

You will almost never call a narrow "get me memories of kind X" query
yourself. The default read is `recall` — seed it with a search query, a task
slug, a code symbol, or a topic, and it does the graph expansion, drops
superseded memories, flags contradictions, and re-ranks the result for you.
See [`recall`](../reference/mcp-tools/memory.md#recall) for the full call
shape, or [Your first memory](../getting-started/first-memory.md) to store and
recall one in about five minutes.

---

**Next:** [The task and project graph →](graph.md)
