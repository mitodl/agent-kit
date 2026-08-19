# Internals

Design documents and implementation specs, kept for the record.

!!! warning "These are point-in-time documents"

    Everything in this section describes what was intended *when it was
    written*. Some of it shipped as specified, some of it changed during
    implementation, and some describes work that was never done. None of it is
    maintained against the current code.

    For what witan does **now**, use [Reference](../reference/index.md) — those
    pages are generated from the source and verified in CI. For why it is shaped
    the way it is, use [Explanation](../explanation/index.md), and in particular
    the [ADRs](../explanation/decisions/0001-write-path-content-scanning.md),
    which *are* maintained.

They are published anyway because the reasoning in them is often the only record
of why an alternative was rejected — which is exactly what you want when you are
about to propose it again.

## What is here

| Document | Subject |
| --- | --- |
| [Agent memory implementation guide](agent-memory.md) | The original end-to-end build guide: schema, queries, server, install, operating modes |
| [Graph-structured memory](design/graph-structured-memory.md) | The case for typed edges over a flat note store |
| [Graph-structured memory (spec)](design/graph-structured-memory-spec.md) | The detailed specification that followed it |
| [witan-core extraction](design/witan-core-extraction-spec.md) | Splitting shared internals out of the two servers |
| [witan surface refinement](design/witan-surface-refinement-spec.md) | Consolidating the tool surface |
| [Workflow UX (P1)](design/witan-workflow-ux-p1-spec.md) | The project/session tracking experience |
| [Workflow hooks & elicitation](design/witan-workflow-hooks-elicitation-evaluation.md) | Evaluating hook-driven vs. elicited session linking |
| [Remote call overhead spike](design/omnigraph-remote-call-overhead-spike.md) | Measuring what a remote graph call actually costs |
| [agent-config-kit](design/agent-config-kit-spec.md) · [CLI](design/agent-config-kit-cli-spec.md) · [profiles](design/agent-config-kit-profiles-composition-spec.md) | The installer that registers witan with each agent platform |

## Contributing to the docs

The site is built with [Zensical](https://zensical.org) from `docs/` in the
[`mitodl/agent-kit`](https://github.com/mitodl/agent-kit) repository.

Three kinds of page, with different rules:

- **Generated** (`docs/reference/`) — produced by `bin/gen_docs.py` from the
  live code. Never edit these; change the source and re-run the generator.
- **Mirrored** (most of `docs/guides/`, the ADRs, the code-graph explanation
  pages) — copied from the package that owns them. Edit the file next to the
  code; the banner on each page links to it.
- **Handwritten** (the tutorials, the section overviews, the rest of
  `docs/explanation/`) — edit directly.

```bash
just docs-gen      # regenerate + re-mirror everything
just docs-check    # fail if any generated page is stale (what CI runs)
just docs-serve    # incremental local preview
```

`just docs-check` runs in CI, so a change to a tool signature, a CLI flag, or the
graph schema fails the build until the reference is regenerated and committed.
That is the mechanism keeping this site from becoming another point-in-time
document.
