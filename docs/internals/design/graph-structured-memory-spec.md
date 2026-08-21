# Graph-structured memory for richer contextualized recall — Spec

Project: `wp-graph-structured-memory-for-richer-contextualize-b95b0b`
Phase: spec (discovery → **spec** → implementation)
Status: design/track only — nothing here is implemented yet.
Companion to: [`graph-structured-memory.md`](graph-structured-memory.md) (discovery).

This document turns the discovery findings into an implementation-ready design:
exact schema diffs, mutation/read query definitions, MCP tool signatures, the
Python re-rank math, config knobs, and a migration. It resolves every open
question from discovery §6. File/line references point at the code as it exists
today so the implementer can diff against it.

## 0. Decisions (resolving discovery §6)

| # | Open question | Decision |
|---|---|---|
| Q1 | `RelatedTo` symmetry & storage direction | **Store one direction; traverse both at read.** `memory_link(kind="related_to", a, b)` writes a single `RelatedTo: a -> b`. Reads union the in- and out-neighbours. Avoids double-write contention and a reconciliation job. `Contradicts` is treated the same way (symmetric, stored once). `Supersedes`/`Refines`/`AppliesTo` stay directional. |
| Q2 | `contract_refs` soft list vs. Topic-anchor-only | **Topic-anchor-only.** Contracts are modelled as `Topic{kind:"contract"}` nodes with a real Layer-1 `Tagged` edge (traversable). No second soft-ref list on `Memory`. `symbol_refs` stays as-is (the code symbol space is high-cardinality and per-repo; promoting it to Topic nodes would explode node count and re-introduce indexer write contention — see §2 rationale). |
| Q3 | Ranking weights / half-life: config vs constants | **Config knobs with constant defaults.** Add a frozen `RankConfig` to `config.py`, sourced from `WITAN_RANK_*` env vars / TOML, defaulting to the constants in §7.3. Ranking is always on; weights are tunable, not feature-flagged. |
| Q4 | Embeddings: `rrf` hybrid in first cut or deferred | **Deferred behind a config gate.** First cut is BM25 + graph re-rank only. `recall()` is designed so the seed step can later swap `search()` for `rrf(bm25, nearest)` with no change to the expand/prune/re-rank stages. Gated on `WITAN_EMBED_ENABLED` (default off) — see §8.3. |
| Q5 | Migration for promoting free-string `tags` → `Topic` | **Additive backfill, dual-write, no removal.** `tags: [String]?` stays on every node. A one-shot `witan migrate topics` backfills a `Topic{kind:"topic"}` + `Tagged` edge per distinct tag; `memory_store`/`memory_update` dual-write tags → Topic nodes going forward. See §6 (Topics) and §9 (migration). |

Two cross-cutting decisions inherited from discovery, restated because every task
depends on them:

- **No cross-store edges.** Memory↔code/bridge links are soft refs (`symbol_refs`)
  or Layer-1 proxy `Topic` nodes — never an omnigraph edge into Layer 2/2.5.
- **Composite ranking lives in Python, not `order`.** The engine's `order` clause
  *can* sort by node fields (the listing queries already use
  `order { $m.created_at desc }`); the constraint is that the v1 **search** queries
  are defined to order by `bm25` alone, and the composite score (recency ×
  corroboration × confidence × penalties) can't be expressed as a single `order`
  clause. So composite ranking and superseded-pruning are a **Python re-rank over
  the candidate set**.

## 1. Task → deliverable map

| Task | Spec section | Net new schema | New MCP tools |
|---|---|---|---|
| `tk-typed-inter-memory-edges-supersedes-6c08da` (p1) | §3 | `Refines`, `Contradicts`, `RelatedTo` edges | `memory_link`, `memory_neighbors` |
| `tk-hard-memory-symbol-and-memory-contract-anchor-ed-61b857` (p2) | §4 | (uses `Topic`+`Tagged` from §6) | `memory_for_contract` (+ extends `context_for_symbol`) |
| `tk-provenance-edges-sessions-traces-produced-memori-53265e` (p2) | §5 | `SessionProduced` edge | (none — auto-wired in `memory_store`) |
| `tk-topic-entity-nodes-traversal-based-retrieval-f52d7f` (p2) | §6 | `Topic` node, `Tagged` edge | `memory_link` (kind `tagged`), `topic_get` |
| `tk-corroboration-confidence-and-recency-decay-457c78` (p2) | §7 | `confidence: F32?` on `Memory` | (none — re-rank in existing `memory_search`) |
| `tk-graph-aware-retrieval-api-optional-embeddings-f90d67` (p2) | §8 | (optional `Vector` on `Memory`) | `recall` |

Implementation order follows discovery §4: **§3 first** (foundation), then §5/§6
(more edges + topic join surface), then §4 (rides on §6 topics), then §7 (needs
edges to score), then §8 (composes all). Each section below is self-contained
enough to be one PR.

---

## 2. Rationale: why Topic nodes for contracts but soft refs for symbols

The asymmetry in Q2 is deliberate and load-bearing, so it is spelled out once
here and referenced by §4 and §6.

- **Contract keys are low-cardinality, shared, and written by the memory path.**
  `env_var`/`endpoint`/`package`/`service` `key_norm` values number in the
  hundreds, are shared across repos (that is the whole point of the bridge), and
  Topic nodes are created only when a human/agent stores a memory — low write
  volume, no indexer involvement. Safe as real Layer-1 nodes.
- **Code symbols are high-cardinality and per-repo.** A repo has tens of
  thousands of symbols. Promoting each referenced symbol to a Layer-1 proxy node
  would balloon node count and — worse — tempt the indexer into upserting them,
  re-creating exactly the write contention the bridge was made edge-free to avoid
  (`bridge-schema.pg:1-16`). So symbols stay soft refs; the win there is exposing
  the *forward* direction and a tidy reverse lookup (§4), not new nodes.

This keeps Topic node growth bounded and the indexer untouched (cross-cutting
constraint 3 in discovery §5).

---

## 3. Typed inter-memory edges + `memory_link` — `tk-typed-inter-memory-edges`

### 3.1 Schema diff — `schema/schema.pg`

After the existing `Supersedes`/`AppliesTo` declarations (`schema.pg:34,38`), add:

```
// Refines: a newer memory sharpens/extends an older one without replacing it.
edge Refines: Memory -> Memory

// Contradicts: two memories conflict. Symmetric in meaning; stored one
// direction and traversed both ways. Never hidden — surfaced for review.
edge Contradicts: Memory -> Memory

// RelatedTo: soft associative link. Symmetric; stored one direction.
edge RelatedTo: Memory -> Memory
```

All four typed edges are `Memory -> Memory`. As discovery §3.1 established, edge
names are **not** overloadable across type-pairs; these carry memory↔memory
relations only. Task/project supersession is explicitly out of scope (close the
old task with a `resolution` pointing at the new slug instead).

### 3.2 Mutation diff — `queries/mutations.gq`

After `link_applies_to` (`mutations.gq:75`):

```
query link_refines($from: String, $to: String) {
    insert Refines { from: $from, to: $to }
}
query link_contradicts($from: String, $to: String) {
    insert Contradicts { from: $from, to: $to }
}
query link_related_to($from: String, $to: String) {
    insert RelatedTo { from: $from, to: $to }
}
```

### 3.3 Read queries — `queries/read.gq`

Two read shapes are needed: (a) **neighbour traversal** for `memory_neighbors`
and the §8 expand step, and (b) **superseded-slug collection** for pruning.

```
// Direct neighbours of a memory along one edge kind (out-direction).
query supersedes_targets($slug: String) {
    match { $m: Memory { slug: $slug } $m supersedes $other }
    return { $other.slug }
}
query refines_targets($slug: String) {
    match { $m: Memory { slug: $slug } $m refines $other }
    return { $other.slug }
}
query applies_to_targets($slug: String) {
    match { $m: Memory { slug: $slug } $m applies_to $other }
    return { $other.slug }
}
// Symmetric edges: union both directions in the server (§3.4).
query related_out($slug: String) {
    match { $m: Memory { slug: $slug } $m related_to $other }
    return { $other.slug }
}
query related_in($slug: String) {
    match { $m: Memory { slug: $slug } $other related_to $m }
    return { $other.slug }
}
query contradicts_out($slug: String) {
    match { $m: Memory { slug: $slug } $m contradicts $other }
    return { $other.slug }
}
query contradicts_in($slug: String) {
    match { $m: Memory { slug: $slug } $other contradicts $m }
    return { $other.slug }
}

// All slugs that are the TARGET of any Supersedes edge — i.e. the superseded set.
// Used to prune/deprioritise in the Python re-rank. Bounded; the superseded set
// is small relative to total memories.
query all_superseded_slugs() {
    match { $m: Memory $m supersedes $old }
    return { $old.slug }
}
```

Lowercase edge predicate names (`supersedes`, `related_to`) match the engine's
traversal convention confirmed in discovery (`code_read.gq:93`, the
`witan-memory` recall memory on lowercase edge traversal).

### 3.4 MCP tools — `witan/server.py`

Add a `MemoryLinkKind` literal and two tools, mirroring the existing `task_link`
shape (`server.py:1487`):

```python
MemoryLinkKind = Literal[
    "supersedes", "refines", "applies_to", "contradicts", "related_to"
]

_MEMORY_LINK_MUTATIONS = {
    "supersedes": "link_supersedes",
    "refines": "link_refines",
    "applies_to": "link_applies_to",
    "contradicts": "link_contradicts",
    "related_to": "link_related_to",
}

@mcp.tool
def memory_link(from_slug: str, to_slug: str, kind: MemoryLinkKind) -> dict:
    """
    Create a typed edge between two memories.

    - supersedes  — `from` (newer) replaces `to` (older). `to` is hidden from
                    default search results.
    - refines     — `from` sharpens/extends `to` without replacing it.
    - applies_to  — `from` (a pattern/lesson) applies in the context of `to`
                    (a project_fact).
    - contradicts — `from` and `to` conflict. Symmetric; surfaced for review,
                    never hidden.
    - related_to  — soft association. Symmetric.
    """
    mutation = _MEMORY_LINK_MUTATIONS[kind]
    client.change("mutations.gq", mutation, {"from": from_slug, "to": to_slug})
    return {"from": from_slug, "to": to_slug, "kind": kind}


@mcp.tool
def memory_neighbors(slug: str, kinds: list[MemoryLinkKind] | None = None) -> dict:
    """
    Return the directly linked memories grouped by edge kind.

    For symmetric kinds (contradicts, related_to) both directions are unioned.
    Use after `memory_get` to see what a memory connects to.
    """
    # dispatch each requested kind to its target query; union in/out for symmetric
    ...
```

`memory_link` **closes the documented v2 gap** (`docs/agent-memory.md:1438`,
discovery §2) that `link_supersedes`/`link_applies_to` had no MCP exposure.

### 3.5 Superseded handling in `memory_search`

`memory_search` (`server.py:147`) keeps its BM25 dispatch, then post-filters:

1. Run the existing BM25 query → candidate rows.
2. `superseded = set(r["slug"] for r in client.read("read.gq", "all_superseded_slugs", {}))`.
3. Drop candidates whose slug ∈ `superseded` (default), **unless**
   `include_superseded=True` (new optional param, default `False`).
4. Contradictions are **not** dropped; §8's `recall` annotates them, but plain
   `memory_search` leaves them in (it returns flat rows). The annotation lives in
   `recall` where the response shape can carry a `contradicted_by` field.

Add the param:

```python
@mcp.tool
def memory_search(query, repo=None, kind=None, include_superseded: bool = False):
```

### 3.6 Acceptance criteria

- `memory_link(a, b, "supersedes")` then `memory_search(<matches b>)` no longer
  returns `b`; `include_superseded=True` returns it.
- `memory_link(a, b, "related_to")` → `memory_neighbors(b)` lists `a` under
  `related_to` (in-direction union works).
- `memory_link(a, b, "contradicts")` → both still appear in search.
- Linking with a non-existent slug does not raise on write (engine takes raw
  slugs) but the dead edge never surfaces in a typed read — documented as a
  known no-op, matching discovery §3.1.

---

## 4. Hard memory↔symbol & memory↔contract-anchor links — `tk-hard-memory-symbol-...`

Reframed per discovery §3.2 and §2 above: no cross-store edges. Two mechanisms.

### 4.1 Symbols — expose the forward direction (soft refs, no schema change)

`symbol_refs` and the reverse lookup `context_for_symbol` (`server.py:1530`)
already exist. The gap is the **forward** direction ("given this memory, what
symbols / who consumes them") and surfacing code context inline. Deliverables:

- `memory_get` already returns `symbol_refs` (`read.gq:159`). Add a thin
  convenience that, for each `symbol_ref`, optionally resolves the live symbol via
  the witan-code tools (`code_find_definition`, `code_interface_consumers`). This
  is **read-time cross-store fan-out in Python**, not an edge — exactly how
  `context_for_symbol` already crosses the boundary.
- No schema or mutation change for this half.

### 4.2 Contracts — `Topic{kind:"contract"}` anchors (depends on §6)

A contract anchor is a `Topic` whose `kind == "contract"` and whose `name` equals
a bridge `key_norm` (e.g. `GET /api/v1/courses/` or `DATABASE_URL`). Memories link
to it with the §6 `Tagged` edge. This makes "what do we know about endpoint X" a
single Layer-1 traversal, while `name` still joins to the bridge's `key_norm` and
`code_interface_consumers` at read time for the code side.

New tool:

```python
ContractKind = Literal["env_var", "endpoint", "package", "service"]

@mcp.tool
def memory_for_contract(key_norm: str, kind: ContractKind | None = None) -> dict:
    """
    Return memories tagged to a contract anchor (env_var / endpoint / package /
    service), plus the bridge bindings that share the key so callers can pivot
    to the code that produces/consumes it.

    Resolves the Topic{kind:"contract", name:key_norm} node, walks `Tagged` to
    its memories (Layer 1), and separately queries the bridge for bindings on
    key_norm (Layer 2.5) — joined in Python on the shared key, never an edge.
    """
```

Contract anchor slug convention: `tp-contract-<kind>-<slug(key_norm)>` so the
node is addressable and idempotent (re-storing a memory for the same contract
re-uses the node).

### 4.3 Acceptance criteria

- Storing a memory with a contract topic, then `memory_for_contract(key_norm)`,
  returns that memory **and** the bridge bindings on the same `key_norm`.
- `context_for_symbol` unchanged in behaviour; forward-resolution helper returns
  live definition/consumer info for a memory's `symbol_refs` when witan-code is
  reachable, and degrades to the raw ref strings when it is not.

---

## 5. Provenance: session/trace produced memories — `tk-provenance-edges`

### 5.1 Schema diff — `schema/schema.pg`

`Informed: WorkflowProject -> Memory` (`schema.pg:121`) covers project-grain
provenance. Add **session-grain**:

```
// SessionProduced: a WorkflowSession created or substantively updated a Memory.
// Bare name `Produced` is taken (WorkflowProject -> WorkflowTrace), hence the
// qualified name.
edge SessionProduced: WorkflowSession -> Memory
```

### 5.2 Mutation — `queries/mutations.gq`

```
query link_session_produced($from: String, $to: String) {
    insert SessionProduced { from: $from, to: $to }
}
```

### 5.3 Read — `queries/read.gq`

Both endpoints are bound as typed nodes, and the traversal predicate is the edge
type lowercased with underscores stripped (`sessionproduced`, not
`session_produced`) — the engine's convention confirmed in discovery.

```
// Memories produced during a session (provenance walk).
query session_produced_memories($session_slug: String) {
    match {
        $s: WorkflowSession { slug: $session_slug }
        $m: Memory
        $s sessionproduced $m
    }
    return { $m.slug, $m.kind, $m.title, $m.created_at }
}

// Reverse: the session(s) that produced a given memory. Needed by the §8.2
// expand step, which walks a memory → its producing session → that session's
// other produced memories (provenance siblings).
query producing_sessions($slug: String) {
    match {
        $s: WorkflowSession
        $m: Memory { slug: $slug }
        $s sessionproduced $m
    }
    return { $s.slug }
}
```

"What did we learn during project X" composes existing
`list_sessions_by_project` (`read.gq:280`) → per-session
`session_produced_memories`, plus project-grain `Informed`.

### 5.4 Auto-wiring in `memory_store`

`memory_store` (`server.py:244`) auto-creates the edge when a session is active.
The active session is discoverable from the session-state file written by
`workflow_session_start` (discovery §3.3). Add, after the `insert_memory`
`client.change` (`server.py:315`):

```python
active = _active_session_slug()   # None if absent
if active:
    client.change(
        "mutations.gq", "link_session_produced",
        {"from": active, "to": slug},
    )
```

**Parallel sessions:** several `workflow-session-*.json` files can coexist in
`/tmp` at once (parallel sessions are explicitly supported — discovery §1). So
`_active_session_slug()` must **not** scan or pick an arbitrary file; it keys off
`$CLAUDE_SESSION_ID` to read *this* session's state file
(`_session_state_path(session_id)`, the same path `workflow_session_start` and the
`session-checkpoint` hook use) and returns its `session_slug`:

```python
def _active_session_slug() -> str | None:
    session_id = os.environ.get("CLAUDE_SESSION_ID")
    if not session_id:
        return None
    try:
        state = json.loads(_session_state_path(session_id).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return state.get("session_slug") or None
```

It must fail soft (return `None` on missing env / any read/parse error) —
provenance is best-effort and must never block a memory write. (A `memory_update`
tool is deferred to the v2 roadmap; when it lands, record provenance there too —
idempotent: accept the duplicate, reads dedupe.)

### 5.5 Acceptance criteria

- After `workflow_session_start` then `memory_store`, the new memory appears in
  `session_produced_memories(active_session)`.
- `memory_store` with **no** active session writes the memory and creates no
  edge — no error.
- Project → sessions → memories assembles the "learned during project X" set.

---

## 6. Topic/Entity nodes + traversal retrieval — `tk-topic-entity-nodes`

This is the join surface used by §4 (contracts) and §8 (expand). Land it before
§4.

### 6.1 Schema diff — `schema/schema.pg`

```
// Topic: a join-surface node memories attach to. One node type, several kinds:
//   topic    — promoted from a free-string tag
//   contract — name == bridge key_norm (env_var/endpoint/package/service)
//   symbol   — reserved; not populated in the first cut (see §2: symbols stay soft)
//   entity   — a named entity (service, library, person, concept)
// Slug convention: tp-<kind>-<slug(name)>
node Topic {
    slug: String @key
    name: String @index
    kind: enum(topic, contract, symbol, entity) @index
    created_at: DateTime @index
}

// Tagged: a Memory is about a Topic. Real Layer-1 edge (traversable).
edge Tagged: Memory -> Topic
```

`name @index` lets a contract anchor be found by `key_norm`; the `slug` key makes
upserts idempotent.

### 6.2 Mutations — `queries/mutations.gq`

```
query insert_topic($slug: String, $name: String, $kind: String, $created_at: DateTime) {
    insert Topic { slug: $slug, name: $name, kind: $kind, created_at: $created_at }
}
query link_tagged($from: String, $to: String) {
    insert Tagged { from: $from, to: $to }
}
```

Topic insert is upsert-on-slug: the server checks `get_topic` first and only
inserts when absent (low write volume, so the read-before-write is cheap and
keeps slugs unique).

### 6.3 Reads — `queries/read.gq`

```
query get_topic($slug: String) {
    match { $tp: Topic { slug: $slug } }
    return { $tp.slug, $tp.name, $tp.kind, $tp.created_at }
}
query topic_by_name_kind($name: String, $kind: String) {
    match { $tp: Topic { name: $name, kind: $kind } }
    return { $tp.slug, $tp.name, $tp.kind }
}
// Memories attached to a topic (traversal-based retrieval; cross-repo by nature).
query memories_for_topic($topic_slug: String) {
    match { $tp: Topic { slug: $topic_slug } $m tagged $tp }
    return { $m.slug, $m.kind, $m.title, $m.repo, $m.created_at }
}
// Topics a memory is tagged with (for expand + display).
query topics_for_memory($slug: String) {
    match { $m: Memory { slug: $slug } $m tagged $tp }
    return { $tp.slug, $tp.name, $tp.kind }
}
```

`memories_for_topic` is the cross-repo propagation primitive from discovery §3.4:
two memories in different repos sharing a `package` topic are one hop apart, so
"what does the org know about `cryptography`" spans repos without BM25 matching.

### 6.4 Tools

- Extend `memory_link` (§3.4) with kind `tagged` (from = Memory slug, to = Topic
  slug); auto-create the Topic if `to` is given as `name:kind` rather than an
  existing slug.
- Add `topic_get(slug)` and have `memory_get` optionally include
  `topics_for_memory`.

### 6.5 Dual-write tags → Topics

`memory_store`/`memory_update` keep writing `tags: [String]?` **and**, for each
tag, upsert a `Topic{kind:"topic", name:tag}` + `Tagged` edge. Back-compat: the
string list is the source of truth for old readers; the Topic graph is the new
traversal surface. See migration §7.

### 6.6 Acceptance criteria

- Two memories in different repos tagged with the same topic both come back from
  `memories_for_topic`.
- `memory_store(tags=["uv"])` creates `tp-topic-uv` and a `Tagged` edge; storing
  a second memory with the same tag re-uses the node (no duplicate).

---

## 7. Corroboration, confidence & recency decay — `tk-corroboration-confidence-...`

Depends on §3/§5/§6 (edges to count) and is a pure **Python re-rank** — the
engine cannot express the composite in `order` (discovery §5.2).

### 7.1 Schema diff — `schema/schema.pg`

Add to `node Memory` (after `symbol_refs`, `schema.pg:29`):

```
    confidence: F32?   // author/agent-set trust 0.0–1.0; null treated as default
```

Thread `confidence` through `insert_memory`/`update_memory`
(`mutations.gq:5,42`), `memory_store`/`memory_update`, and the search/get return
projections (so the re-rank can read it).

### 7.2 Ranking formula

Composite score over the BM25 candidate set:

```
score = w_bm25   * norm_bm25
      + w_recency * exp(-age_days / half_life_days)
      + w_corrob  * log1p(corroboration)
      + w_conf    * (confidence ?? default_confidence)
      - penalty_superseded   * is_superseded
      - penalty_contradicted * is_contradicted
```

- `norm_bm25` — a normalised BM25 signal over the candidate set so weights are
  comparable. The engine can't project the raw `bm25(...)` score as a returnable
  column (it's only valid inside `order`), so the implementation uses **rank
  position** as the proxy: the candidate set already comes back in BM25-desc order,
  so the top hit is `1.0` and the last is `0.0` (single candidate → `1.0`). A true
  min-max of the raw score would be equivalent if/when the engine exposes it.
- `age_days` — from `updated_at` to now. `recall`/search receive `now` from the
  server clock at call time.
- `corroboration` — count of supporting edges into/out of the memory:
  `AppliesTo` + `RelatedTo` + `Informed` + `SessionProduced`. `Contradicts` does
  **not** count as support (it drives the penalty instead).
- `is_superseded` / `is_contradicted` — booleans from the §3 traversals.
  Superseded items are normally dropped before scoring (§3.5); the penalty term
  matters only when `include_superseded=True`.

### 7.3 Config knobs — `config.py` (Q3)

Add a frozen `RankConfig` resolved from `WITAN_RANK_*` env vars / TOML, defaults:

| Knob | Env var | Default |
|---|---|---|
| `w_bm25` | `WITAN_RANK_W_BM25` | `1.0` |
| `w_recency` | `WITAN_RANK_W_RECENCY` | `0.3` |
| `w_corrob` | `WITAN_RANK_W_CORROB` | `0.2` |
| `w_conf` | `WITAN_RANK_W_CONF` | `0.2` |
| `half_life_days` | `WITAN_RANK_HALFLIFE_DAYS` | `90` |
| `default_confidence` | `WITAN_RANK_DEFAULT_CONF` | `0.6` |
| `penalty_superseded` | `WITAN_RANK_PEN_SUPERSEDED` | `1.0` |
| `penalty_contradicted` | `WITAN_RANK_PEN_CONTRADICTED` | `0.25` |
| `w_hop` | `WITAN_RANK_W_HOP` | `0.5` |

`w_hop` is the per-hop distance penalty applied only by graph-aware `recall`
(§8.2 step 4): seeds (hop 0) outrank expanded neighbours. It lives on `RankConfig`
alongside the search weights but does not affect plain `memory_search`, which has
no expansion step.

### 7.4 Where it runs

`memory_search` and `recall` (§8) both apply the re-rank after fetching the BM25
candidate set and the edge/superseded sets. Factor it into a single
`_rerank(rows, *, now, rank_cfg, edge_index)` helper so both paths share it.
Listing tools (`memory_list`, `memory_get_project_facts`, `patterns_*`) keep
their `created_at` ordering — re-rank is search-only.

### 7.5 Acceptance criteria

- Of two equal-BM25 memories, the more recent and more corroborated ranks higher.
- Setting `WITAN_RANK_W_RECENCY=0` reproduces (modulo corroboration/confidence)
  the old BM25 order.
- A memory with low `confidence` ranks below an equal one with high confidence.

---

## 8. Graph-aware retrieval API + optional embeddings — `tk-graph-aware-retrieval-api`

The capstone — composes §3–§7 into one tool.

### 8.1 `recall` tool

```python
@mcp.tool
def recall(
    query: str | None = None,
    symbol_id: str | None = None,
    task: str | None = None,
    topic: str | None = None,
    repo: str | None = None,
    kind: MemoryKind | None = None,
    hops: int = 1,
    limit: int = 20,
) -> dict:
    """
    Graph-aware contextual recall. Seeds from any combination of query (BM25),
    symbol_id, task, or topic; expands along memory edges + topics; prunes
    superseded; flags contradictions; re-ranks with the composite score.

    Returns {"memories": [...ranked...], "contradictions": [...], "seeds": {...}}.
    """
```

### 8.2 Pipeline

1. **Seed**
   - `query` → BM25 candidate set (existing dispatch, §3.5).
   - `symbol_id` → `context_for_symbol` memories (`server.py:1531`).
   - `task` → memories the task `Addresses` (`schema.pg:175`) + memories sharing
     the task's `symbol_refs`.
   - `topic` → `memories_for_topic` (§6.3).
   - Union the seed slugs (dedupe).
2. **Expand** `hops` (default 1, cap 2) along `AppliesTo`, `RelatedTo`, `Tagged`
   (memory→topic→memories), and `SessionProduced` (via the producing session's
   siblings). Track hop distance per slug for a small distance penalty.
3. **Prune** drop superseded (unless `include_superseded`); collect
   `Contradicts` pairs into the `contradictions` list.
4. **Re-rank** with `_rerank` (§7.4), minus `hop_distance * w_hop` so seeds
   outrank distant neighbours. `limit` the result.
5. **Hydrate** fetch full rows for the surviving slugs.

### 8.3 Optional embeddings (Q4 — deferred, config-gated)

Gated on `WITAN_EMBED_ENABLED` (default `false`). When enabled, the **seed** step
swaps `search()` for the hybrid path already specced in the v2 roadmap
(`docs/agent-memory.md:1436`):

- Schema: `content_vec: Vector(1536) @embed("content")` on `Memory`.
- Build step: `omnigraph embed` (or `witan embed`) populates vectors.
- Seed query: `order { rrf(bm25($m.content, $query), nearest($m.content_vec, $query)) desc }`.

Expand/prune/re-rank stages are unchanged — embeddings only widen the seed set.
The retrieval path degrades cleanly to BM25-only when the flag is off or no
embedding provider is configured.

### 8.4 Acceptance criteria

- `recall(query=...)` with no edges in the graph returns the same set as
  `memory_search` (graph expansion is additive, not lossy).
- `recall(topic=...)` returns cross-repo memories sharing that topic.
- `recall` over a graph with a `Supersedes` edge omits the superseded node and
  lists any `Contradicts` pair under `contradictions`.
- With `WITAN_EMBED_ENABLED=false` (default), no embedding provider is required
  and `recall` works.

---

## 9. Migration & rollout

1. **Schema is additive.** New edges, the `Topic` node, and `Memory.confidence`
   add nothing required on existing rows (`confidence` nullable; old memories have
   no edges/topics and simply score without those terms). No destructive
   migration of `Memory`.
2. **`witan migrate topics`** (one-shot CLI, idempotent): scan all `Memory` rows;
   for each distinct `tag`, upsert `Topic{kind:"topic"}` + `Tagged` edge. Safe to
   re-run (upsert-on-slug). Run once after the §6 deploy.
3. **Dual-write window.** `tags: [String]?` is never removed; Topic graph is
   additive. Old readers keep working on the string list.
4. **Re-rank is on by default** with the §7.3 defaults; teams tune via
   `WITAN_RANK_*` without redeploying.
5. **Embeddings stay off** until a provider is configured and `omnigraph embed`
   has run.

## 10. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Off-type slug in an edge insert creates a dead edge (engine takes raw slugs) | Server validates both endpoints exist + are `Memory` before `memory_link` write; documented as the engine's behaviour, not a guarantee. |
| `all_superseded_slugs` grows unbounded | Superseded set is small (only edge targets); if it ever dominates, switch to per-query `supersedes_targets` over the candidate set instead of a global fetch. |
| Re-rank changes long-stable top results, surprising users | Defaults weight BM25 at 1.0 and others ≤0.3; `WITAN_RANK_W_*=0` reproduces BM25 order for rollback. |
| Topic node duplication under concurrent writes | Upsert-on-slug + deterministic `tp-<kind>-<slug(name)>` slug; duplicate inserts collide on `@key` and are no-ops. |
| Provenance edge write fails and blocks a memory store | `_active_session_slug()` and the `SessionProduced` write fail soft — provenance is best-effort. |

## 11. Spec → implementation handoff checklist

- [ ] §3 schema + mutations + `memory_link`/`memory_neighbors` + superseded prune (PR 1, p1)
- [ ] §6 `Topic`/`Tagged` + dual-write + `witan migrate topics` (PR 2)
- [ ] §5 `SessionProduced` + auto-wire in `memory_store`/`memory_update` (PR 3)
- [ ] §4 `memory_for_contract` + symbol forward-resolution helper (PR 4, after §6)
- [ ] §7 `Memory.confidence` + `RankConfig` + `_rerank` shared helper (PR 5)
- [ ] §8 `recall` pipeline; embeddings left behind `WITAN_EMBED_ENABLED` (PR 6)

Each PR carries the acceptance criteria from its section as tests.
