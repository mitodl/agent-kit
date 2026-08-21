<!--
  MIRRORED FILE — DO NOT EDIT HERE.
  Edit mcp/servers/witan-code/docs/SYMBOL_TABLE.md instead; `just docs-gen` copies it into the site.
-->

!!! info "This page lives with the code"

    The authoritative copy is
    [`mcp/servers/witan-code/docs/SYMBOL_TABLE.md`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan-code/docs/SYMBOL_TABLE.md).

# Per-repo symbol tables (Stage 1)

Status: accepted (implementation phase, 2026-07-05)
Related: [SYMBOL_FORMAT.md](symbol-format.md), [PACKAGE_MAP.md](package-map.md),
[STAGE2_STITCHING.md](stage2-stitching.md)

Stage 1 of the two-stage cross-repo model: every indexed repo emits a
**self-contained symbol table** — the stable, deduplicated artifact that
Stage 2 joins across repos at read time. No extraction pass ever writes a
cross-repo edge; linking is deferred entirely to read time (the SCIP model).

## The artifact

One `RepoSymbol` row per `(repo, role, symbol)` in the shared bridge store
(`_bridge.omni`), alongside the per-occurrence `InterfaceBinding` rows it is
aggregated from:

| role       | meaning                                                        |
|------------|----------------------------------------------------------------|
| `exported` | The repo provides this contract — its public surface (from provider bindings, fully qualified by the [package map](package-map.md)) |
| `external` | The repo references this contract but cannot resolve it locally (from consumer bindings; package/manager/version are `.` unless the reference site names them) |

`external` rows are the RANGER-style *import placeholder nodes*: unresolved
stand-ins written at index time, redirected to another repo's `exported` row
at read time by Stage 2. They are rows, not edges — grouping at read time is
what makes per-repo indexing order-independent (re-indexing repo B never
invalidates repo A).

Each row carries the parsed symbol fields (`scheme`, `manager`, `package`,
`version`, `descriptor` — see [SYMBOL_FORMAT.md](symbol-format.md)) plus:

* `key_norm` — the coarse join key (for `http` the method-less normalized
  path; consumer methods are usually the `*` wildcard, so exact descriptor
  equality under-joins endpoints).
* `n_refs` — occurrence count in the repo.
* `confidence` — max over occurrences (`exported` rows are always 1.0).
  Stage 2's precision tiers filter on this.
* `file` / `line` — one deterministic exemplar occurrence (min by file, line).

## Rebuild semantics

The table is **exactly rebuilt** on every bridge write (`bridge.write_bindings`),
not incrementally patched: delete the repo's `RepoSymbol` rows, re-aggregate
from the binding occurrences that survive that write (stored rows outside the
per-file purge set, plus the fresh batch). This keeps the table consistent
with the bindings even on narrow single-file reindexes, with no
tombstone/refcount bookkeeping.

Rows whose stored bindings predate symbol emission (no `symbol` value) are
skipped; they regain table coverage when their file is next reindexed.

## Stage-2 join contract

Stage 2 matches `external` rows against other repos' `exported` rows:

1. `env` / `svc` — exact `(scheme, descriptor)` match (`symbols_by_descriptor`).
2. `http` / `pkg` — `(scheme, key_norm)` match (`symbols_by_key`). `http`
   descriptors embed the method, which consumers usually can't determine
   statically (`*`), so the coarse key_norm (method-less path) is the join
   key, followed by method compatibility: a consumer method of `*` matches
   any provider method. `pkg` canonical descriptors are always `.`
   ([SYMBOL_FORMAT.md](symbol-format.md) — identity lives in the
   manager/package fields, not the descriptor); `key_norm` carries the
   package name for both `exported` and `external` rows instead, so it is the
   only usable join key for packages.
3. Package identity (`manager`/`package` fields) disambiguates when several
   repos export the same descriptor or key_norm; version matching follows
   SYMBOL_FORMAT.md decision 1 (`.` matches anything; prefer exact, then
   `main`, else flag `ambiguous_version`).

A successful join is a `:CALLS/precise` edge (computed, never stored); the
`(kind, key_norm)` binding grouping remains the `:CALLS/heuristic` fallback.
The concrete join implementation, its edge shape, and the unresolved-symbol
gap report are specified in [STAGE2_STITCHING.md](stage2-stitching.md).

## Second consumer: the heuristic tier's confidence signals

The table has a second reader beyond Stage 2's join: `bridge.write_bindings`
sources the cross-repo half of two confidence heuristics
(`bridge_extractors.adjust_confidence`) from other repos' `exported` rows
rather than re-deriving the same information from raw `InterfaceBinding`
rows —

* `self_provided_key` (−0.5): the consuming repo also exports the same
  `key_norm` — checked against other repos' `exported` rows plus this
  repo's own surviving/fresh provider bindings (its own table hasn't been
  rebuilt yet at this point in the write).
* `known_provider_package` (+0.3): a co-located package import matches an
  `exported` package row from a different repo.

Both signals degrade to their pre-Stage-1 baseline (no boost/penalty) if the
bridge store predates `RepoSymbol` — the write is never blocked on it.

## Inspecting

```
witan code symbols [--repo URI] [--role exported|external] [--scheme http]
witan code stitch [--repo URI] [--unresolved]
```

## Write contention

`RepoSymbol` rows are keyed by repo (slug prefix `repo|`), so concurrent index
runs from different repos never touch the same row — the same flat-node
argument that shaped `InterfaceBinding` (see bridge-schema.pg header).
