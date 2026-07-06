# Per-repo symbol tables (Stage 1)

Status: accepted (implementation phase, 2026-07-05)
Related: [SYMBOL_FORMAT.md](SYMBOL_FORMAT.md), [PACKAGE_MAP.md](PACKAGE_MAP.md)

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
| `exported` | The repo provides this contract — its public surface (from provider bindings, fully qualified by the [package map](PACKAGE_MAP.md)) |
| `external` | The repo references this contract but cannot resolve it locally (from consumer bindings; package/manager/version are `.` unless the reference site names them) |

`external` rows are the RANGER-style *import placeholder nodes*: unresolved
stand-ins written at index time, redirected to another repo's `exported` row
at read time by Stage 2. They are rows, not edges — grouping at read time is
what makes per-repo indexing order-independent (re-indexing repo B never
invalidates repo A).

Each row carries the parsed symbol fields (`scheme`, `manager`, `package`,
`version`, `descriptor` — see [SYMBOL_FORMAT.md](SYMBOL_FORMAT.md)) plus:

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
   ([SYMBOL_FORMAT.md](SYMBOL_FORMAT.md) — identity lives in the
   manager/package fields, not the descriptor); `key_norm` carries the
   package name for both `exported` and `external` rows instead, so it is the
   only usable join key for packages.
3. Package identity (`manager`/`package` fields) disambiguates when several
   repos export the same descriptor or key_norm; version matching follows
   SYMBOL_FORMAT.md decision 1 (`.` matches anything; prefer exact, then
   `main`, else flag `ambiguous_version`).

A successful join is a `:CALLS/precise` edge (computed, never stored); the
`(kind, key_norm)` binding grouping remains the `:CALLS/heuristic` fallback.

## Inspecting

```
witan code symbols [--repo URI] [--role exported|external] [--scheme http]
```

## Write contention

`RepoSymbol` rows are keyed by repo (slug prefix `repo|`), so concurrent index
runs from different repos never touch the same row — the same flat-node
argument that shaped `InterfaceBinding` (see bridge-schema.pg header).
