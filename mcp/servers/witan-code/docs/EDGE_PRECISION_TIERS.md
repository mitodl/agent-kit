# Typed cross-repo edge precision tiers

Status: accepted (implementation phase, 2026-07-06)
Related: [STAGE2_STITCHING.md](STAGE2_STITCHING.md), [SYMBOL_TABLE.md](SYMBOL_TABLE.md)

Kythe distinguishes `ref/call/direct` from the weaker `ref/call` edge kind so
consumers can choose how much they trust a cross-repo link. This project
does the same thing: replace the previous binary consumer/provider model
with **typed edges that carry precision**, so callers filter by a minimum
trust floor (`min_precision`) instead of picking a confidence threshold
themselves.

## Tiers

| Tier        | Source                                                          | Trust |
|-------------|------------------------------------------------------------------|-------|
| `precise`   | Stage 2: canonical symbol string join (`witan_code.stitch`)       | high  |
| `heuristic` | Stage 3: `(kind, key_norm)` binding grouping, confidence-scored    | medium|
| `fuzzy`     | future: embedding/BM25 route similarity (separately tracked research task) | low |

No `fuzzy` edges exist yet — that tier is currently identical to
`heuristic` everywhere it's accepted as a parameter.

## `min_precision` is a floor, not an exact match

`min_precision="precise"` returns only the precise tier. `min_precision=
"heuristic"` (the default everywhere — **preserves every tool's prior
behavior** when the parameter is omitted) returns precise + heuristic.
`min_precision="fuzzy"` returns everything. A heuristic edge is suppressed
whenever a precise edge already covers the same `(consumer_repo,
provider_repo, kind, key_norm)` triple, so the same logical link never shows
up twice at two different trust levels.

## The merged edge (`witan_code.edges`)

```python
TypedEdge(
    precision,           # "precise" | "heuristic" | "fuzzy"
    consumer_repo, provider_repo,
    kind, key_norm,
    canonical_symbol,     # consumer's symbol string; precise tier only, else None
    confidence,           # 1.0 for precise; the heuristic tier's score otherwise
    evidence,             # tuple[dict] of {repo, file, line} — see below
)
```

`edges.cross_repo_edges(repo_symbol_rows, binding_rows, *, min_precision=
"heuristic", min_confidence=0.5) -> list[TypedEdge]` computes precise edges
via `stitch.resolve()`, then (unless `min_precision == "precise"`) adds
heuristic edges grouped from raw `InterfaceBinding` rows the same way
`visualize.build_graph` already does, minus whatever the precise pass
already covered.

**Evidence** is source-level backing for an edge: `{repo, file, line}` per
contributing occurrence. Heuristic edges can carry many (one per matching
consumer binding row); precise edges carry at most two — Stage 1's symbol
table keeps only one deterministic exemplar occurrence per symbol (see
SYMBOL_TABLE.md), not every occurrence, so a precise edge's evidence is
`(consumer exemplar, provider exemplar)`.

`edges.precise_pairs(repo_symbol_rows) -> frozenset[(consumer_repo,
provider_repo, kind, key_norm)]` is a cheaper membership-test helper for
tools that only need to know whether a *specific* binding participates in a
precise edge, without building the full merged list.

## Surface

Every place that already produced (or filtered) cross-repo links now takes
`min_precision`, defaulting to `"heuristic"` so nothing changes unless a
caller opts in:

* CLI: `witan code deps --min-precision precise` (fetches `all_repo_symbols`
  only when asked — `heuristic`, the default, has zero extra query cost).
* `visualize.build_graph(rows, *, min_precision="heuristic",
  repo_symbol_rows=None)` — pass `repo_symbol_rows` when requesting
  `"precise"`. The special "repo depends on what it deploys" `service` edge
  is unaffected by `min_precision`; it isn't a symbol-joined consumer/
  provider relationship.
* MCP: `code_interface_providers`, `code_interface_consumers`,
  `code_interface_search`, `code_cross_repo_impact` all accept
  `min_precision`. For the first three, a row is kept if its
  `(kind, key_norm)` is covered by a precise edge *anywhere* in the store.
  `code_cross_repo_impact` is repo-pair-specific: an `other` binding is kept
  only if a precise edge links `own_repo` and that binding's repo
  specifically, not merely because some unrelated repo pair resolves the
  same key_norm precisely.
* `code_precise_edges` / `code_unresolved_symbols` (Stage 2's own tools,
  added earlier) are unaffected — they only ever produce the `precise` tier
  by definition.

## Not in scope here

* A `c4gen` extractor filtering to `:CALLS/precise` by default — no such
  extractor exists in this repository; if one is added elsewhere, it should
  call `edges.cross_repo_edges(..., min_precision="precise")` and fall back
  to `"heuristic"` with a warning, per the original design.
* Repositioning the existing confidence-scoring heuristic stack
  (`bridge_extractors.adjust_confidence`) as the formal `:CALLS/heuristic`
  tier's scoring — that stack already exists (PR #10) and is reused as-is
  here; migrating its framing/naming is a separate, still-blocked task.
* Visual retagging of the HTML/Rich `deps` graph by precision (e.g. distinct
  edge colors per tier) — `min_precision` filters which edges are shown;
  making the surviving edges visually distinguishable by tier is a follow-on
  UX improvement, not required by this task.
