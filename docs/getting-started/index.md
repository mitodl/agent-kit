# Get started

Four short pages. By the end you will have witan installed and wired into your
agent, one memory stored and recalled, one task claimed and closed, and one
repository indexed into the code graph.

They are meant to be read in order — each builds on the last — and the whole
sequence takes about twenty minutes.

<div class="grid cards" markdown>

-   **1. [Installation](installation.md)**

    Install `ol-agent-kit`, run `witan setup`, and confirm your agent can see
    the tools.

-   **2. [Your first memory](first-memory.md)**

    Store something worth keeping, then get it back — and see why `recall`
    returns more than a text search would.

-   **3. [Tasks and projects](tasks-and-projects.md)**

    File work, claim it, and understand what a claim actually guarantees when
    two agents want the same task.

-   **4. [Indexing a repository](code-graph.md)**

    Build the code graph and ask it questions grep cannot answer.

</div>

## Before you begin

You will need:

- **Python 3.11 or newer**, and [`uv`](https://docs.astral.sh/uv/). Every
  install path here uses `uv`; nothing is installed with system `pip`.
- **A git repository to work in.** witan scopes almost everything by repo,
  detected from `origin` in `.git/config`. It works outside a repo, but the
  defaults make much less sense.
- **A coding agent** — Claude Code, Pi, GitHub Copilot, OpenCode, or Kilo.
  `witan setup` registers itself with whichever it finds.

You do **not** need a server, a database, or any credentials. The default store
is a single file at `~/.local/share/witan/graph.omni`, and everything in this
tutorial runs against it locally. Pointing witan at a shared, deployed service
is a later, separate step — see [Using a deployed
witan](../guides/deployed-witan.md).

## A note on where things run

witan has two faces over the same graph, and both appear throughout these pages:

- **The `witan` CLI** — what *you* type. Good for browsing, triage, and
  operations.
- **The MCP tools** — what your *agent* calls. Where both offer an operation
  they share an implementation: the CLI calls the very same functions the MCP
  server exposes, so they cannot disagree about what the graph says.

When a page shows `witan tasks --ready` and then mentions `task_ready`, those
are the same operation from the two sides.

**The surfaces are not equivalent, though.** Some things are deliberately
MCP-only, because the intended caller is an agent rather than a person:
`memory_store` has no CLI equivalent (the CLI reads memory, it does not write
it), and the code-graph queries are tools only — the `witan code` CLI builds
and operates the index rather than querying it.
