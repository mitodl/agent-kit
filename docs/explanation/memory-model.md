# The memory model

## One node type, four kinds

Every memory is the same node type with a `kind` discriminator: `pattern`,
`project_fact`, `lesson`, `agent_context`. A few fields are populated only for
the relevant kind — `language` for patterns, `category` for project facts,
`severity` for lessons.

One node type rather than four keeps cross-kind search simple: a single BM25
index over `content` serves every query, and a read that wants only lessons adds
a filter rather than a different code path.

The kinds themselves are not decoration. They encode *when* something should
resurface:

- A **pattern** is read when you are about to write similar code.
- A **project fact** is read when you are orienting in an unfamiliar repo.
- A **lesson** is read when something has gone wrong, or is about to.
- **Agent context** is read by whoever picks up this specific task next, and is
  the only kind with a natural expiry.

An untyped note is read at none of those moments, which is the practical
argument for making the author choose.

## Slugs are readable on purpose

```
pat-always-use-uv
pf-ol-django-vault-secrets
les-no-raw-sql-in-views
ctx-ticket-1234-approach
```

A slug is derived from the title with a kind prefix. It is stable, human-typable
in a CLI, and identifies the kind at a glance — which matters because slugs
appear in edges, in task links, and in hook output where there is no room for
more.

## Edges are the point

A pile of notes is a search index. What makes this a graph is that memories
relate to each other, and five edge kinds carry those relationships:

| Edge | Meaning | Effect on reads |
| --- | --- | --- |
| `supersedes` | This replaces that | The superseded one is hidden by default |
| `refines` | This sharpens that, without replacing it | Both surface; this one ranks higher |
| `applies_to` | This pattern/lesson applies in that context | Expansion follows it |
| `contradicts` | These two disagree | **Both** surface, flagged for a human |
| `tagged` | This memory is about that topic | Topic siblings expand together |

### Superseding is not deleting

This is the design decision that everything else about reads follows from.

When knowledge changes, you store the new memory and link
`new --supersedes--> old`. The old memory stops appearing in default reads. It
is **not removed** — `include_superseded=True` still returns it.

The alternative — editing the old memory in place — destroys the record that the
knowledge ever changed, and with it the answer to "why did we think that?" A
store that cannot distinguish *wrong* from *no longer true* forces a choice
between losing history and serving stale facts. Superseding refuses the choice.

So the rule is:

- **The knowledge changed** → store new, link `supersedes`.
- **The record was wrong** — typo, wrong repo, bad tag → `memory_update`.
- **It should never have existed** → `memory_delete`.

### Contradictions are surfaced, never resolved

Two memories that disagree both keep appearing, flagged. witan does not pick a
winner and does not hide either.

That is deliberate. A contradiction usually means a genuine disagreement between
two people, or a fact that changed without anyone superseding the old one — both
of which need a human, and neither of which is improved by an automatic
heuristic silently choosing. The mild ranking penalty
([`WITAN_RANK_PEN_CONTRADICTED`](../reference/environment.md#recall-ranking),
0.25) nudges them down without burying them.

## Topics: a join surface

Free-string tags do not connect anything — two memories tagged `vault` share a
string, not an edge. So tags are promoted to `Topic` nodes, and `tagged` is a
real traversable edge.

Topics come in kinds: `topic` (promoted from a tag), `contract` (whose name is a
bridge key — an env var, endpoint, package, or service), `entity` (a named
service, library, or concept), and `symbol` (reserved; symbols stay soft refs
for now).

The `contract` kind is the interesting one: it is the join between the memory
graph and the code graph. `memory_for_contract("DATABASE_URL")` returns both the
memories tagged to that contract and the code that provides or consumes it.

## How `recall` composes all of it

`recall` exists because doing this well requires five steps, and no agent should
have to remember to run them in order.

1. **Seed** from any combination of `query` (BM25), `symbol_id`, `task`, or
   `topic`. Multiple seeds are a union, which is what makes "what do we know
   about this task" a single call.
2. **Expand** one hop — capped at two — across `applies_to` / `related_to`
   edges, topic siblings, and provenance siblings (memories produced by the same
   session or project).
3. **Prune** superseded memories.
4. **Flag** contradiction pairs.
5. **Re-rank** by a composite score, minus a per-hop distance penalty so seeds
   outrank the neighbours they pulled in.

The composite is BM25 relevance, recency (90-day half-life), corroboration, and
author confidence — each separately weighted and all
[tunable](../reference/environment.md#recall-ranking). Setting every weight to
zero reproduces raw BM25 order, which is the useful thing to do when you suspect
ranking is hiding something.

**With no edges in the graph, `recall` returns exactly what `memory_search`
would.** Expansion is additive, never lossy. That property is what makes it
safe as the default read from day one, on an empty graph, before anyone has
linked anything.

## Provenance

Memories are attributed — an author, a timestamp, and edges back to the session
and project that produced them. `workflow_project_memories` asks what a project
learned; `SessionProduced` edges make "what came out of this session" a
one-hop query.

Provenance is also a ranking input: memories that emerged from the same piece of
work are treated as siblings during expansion, on the theory that things learned
together are usually relevant together.

## Repo scoping

Almost everything is scoped by repo, detected from `.git/config`. Pass `repo`
explicitly to override, or `repo=""` to operate across every repo in the store.

Scoping is a default rather than a boundary. A pattern learned in one repo is
often exactly what another needs, so cross-repo reads are one flag away — and
topics, contracts, and the bridge are cross-repo by construction.
