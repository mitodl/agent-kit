# Graph-structured memory for richer contextualized recall — Discovery

Project: `wp-graph-structured-memory-for-richer-contextualize-b95b0b`
Phase: discovery → spec
Status: design/track only — nothing here is implemented yet.

## 1. Goal

Evolve witan memory from flat `Memory` nodes (BM25 over `content`, soft
`symbol_refs`, free-string `tags`) into a connected knowledge graph so recall is
*contextual and traversable*, not just keyword search. Six directions were
proposed; this document grounds each one in the actual code and records the
feasibility, the mechanism, and the open decisions that the spec phase will turn
into a design.

## 2. Current state (as built)

- **Single Layer-1 store.** `witan/server.py` instantiates exactly one
  `OmnigraphClient(cfg.graph_uri, …)` over the memory/workflow/task store and
  never opens any other store. (`server.py:52`)
- **Schema** (`schema/schema.pg`): one `Memory` node with a `kind` discriminator
  and BM25 `@index` on `content`. Two inter-memory edges already exist —
  `Supersedes` and `AppliesTo` — plus the workflow/task subgraph
  (`WorkflowProject`/`Session`/`Trace`, `Task`, and their edges incl. `Informed`,
  `Addresses`, `Closes`).
- **Edge mutations exist but are not exposed.** `mutations.gq` defines
  `link_supersedes` / `link_applies_to`, but **no MCP tool calls them** — this is
  the documented v2-roadmap gap (`docs/agent-memory.md:1438`). Edges can only be
  created via the raw CLI today.
- **Search is BM25-only.** `read.gq` search queries all end with
  `order { bm25($m.content, $query) desc } limit 20`. No recency, no confidence,
  no superseded-filtering, no traversal.
- **Code/SOA links are soft strings.** `memory_store(symbol_refs=[...])` stores
  `repo#path::Name` strings; `context_for_symbol` resolves them by *fetching
  candidate rows and filtering in Python* (`server.py:1557`) — it is not a graph
  traversal and crosses no store boundary.

### The central constraint — three separate stores, no cross-store edges

witan is three independent omnigraph (Lance) stores:

| Layer | Store | Owner | Contents |
|---|---|---|---|
| 1 | `graph_uri` (memory) | `witan` server | Memory, Workflow*, Task + their edges |
| 2 | per-repo code store | `witan-code` | `CodeFile`, `Symbol`, Calls/References/… |
| 2.5 | `_bridge.omni` | `witan-code` | flat `InterfaceBinding` (env_var/endpoint/package/service) |

omnigraph edges are **intra-store only**, and the bridge store is *deliberately
edge-free* — flat `InterfaceBinding` rows grouped on `(kind, key_norm)`, with an
explicit comment that anchor-node+edge modelling was rejected to avoid write
contention among many concurrent indexers (`bridge-schema.pg:1-16`).

**Consequence that reshapes the task list:**

- Inter-memory edges (direction 1) have *both endpoints in Layer 1* → real
  omnigraph edges, fully feasible.
- "Hard memory↔symbol / memory↔contract-anchor edges" (direction 2) would span
  store boundaries → **cannot be omnigraph edges as literally stated.** They must
  be either (a) soft refs resolved by cross-store fan-out, or (b) promoted to
  *Layer-1 proxy nodes* (Topics/Anchors) that carry the join key, with real
  Layer-1 edges. See §3.2.

## 3. Direction-by-direction findings

### 3.1 Typed inter-memory edges + Supersedes — `tk-typed-inter-memory-edges` (p1)

**Feasible, lowest-risk, highest-leverage. Start here.**

- Add `Refines`, `Contradicts`, `RelatedTo` edges (Memory→Memory) alongside the
  existing `Supersedes`/`AppliesTo`.
- Add `link_refines` / `link_contradicts` / `link_related_to` mutations and a
  single `memory_link(from_slug, to_slug, kind)` MCP tool (kind enum). This also
  closes the existing `link_supersedes`/`link_applies_to` exposure gap.
- Traversal syntax is confirmed available — `witan-code` already does
  `$caller calls $target` predicate joins (`code_read.gq:93`), so
  `$new supersedes $old` queries are supported.
- **Superseded handling.** The v1 `search()` queries are defined to order by
  `bm25` only (the engine's `order` clause can sort by node fields — the listing
  queries use `order { $m.created_at desc }` — but the BM25 search path doesn't),
  so the clean
  approach is: run the BM25 search, separately fetch the set of slugs that are the
  *target* of a `Supersedes` edge, then drop/deprioritise them in Python before
  returning. Contradictions are *not* hidden — they are surfaced ("memory X
  contradicts memory Y, review").
- **Decision needed (spec):** is `RelatedTo` symmetric? Recommend storing one
  direction and traversing both in reads (avoids double-write contention).

**Edges are typed by endpoint node type — names are not reusable across types.**
Every edge in the schema is declared with one fixed type-pair (`Supersedes`/
`AppliesTo` are `Memory -> Memory`; `schema.pg:34,38`) and no name is reused. The
`insert <Edge> { from, to }` mutation takes raw slug strings so it may not *reject*
an off-type slug at write time, but every read binds its endpoints to a concrete
type (`$m: Memory … $m supersedes $other`), so a `Task`/`WorkflowProject` endpoint
would never come back from a traversal — a dead, untraversable edge. **These edges
therefore cannot carry task/project relations.** If task- or project-level
supersession is wanted, declare *new* typed edges per type-pair, matching the
existing convention where the task and project worlds already use distinct names
rather than overloading one (task `Blocks` vs project `ProjectBlocks`; task→memory
`Addresses` vs project→memory `Informed`):
- `TaskSupersedes: Task -> Task`, `ProjectSupersedes: WorkflowProject -> WorkflowProject`.
- Do **not** assume omnigraph allows declaring one edge name for multiple
  type-pairs (overloading) — the schema deliberately never does this; use distinct
  names.
- Before adding any of these, note the partial coverage that already exists:
  task→memory context is `Addresses: Task -> Memory`; task provenance/hierarchy is
  `DiscoveredFrom`/`ParentOf`. A "task X obsoletes task Y" need is often met more
  cheaply by closing the old task with a `resolution` pointing at the new slug than
  by adding a new edge type.

### 3.2 Hard memory↔symbol & memory↔contract-anchor edges — `tk-hard-memory-symbol-...` (p2)

**Reframe required** — true cross-store hard edges are impossible (§2). Two
mechanisms, recommend a hybrid:

- **Symbols → keep/strengthen soft refs.** `symbol_refs` already exist;
  `context_for_symbol` already does the reverse lookup. Low-cost win: add the same
  for the *forward* direction and expose it, rather than inventing an edge that the
  engine can't store.
- **Contracts → Layer-1 anchor via Topic nodes (§3.4).** Contract keys
  (`env_var`/`endpoint`/`package`/`service` `key_norm`) are *low-cardinality,
  shared join keys* — ideal as real Layer-1 nodes. Model a `Topic` of kind
  `contract` whose `name == bridge key_norm`; link memories to it with a real
  Layer-1 `Tagged` edge. "What do we know about endpoint `/api/v1/courses/`"
  becomes one Layer-1 traversal, and the topic's name still joins to the bridge's
  `key_norm` / `code_interface_consumers` at read time for the code side.
- **Decision needed:** add `contract_refs: [String]?` to `Memory` as a soft-ref
  carrier (mirrors `symbol_refs`), or rely solely on `Topic` anchors. Recommend
  Topic anchors (traversable) over a second soft-ref list (not traversable).

### 3.3 Provenance edges: sessions/traces produced memories — `tk-provenance-edges` (p2)

- `Informed: WorkflowProject -> Memory` already exists (via
  `workflow_project_link_memory`) — that covers "project consulted/produced this
  memory" at project granularity.
- Missing piece is **session-granular** provenance. Add a new edge (e.g.
  `SessionProduced: WorkflowSession -> Memory`; note the bare name `Produced` is
  already taken by `WorkflowProject -> WorkflowTrace`). Auto-create it in
  `memory_store` when a session is active — the session state file in `/tmp`
  written by `workflow_session_start` already makes the active session
  discoverable.
- Enables "what did we learn during project X" as project → sessions → memories,
  and feeds richer `WorkflowTrace` corpus records for pattern mining.

### 3.4 Topic/Entity nodes + traversal-based retrieval — `tk-topic-entity-nodes` (p2)

- Add a `Topic` node (`slug`, `name`, `kind ∈ {topic, contract, symbol, entity}`)
  and `Tagged: Memory -> Topic`. Promote today's free-string `tags` into Topic
  nodes (keep the string list for back-compat; reconcile in a migration).
- Topics are the join surface for cross-repo propagation: two memories in
  different repos that share a `package` topic become one hop apart, so "what does
  the org know about `cryptography`" spans repos without BM25 ever matching.
- Topics also serve as the **contract anchor** from §3.2 — one node type, two uses.

### 3.5 Corroboration, confidence & recency decay — `tk-corroboration-confidence-...` (p2)

- Add `confidence: F32?` to `Memory`. Ranking moves to a **Python re-rank over the
  BM25 candidate set** (the engine can't express the composite in `order`):
  `score = w_bm25·bm25 + w_recency·decay(updated_at) + w_corrob·corroboration −
  penalty(superseded|contradicted)`, with `decay = exp(-age / half_life)` and
  `corroboration` = count of supporting edges (`AppliesTo`/`RelatedTo`/`Informed`/
  `SessionProduced`).
- This direction *depends on* 3.1 (superseded/contradicted edges) and 3.3/3.4
  (edges to count) to have signal — so it sequences after them.
- **Decision needed:** weights and half-life as config knobs vs. fixed defaults.

### 3.6 Graph-aware retrieval API + optional embeddings — `tk-graph-aware-retrieval-api` (p2)

The capstone — assembles everything above into one `recall(...)` tool:

1. **Seed** from any of `query` (BM25), `symbol_id`/`task`/`topic` (anchor match).
2. **Expand** 1–2 hops along `AppliesTo`/`RelatedTo`/`Tagged`/`SessionProduced`.
3. **Prune** superseded; flag contradictions.
4. **Re-rank** with the §3.5 composite score.
5. **Optional embeddings** for semantic neighbours beyond BM25 — the upgrade path
   is already specced in `docs/agent-memory.md:1436`: add `Vector(1536)
   @embed("content")` to `Memory`, run `omnigraph embed`, switch search to
   `rrf(bm25(...), nearest(...))`. Keep BM25-only as the default; embeddings are a
   config-gated enhancement so the retrieval path degrades cleanly without a
   provider.

## 4. Recommended sequencing

```
3.1 typed edges (p1)  ──┬─→ 3.3 provenance ─┐
                        ├─→ 3.4 topics/anchors ─┼─→ 3.5 ranking ─→ 3.6 recall API
                        └─→ 3.2 symbol/contract links ─┘
```

3.1 is the foundation (edges to traverse and to hide superseded). 3.3/3.4 add more
edge types and the topic join surface. 3.2 rides on 3.4's Topic anchors. 3.5 needs
edges to score. 3.6 composes all of them; embeddings are an independent, gated
add-on.

## 5. Cross-cutting constraints carried into spec

1. **No cross-store edges.** Anything linking memory to code/bridge is soft refs
   or Layer-1 proxy (Topic) nodes — never an omnigraph edge into Layer 2/2.5.
2. **Composite ranking lives in Python, not `order`.** The v1 search queries are
   defined to order by `bm25` only (the engine's `order` clause *can* sort by node
   fields — listing queries use `order { $m.created_at desc }`), and the composite
   score can't be expressed as one `order` clause → composite ranking and
   superseded-pruning happen as a Python re-rank over the candidate set.
3. **Write contention is real.** The store uses optimistic concurrency with a
   per-store advisory lock + repair retries (`graph.py:9-18`). Favour low-fanout
   writes; do not require every repo's indexer to upsert shared anchor rows (the
   reason the bridge is edge-free). Topic nodes are written by the memory path
   (low volume), not by indexers — so they are safe.
4. **Back-compat.** `tags`/`symbol_refs` stay; new structures are additive with a
   migration that backfills Topic nodes from existing tags.

## 6. Open questions for spec

- `RelatedTo` symmetry and storage direction.
- `contract_refs` soft list vs. Topic-anchor-only for contracts.
- Ranking weights/half-life: config vs. constants.
- Embedding provider + whether `rrf` hybrid is in-scope for the first cut or
  deferred behind the existing v2-roadmap line.
- Migration story for promoting free-string `tags` into `Topic` nodes.
