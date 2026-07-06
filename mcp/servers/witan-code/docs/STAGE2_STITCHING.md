# Stage 2: cross-repo symbol stitching (read time)

Status: accepted (implementation phase, 2026-07-06)
Related: [SYMBOL_TABLE.md](SYMBOL_TABLE.md), [SYMBOL_FORMAT.md](SYMBOL_FORMAT.md),
[EDGE_PRECISION_TIERS.md](EDGE_PRECISION_TIERS.md)

Stage 2 joins the per-repo symbol tables Stage 1 emits into precise
cross-repo edges — entirely at read time, entirely in Python, never written
back to the store. This is the RANGER/SCIP pattern this project is built
around: a repo's indexing pass never has to know about any other repo; the
join only happens when someone asks a cross-repo question.

## Algorithm (`witan_code.stitch.resolve`)

Input is every `RepoSymbol` row in the bridge store (`all_repo_symbols`).
Rows split into two sets:

* `exported` rows, grouped by their join key (see
  [SYMBOL_TABLE.md § Stage-2 join contract](SYMBOL_TABLE.md#stage-2-join-contract)).
* `external` rows, each resolved independently against that grouping.

For one `external` row:

1. Look up its join key in the `exported` grouping, excluding rows from the
   same repo (a repo doesn't cross-repo-link to itself) and, for `http`,
   excluding provider rows whose method doesn't match (`*` matches
   anything).
2. **Zero candidates** → the row goes to `unresolved` (Stage-3 fallback
   territory — see below).
3. **One or more candidates** → one edge per candidate. `match_count` is the
   candidate count, so a caller can tell a clean single match from a
   fan-out. Version disambiguation (SYMBOL_FORMAT.md decision 1) picks which
   candidate(s) are `preferred`: exact version match, else `main`, else every
   remaining candidate is preferred and the edge is flagged
   `ambiguous_version` — every candidate is still returned as its own edge
   rather than silently dropped, matching this project's pattern of
   surfacing all cross-repo data and letting the caller filter (see
   `code_cross_repo_impact`).

No edge is ever stored: `resolve()` is pure and its output is recomputed on
every call from the current `RepoSymbol` rows, so it can never go stale the
way a written edge could.

## Output shape

```python
PreciseEdge(
    consumer_repo, consumer_symbol,
    provider_repo, provider_symbol,
    kind, scheme,
    match_count, preferred, ambiguous_version,
)
```

`resolve(rows) -> (list[PreciseEdge], list[dict])` — the second element is
the raw `RepoSymbol` rows that had no candidate (`unresolved`).

## Stage-3 fallback

An `external` row landing in `unresolved` isn't necessarily a dead end: the
existing heuristic tier (`visualize.cross_repo_edges`, grouping raw
`InterfaceBinding` occurrences on the coarser `(kind, key_norm)` key with
confidence scoring) still has a chance to surface it — e.g. the provider
repo hasn't been indexed yet, or its extractor doesn't understand the
provider's framework. `code_unresolved_symbols` exists specifically to find
these gaps; `code_interface_consumers`/`code_interface_providers` remain the
way to check the heuristic tier for the same reference.

Typed edge kinds (`:CALLS/precise`, `:CALLS/heuristic`, `:CALLS/fuzzy`) that
formally merge these two tiers into one filterable result now exist —
`witan_code.edges.cross_repo_edges()`, see
[EDGE_PRECISION_TIERS.md](EDGE_PRECISION_TIERS.md).

## Surface

* CLI: `witan code stitch [--repo URI] [--unresolved]`
* MCP: `code_precise_edges(repo=None)`, `code_unresolved_symbols(repo=None)`

Both accept an optional `repo` filter that keeps edges/gaps touching that
repo (either side, for edges).
