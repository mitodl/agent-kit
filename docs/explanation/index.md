# Explanation

Why witan is built the way it is. These pages are for understanding rather than
doing — read them when a design decision seems arbitrary, or when you are about
to work against the grain of one.

<div class="grid cards" markdown>

-   **[Architecture](architecture.md)**

    The three layers, the two tiers, and what actually happens when a tool is
    called.

-   **[The memory model](memory-model.md)**

    Why memories are typed, why they link, and why superseding is not deleting.

-   **[Coordinating work](task-coordination.md)**

    What a claim guarantees, what it does not, and why the honest answer is
    "best effort".

-   **[Code graph](code-graph/symbol-format.md)**

    Symbol identity, edge precision tiers, and how the cross-repo bridge is
    stitched together.

-   **[witan-core](witan-core.md)**

    The shared floor under both servers: what belongs there, why it is
    stdlib-only, and the version-floor trap the uv workspace hides.

</div>

## Decisions

The [ADR index](decisions/0001-write-path-content-scanning.md) records
architectural decisions with their context and consequences — including the ones
that turned out to constrain everything after them.

The ones worth reading first:

| ADR | Why it matters |
| --- | --- |
| [0001 Write-path content scanning](decisions/0001-write-path-content-scanning.md) | Why every write is scanned, and why it fails closed |
| [0003 Atomic task claims](decisions/0003-atomic-task-claims-cas.md) | The limits of coordination on a store with no conditional write |
| [0005 Secure CLI path into a deployed witan](decisions/0005-secure-cli-path-into-deployed-witan.md) | How the local CLI reaches a shared service |
| [0009 Stateless MCP protocol era](decisions/0009-stateless-mcp-protocol-era.md) | Why the server holds no session state |

## The idea underneath

Everything here follows from one observation: **a coding agent's context dies
with its session, and nothing about that is inevitable.**

The knowledge an agent builds up — why this approach and not that one, which
invariant is load-bearing, what already failed — is exactly the knowledge that
would make the *next* session good. Left in a transcript, it is gone. Left in a
per-agent memory file, it is invisible to your teammates and to the other agent
running in the next terminal.

So witan makes it a shared, typed, linked graph instead:

- **Shared**, because the unit that benefits is the team, not the session.
- **Typed**, because "pattern" and "lesson" get read at different moments, and
  an untyped note gets read at none of them.
- **Linked**, because knowledge changes, and a store that cannot say *this
  replaced that* forces you to choose between losing history and serving stale
  facts.

The work-coordination layer exists for the same reason one step out: once
several agents can act at once, they need a shared answer to "what is being
worked on" — and that answer has to live somewhere neither of them owns.
