# Your first memory

A memory is one durable fact worth keeping past the end of a session. Not a
summary of what you did — a thing that will still be true, and still useful, in
three months.

## The four kinds

Every memory has a `kind`, and picking the right one is most of what makes
recall useful later:

| Kind | What it holds | Example |
| --- | --- | --- |
| `pattern` | A reusable technique or convention | "Use `uv`, never system `pip`, for tooling" |
| `project_fact` | A structural fact about a repo or service | "`ol-django` reads Vault secrets through `hvac`, role from env" |
| `lesson` | A correction, or something that bit you | "`perl -pi` with `\x{…}` re-encodes the whole file's non-ASCII" |
| `agent_context` | What a future agent on *this* task should know | "The retry test is flaky above 8 workers; run it serially" |

The split matters because reads filter on it. Someone asking "how do we do X
here" wants patterns; someone debugging wants lessons.

## Write one

**Writes go through the MCP tools, not the CLI.** The `witan` CLI reads the
graph — it deliberately has no `memory store` command, because the intended
author of a memory is the agent that just learned the thing.

So ask your agent, in whatever session you are already in:

> Store a memory: the pattern that this repo's tests must run through
> `just test-<package>`, because the uv workspace shares one `.venv` and running
> pytest directly cross-contaminates sibling packages.

It will call:

```python
memory_store(
    kind="pattern",
    title="Run package tests through `just test-<package>`",
    content="The uv workspace shares one .venv, so running pytest directly ...",
    tags=["testing", "uv"],
)
```

and hand back a slug like `pat-run-package-tests-through-just-a1b2c3`. The
`repo` is filled in automatically from your checkout.

!!! tip "What makes a memory worth storing"

    Ask whether it would have saved you the last hour. Facts the repository
    already records — its structure, its git history, what a function does —
    are not worth storing; anyone can read those. What is worth storing is the
    thing that was *non-obvious*: why an approach was rejected, which invariant
    is load-bearing, what looked correct and was not.

## Read it back

```bash
witan memory "test isolation"
```

```
        Memory search: 'test isolation'
┏━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ kind    ┃ slug                   ┃ title                   ┃
┡━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ pattern │ pat-run-package-tes... │ Run package tests thr...│
└─────────┴────────────────────────┴─────────────────────────┘
```

With no query, `witan memory` lists instead of searching, and `--kind` filters
either mode:

```bash
witan memory --kind lesson          # every lesson in this repo
witan memory "vault" --all-repos    # search across all repos
```

## Why `recall` beats search

`witan memory` runs a BM25 text search. That finds documents containing your
words. It does not know that one memory replaced another, that two memories
contradict each other, or that the *really* relevant fact is one hop away and
shares none of your vocabulary.

[`recall`](../reference/mcp-tools/memory.md#recall) is the tool your agent
should reach for instead. In one call it:

1. **Seeds** from any combination of a text query, a code symbol, a task, or a
   topic.
2. **Expands** one hop (up to two) across `applies_to` / `related_to` edges,
   topic siblings, and provenance siblings.
3. **Prunes** memories that something else supersedes, so you get the current
   version rather than the history.
4. **Flags** contradictions rather than hiding them — a disagreement is
   surfaced for a human to resolve.
5. **Re-ranks** everything by a composite of text relevance, recency,
   corroboration, and author confidence, with a per-hop penalty so direct hits
   still outrank neighbours.

With no edges in the graph, `recall` returns exactly what `memory_search` would
— expansion is additive, never lossy. So it is always the right default; it just
gets better as the graph fills in.

## Linking memories

Edges are what turn a pile of notes into a graph. The one to learn first is
`supersedes`:

```python
memory_link(from_slug="<the new one>", to_slug="<the old one>", kind="supersedes")
```

After that link, the old memory stops appearing in default reads — but it is
**not deleted**. It stays in the graph, retrievable with
`include_superseded=True`, so the history of a decision survives.

This is the correct way to change knowledge that has *changed*. Use
`memory_update` only when a memory was simply *wrong* — a typo'd title, the
wrong repo. The distinction matters: updating destroys the old version,
superseding keeps it.

The other edge kinds — `refines`, `applies_to`, `contradicts`, `related_to` —
are covered in [The memory model](../explanation/memory-model.md).

---

**Next:** [Tasks and projects →](tasks-and-projects.md)
