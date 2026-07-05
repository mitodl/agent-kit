# Canonical symbol strings for cross-repo bindings

Status: accepted (discovery phase, 2026-07-05)
Related: [PACKAGE_MAP.md](PACKAGE_MAP.md)

Every interface binding written to the bridge store carries a **canonical
symbol string** — a stable, self-contained identifier modeled on
[SCIP symbols](https://github.com/sourcegraph/scip/blob/main/scip.proto).
Symbols are the Stage-2 join key for precise cross-repo linking: a consumer
binding whose unresolved symbol matches a provider binding's exported symbol is
a `:CALLS/precise` edge; the existing `(kind, key_norm)` grouping remains as
the `:CALLS/heuristic` fallback.

## Format

```
{scheme}:{manager}:{package}:{version}:{descriptor}
```

Five colon-separated fields. The descriptor is the final field and may itself
contain colons — parsers must split with `maxsplit=4`. An empty/unknown field
is written as `.` (SCIP's empty-field convention). A literal `:` or `%` inside
any of the first four fields is percent-encoded (`%3A`, `%25`); descriptors are
never encoded (they are terminal).

| Field      | Meaning                                                        |
|------------|----------------------------------------------------------------|
| scheme     | Contract transport/kind: `http`, `env`, `pkg`, `svc` (future: `grpc`, `ws`) |
| manager    | Package-manager namespace: `pypi`, `npm`, `.`                  |
| package    | Canonical package name from the repo's [package map](PACKAGE_MAP.md) |
| version    | Package version; `main` = trunk-tracking, `.` = unknown        |
| descriptor | Kind-specific identifier (see below)                           |

## Descriptors by binding kind

### `endpoint` → scheme `http`

```
{METHOD} {normalized-path}
```

* Method is upper-case; a consumer whose method cannot be determined
  statically uses `*` (wildcard).
* The path is normalized exactly as `key_norm` today
  (`bridge_extractors.normalize_endpoint`): path parameters
  (`{id}`, `${x}`, `:id`) collapse to `{}`, duplicate slashes collapse, one
  trailing slash is stripped, and any `scheme://host` prefix is dropped.
* Query parameters are **not** part of the descriptor — they select a
  representation, not the resource, and no consumer/provider pair would agree
  on them syntactically.

```
provider:  http:pypi:mit-learn:main:GET /api/v0/users/me
consumer:  http:.:.:.:* /api/v0/users/me
```

### `env_var` → scheme `env`

Descriptor is the variable name, verbatim.

```
provider:  env:.:mit-learn:main:MITOL_APP_BASE_URL
consumer:  env:.:.:.:MITOL_APP_BASE_URL
```

### `package` → scheme `pkg`

The symbol identifies the package itself; the descriptor is `.`. Manager and
package name come from the import site (consumer) or the publishing repo's
package map / `package.json` (provider).

```
provider:  pkg:npm:@mitodl/course-search-utils:main:.
consumer:  pkg:npm:@mitodl/course-search-utils:.:.
```

### `service` → scheme `svc`

Descriptor is `{sub_kind}/{key_norm}` (sub_kind: `repo` | `image` | `name`).

```
provider:  svc:.:ol-infrastructure:main:repo/https://github.com/mitodl/mit-learn
```

## Provider vs consumer symbols

**Providers** get a fully-qualified symbol: package/manager/version come from
the repo's package map (or its fallback identity — see PACKAGE_MAP.md).
Exported symbols are the repo's public contract surface.

**Consumers** emit *unresolved external symbols* (the SCIP pattern for
dependencies indexed separately): package, manager, and version are `.` unless
the reference site names the package explicitly (package imports do; endpoint
path literals do not). Stage 2 resolves them at read time by matching the
scheme + descriptor against other repos' provider symbols; the package map
disambiguates when more than one repo exports the same descriptor.

This is deliberate: per-repo indexing stays self-contained (no repo needs any
other repo checked out or indexed first), and re-indexing repo B never
invalidates repo A's rows.

## Design decisions (task open questions)

1. **Version is `main`, not a package version or git SHA.** These services
   deploy continuously from trunk; a git SHA would churn the join key on every
   commit and version pinning across SOA repos is not practiced here.
   Published libraries may override `version` in their package map when
   parallel release lines actually exist. Read-time matching rule: a consumer
   version of `.` matches any provider version; on multiple provider versions
   prefer exact match, then `main`, else flag the edge `ambiguous_version`
   rather than guessing.
2. **Path parameters** normalize to `{}` — same rule as `key_norm`, so precise
   and heuristic tiers agree on path shape.
3. **Query parameters** are stripped.
4. **Transport lives in the scheme** (`http` now; `grpc`, `ws` reserved), not
   in the descriptor — a gRPC method descriptor (`package.Service/Method`) has
   nothing in common with an HTTP path, so overloading one scheme would push
   transport dispatch into every descriptor parser.
5. **env-var and package symbols** use the same 5-field frame with
   kind-appropriate schemes (`env`, `pkg`) so Stage-2 joining is a single
   mechanism keyed on `(scheme, descriptor)` — not one bespoke joiner per kind.

## Relationship to `key_norm`

`symbol` does not replace `key_norm`. `key_norm` remains the heuristic-tier
join key and the FTS target for `search_bindings`. `symbol` adds the
precision tier on top: identical descriptors with compatible package identity
⇒ `:CALLS/precise`; `key_norm` match without symbol agreement stays
`:CALLS/heuristic` with its confidence score.

## Storage

`InterfaceBinding.symbol` (indexed, nullable) in `bridge-schema.pg`. Symbols
are computed at bridge-write time (`bridge.write_bindings`), not extraction
time, because provider identity comes from the package map which is loaded
once per repo. The pure construction function is
`bridge_extractors.canonical_symbol`.
