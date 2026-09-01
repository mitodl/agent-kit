# Glossary

witan's documentation draws on several vocabularies at once — the workflow
graph's own terms, the shape of the deployed service, and the concurrency
language the storage layer forces on anyone reading an error message. This page
defines them in one place, so a reader who has not followed the project can
still follow the docs.

Terms are grouped by where you meet them.

---

## Project tracking

**Phase** — a `WorkflowProject` moves through exactly four phases:
`discovery` → `spec` → `implementation` → `delivery`. The value is a schema
enum, not a convention, so nothing else is a valid phase. Transitions happen
only through `workflow_project_advance`.

**Phase-exit criteria** — the conditions a project writes down as "what would
convince us this phase is actually done", to be met before it advances. This is
a discipline the project imposes on itself rather than something the tooling
enforces: the graph will let you advance whenever you like, and the point of
recording criteria is to make advancing without meeting them a visible choice
instead of a quiet one.

**Slug prefixes** — every node's slug names its kind, followed by a short
uniqueness suffix.

| Prefix | Node |
| --- | --- |
| `pat-` | Memory — pattern |
| `pf-` | Memory — project fact |
| `les-` | Memory — lesson |
| `ctx-` | Memory — agent context |
| `tk-` | Task |
| `wp-` | WorkflowProject |
| `ws-` | WorkflowSession |
| `wt-` | WorkflowTrace |

So `les-` and `pat-` are both memories; the prefix tells you which of the
[four kinds](concepts/memory.md) it is without a lookup.

**Session** — one agent working stint, registered with
`workflow_session_start` and linked to a project. A session is what gives a
memory or a task its provenance edge; several sessions may be open on one
project at once, which is the case the coordination primitives exist for.

---

## Identity and authorization

**Actor** — the identity a call is attributed to. Against a deployed witan it
is derived per request from the caller's own JWT, so writes carry the person
who made them rather than the service's own credential. Actor ids are of the form
`act-<id>`, mapped from the token's `sub`.

**Cedar bundle** — the policy that decides whether an actor may read or write a
given graph. It is evaluated at the data tier, so it applies to every client
rather than only the ones that go through the MCP server.

---

## Concurrency and storage

These come up in error messages, so they are worth knowing even if you never
read the storage code.

**CAS** — compare-and-swap. A write that applies only if the thing it read has
not changed since. `task_claim` uses one so that when two agents claim the same
task simultaneously, exactly one wins and the other is told it lost, rather
than the second silently overwriting the first.

**OCC** — optimistic concurrency control. Let writers proceed without locking
and detect the collision at commit time, instead of serialising them up front.

**Graph commit id** — the identifier of the snapshot a read was served from,
and the precondition a CAS write states. They are
[ULIDs](https://github.com/ulid/spec), so they sort lexicographically in
creation order — which is what makes "is this snapshot at least as new as my
write?" a string comparison.

**Advisory lock** — a filesystem lock (`flock`) that serialises writers sharing
a local store. It is deliberately **skipped for any store it cannot
cover** — `http://`, `https://` and `s3://` — because a filesystem lock cannot
coordinate writers that do not share a filesystem. A deployed witan therefore
gets its mutual exclusion from CAS, not from this.

**Write-admission gate** — a client-side bound on how many writes witan will
have in flight against one graph at a time (default 4). It exists because the
data tier's own cap is *per actor*, and a per-actor cap cannot bound a shared
service: ten users at one write each is ten writes in flight with every
per-actor cap satisfied.

The number is measured rather than chosen. Writes against the graph are
strictly serialised and get *worse per write* as writers are added — throughput
falls as concurrency rises, because a single-row insert is a full commit cycle
against object storage plus a read of the whole table. Past a certain width,
admitting another writer does not make it finish sooner; it makes it time out.
So when a burst is refused, the refusal happens **before anything is sent**,
which is the safe direction: nothing was written, and retrying once the burst
clears is the remedy.

---

## Deployment

**Data tier** — `omnigraph-server`, which owns the graph and its object
storage. It is reachable only inside the cluster, which is why an ordinary user
cannot export the deployed graph directly.

**MCP tier** — the `witan` server your agent talks to. It authenticates the
caller, resolves their actor, and makes the calls to the data tier on their
behalf.

**APISIX** — the ingress in front of the MCP tier. A `502` or `503` on a witan
call is usually APISIX reporting that it could not reach or could not wait for
the backend, not witan reporting a result.

!!! note "A note on ToolHive and vMCP"

    Older material describes witan being served through a ToolHive-managed
    vMCP aggregator. That tier was removed: witan is now served as a plain
    Deployment behind APISIX in every environment. Mentions of `vMCP`, the
    proxy runner, or a ToolHive sidecar in the architecture notes and ADRs are
    historical — they explain why the tier is gone, not how requests are
    served today.

---

## Storage vocabulary you may meet in errors

**Store** — one graph's on-disk or object-storage home. A *local* store is a
`.omni` directory on your machine; the deployed store lives in object storage
behind the data tier.

**Branch** — an independent line of a graph's history. Code graphs use them to
keep one developer's in-progress reindex from disturbing the shared view.

**Merge** — reconciling one store's contents into another, as when a personal
store is migrated into the shared graph. Reconciliation is by `(type, slug)`,
and it is idempotent: re-running a merge that partly failed writes only what is
missing.
