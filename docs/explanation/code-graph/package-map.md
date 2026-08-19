<!--
  MIRRORED FILE — DO NOT EDIT HERE.
  Edit mcp/servers/witan-code/docs/PACKAGE_MAP.md instead; `just docs-gen` copies it into the site.
-->

!!! info "This page lives with the code"

    The authoritative copy is
    [`mcp/servers/witan-code/docs/PACKAGE_MAP.md`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan-code/docs/PACKAGE_MAP.md).

# Package map — declaring repo → package identity

Status: accepted (discovery phase, 2026-07-05)
Related: [SYMBOL_FORMAT.md](symbol-format.md)

The package map declares which canonical package identity a repository
provides. It is the input Stage-2 cross-repo stitching uses to qualify
provider [symbol strings](symbol-format.md) and the source of truth for the
`known_provider_package` boost in the heuristic confidence scorer. Every
production system that does precise cross-repo linking requires an explicit
map of this kind (scip-clang, LLVM, Chromium); witan-code is no exception.

## Decision: per-repo `witan-code.toml` (Option A)

Two options were evaluated:

* **Option A — per-repo `witan-code.toml`** at the repo root, read during
  indexing, accumulated into the bridge store.
* **Option B — central bridge config** mapping repo URLs to identities.

Option A wins:

1. **Identity ownership belongs to the repo** — the same reasoning that puts
   `name` in `package.json`/`pyproject.toml`. The team that owns the repo owns
   its declared identity; changes ride normal PR review.
2. **The bridge store already is the central registry.** Each repo's map is
   written to the shared `_bridge.omni` store at index time, so read-time
   stitching sees the union without any out-of-band config distribution. A
   central file would duplicate that role and have to be synced to every
   machine that runs the indexer.
3. **scip-clang needs a central map only because C++ has no in-repo package
   identity.** Our repos have one; they just need to state which is canonical.
4. **Incremental adoption.** A repo without the file still indexes — it gets a
   fallback identity derived from the repo URI (below). Nothing blocks on a
   40-repo rollout.

## File format

`witan-code.toml`, repo root:

```toml
[package]
name = "mit-learn"      # required: canonical package name
manager = "pypi"        # optional: package-manager namespace (default ".")
version = "main"        # optional: trunk-tracking default "main"

# Optional: additional package identities this repo publishes, as
# "manager:name" strings. Version defaults to [package].version.
provides = [
  "npm:@mitodl/course-search-utils",
]
```

* `name` — used as the `{package}` field of every provider symbol the repo
  emits (endpoints, env vars, services).
* `manager` — `pypi` / `npm` / `.`; the `{manager}` field for the primary
  identity.
* `version` — see SYMBOL_FORMAT.md § design decision 1. Leave at `main`
  unless the repo genuinely maintains parallel release lines.
* `provides` — extra published identities (a service repo that also publishes
  a client library). These feed the `known_provider_package` heuristic and
  let package-consumer symbols in other repos resolve precisely.

## Fallback identity

When the file is absent (or `[package].name` is missing / the TOML is
malformed), the identity is derived from the canonical repo URI:

* `name` = last path segment of the repo URI (`…/mitodl/mit-learn` →
  `mit-learn`)
* `manager` = `.`
* `version` = `main`
* `provides` = empty

Provider symbols therefore always carry *some* package qualifier; the TOML
only makes it authoritative.

## Storage

One `PackageMap` node per repo in the bridge store, keyed on the repo URI and
overwritten on every full-repo index (merge-by-slug):

```
node PackageMap {
    slug: String @key      // canonical repo URI
    repo: String @index
    name: String @index
    manager: String
    version: String
    provides: String?      // JSON array of "manager:name" strings
    indexed_at: DateTime
}
```

## Consumers of the map

* `bridge.write_bindings` — qualifies provider symbols with the repo's
  identity before writing bindings.
* `known_provider_package` heuristic — a package-consumer binding whose
  key matches another repo's declared `name`/`provides` boosts co-located
  endpoint-consumer confidence (+0.3), replacing reliance on incidentally
  indexed `package.json` provider rows.
* Stage-2 stitching (future) — resolves consumer `.`-package symbols against
  provider symbols, using the map to disambiguate when two repos export the
  same descriptor.
