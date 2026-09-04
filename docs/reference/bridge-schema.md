<!--
  GENERATED FILE — DO NOT EDIT BY HAND.
  Regenerate with `just docs-gen` (or `./bin/gen_docs.py`).
  Source of truth: mcp/servers/witan-code/witan_code/schema/bridge-schema.pg
-->

# Cross-repo bridge schema

The bridge store links repositories to each other by shared contract keys — an env var, an HTTP endpoint, a package name, a service name. It is what makes `code_interface_providers` and `code_cross_repo_impact` able to answer a question that spans two checkouts.

Source of truth: [`mcp/servers/witan-code/witan_code/schema/bridge-schema.pg`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan-code/witan_code/schema/bridge-schema.pg).

## Nodes

### `InterfaceBinding`

Cross-Repo Context Bridge (Layer 2.5) — one shared store across all repos.

Deployed as the `code-bridge` graph on the shared, S3-backed omnigraph-server
cluster (config.BRIDGE_GRAPH_ID); locally it is the `_bridge.omni` store.

One flat node per interface binding. Cross-repo linkages are computed by
GROUPING bindings on (kind, key_norm) where roles/repos differ — there are
NO link edges. An anchor-node + edge model would force every repo touching a
shared contract (e.g. env_var DATABASE_URL) to upsert the same node, maxing
write contention on a store with many concurrent writers (every repo's index
run + every PostToolUse reindex hook). Flat bindings are each scoped to their
own repo+file, so concurrent writers never touch the same row.

Re-derivable, like the per-repo Layer-2 graphs; seed the shared copy by
re-indexing on the server or export→load, never by copying a local store to S3.

Id convention:
InterfaceBinding.slug = repo|file|kind|key_norm|role|symbol_id
e.g. https://github.com/mitodl/mit-learn|main/settings.py|env_var|
MITOL_APP_BASE_URL|consumer|...settings.py::&lt;module&gt;

| Field | Type | Notes |
| --- | --- | --- |
| `slug` | `String @key` |  |
| `kind` | `enum(env_var, package, service, endpoint) @index` |  |
| `key` | `String @index` | raw as written (METHOD path, pkg name, env NAME) |
| `key_norm` | `String @index` | normalized join key (collapsed path params, …) |
| `role` | `enum(provider, consumer, shared) @index` |  |
| `repo` | `String @index` | canonical HTTPS repo URI (join key to Layer 2) |
| `file` | `String @index` | repo-relative source path of the binding |
| `repo_file` | `String @index` | "repo|file" — single-field key for per-file delete |
| `sub_kind` | `String?` | service anchor variant: repo | image | name |
| `symbol_id` | `String?` | enclosing Symbol id (repo#path::Qn) when applicable |
| `line` | `I32?` |  |
| `language` | `String?` |  |
| `framework` | `String?` | django | drf | pulumi | npm | nextjs | … |
| `generic` | `String?` | "1" for stoplisted generic keys (DEBUG, PORT, …) |
| `confidence` | `F32?` | 0.0–1.0 endpoint-consumer trust score (phantom |
| `symbol` | `String? @index` | canonical symbol string (docs/SYMBOL_FORMAT.md): |
| `indexed_at` | `DateTime` |  |

### `RepoSymbol`

Per-repo symbol table (Stage 1 artifact — docs/SYMBOL_TABLE.md): one row per
(repo, role, symbol), aggregated from that repo's InterfaceBinding rows on
every bridge write. role=exported is the repo's public contract surface;
role=external is an unresolved reference to another repo's contract (a
RANGER-style import placeholder, redirected at read time by Stage 2 — no
cross-repo edges are ever written). Rows are repo-scoped, so concurrent
writers never touch the same row (same argument as InterfaceBinding).

| Field | Type | Notes |
| --- | --- | --- |
| `slug` | `String @key` | repo|role|symbol |
| `repo` | `String @index` | canonical HTTPS repo URI |
| `role` | `enum(exported, external) @index` |  |
| `symbol` | `String @index` | full canonical string (docs/SYMBOL_FORMAT.md) |
| `scheme` | `String @index` | http | env | pkg | svc |
| `descriptor` | `String @index` | precise Stage-2 join key (with scheme) |
| `key_norm` | `String @index` | coarse join key — descriptor minus the |
| `manager` | `String?` | "." = unknown |
| `package` | `String?` | "." = unresolved (typical for external) |
| `version` | `String?` |  |
| `kind` | `String` | binding kind: env_var | package | service | endpoint |
| `n_refs` | `I32` | occurrence count in this repo |
| `confidence` | `F32?` | max occurrence confidence; 1.0 for exported |
| `file` | `String?` | exemplar occurrence (deterministic: min file/line) |
| `line` | `I32?` |  |
| `indexed_at` | `DateTime` |  |

### `PackageMap`

One row per indexed repo: its declared (or fallback) package identity from
witan-code.toml (docs/PACKAGE_MAP.md). Overwritten on each full-repo index
(merge by slug). Qualifies provider symbols and backs the
known_provider_package confidence heuristic.

| Field | Type | Notes |
| --- | --- | --- |
| `slug` | `String @key` | canonical repo URI |
| `repo` | `String @index` |  |
| `name` | `String @index` | canonical package name |
| `manager` | `String` | pypi | npm | "." |
| `version` | `String` | "main" = trunk-tracking |
| `provides` | `String?` | JSON array of extra "manager:name" identities |
| `declared` | `String?` | "1" when read from witan-code.toml (vs fallback) |
| `indexed_at` | `DateTime` |  |

## Edges

Edges are directional and typed. A traversal names the edge in lowercase (`supersedes`, `blocks`), while the schema declares it in PascalCase.

An edge with properties exposes them only through a **bound** traversal — `$src $w:supersedes $dst` binds the matched edge row, making `$w.confidence` a column you can project, filter, and order on. The unbound form (`$src supersedes $dst`) still only asserts the edge exists. Binding also drops set semantics: one row per *edge*, so parallel edges between the same pair arrive as separate rows.

| Edge | From | To | Properties | Meaning |
| --- | --- | --- | --- | --- |
