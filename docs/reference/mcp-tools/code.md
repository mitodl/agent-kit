<!--
  GENERATED FILE — DO NOT EDIT BY HAND.
  Regenerate with `just docs-gen` (or `./bin/gen_docs.py`).
  Source of truth: the registered FastMCP tool objects
-->

# Code graph

Exact symbol lookups, caller graphs, change-impact analysis, and cross-repo contract tracing, served from a tree-sitter index. Reach for these instead of grep when you need a definition, a blast radius, or the provider of a shared env var, endpoint, package, or service.

## `code_callers`

Find symbols that call ``symbol_id``, or that ``symbol_id`` calls.

Follows Calls edges into ``symbol_id`` by default, or out of it with
``direction="callees"``. Heuristic name-resolution based; not a precise
call graph. For calls plus other references use code_find_references.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `symbol_id` | str | **required** | The symbol to start from (a ``symbol_id`` from a name-based tool). |
| `direction` | `callers` \| `callees` | `'callers'` | ``callers`` (default) or ``callees``. Calls resolve only to symbols in<br>the same file, so a call into another module is not listed. |

## `code_cross_repo_impact`

Find the cross-repo surface of a symbol.

Looks up the interface bindings attributed to ``symbol_id`` (env vars it
reads, packages it imports, endpoints it serves/calls), then returns every
binding for those same contracts in OTHER repos — i.e. who else is coupled to
this symbol across the SOA. Returns a shaped empty result when the symbol has
no cross-repo surface or the bridge store does not exist yet.

Generic env vars (DEBUG, PORT, …) are flagged ``generic`` and excluded from
the cross-repo fan-out so a trivial edit doesn't appear to touch every repo.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `symbol_id` | str | **required** | The symbol whose cross-repo surface to trace. |
| `min_precision` | `precise` \| `heuristic` \| `fuzzy` | `'heuristic'` | See instructions. |
| `branch` | str? | `null` | Branch whose bridge overlay for ``symbol_id``'s repo to read; see<br>instructions. A branch with no overlay reads bridge main. |

## `code_find_definition`

Find symbol definitions whose name or qualified name matches ``name``.

Returns matching symbols (function, method, class, module, …) with their
``symbol_id``, file, line range, signature, docstring, and ``repo`` of
origin. Feed a returned ``symbol_id`` to code_find_references / code_callers
/ code_impact.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | str | **required** | Bare name (``run``) or qualified name (``Service.run``). |
| `repo` | str? | `null` | Repo to search; see instructions. |
| `branch` | str? | `null` | Branch or view to read; see instructions. |

## `code_find_references`

Find symbols that reference or call ``symbol_id`` (incoming edges).

Supersets code_callers (references includes calls); use code_callers when
you want calls only. Heuristic — may miss or over-report.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `symbol_id` | str | **required** | The symbol to start from (a ``symbol_id`` from a name-based tool). |

## `code_impact`

Estimate change impact: the transitive set of callers of ``symbol_id``.

Performs a breadth-first traversal over Calls edges, capping at ``max_depth``
levels and ``max_nodes`` total. Returns the reached symbols and whether the
traversal was truncated. Heuristic — see code_callers.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `symbol_id` | str | **required** | The symbol to start from (a ``symbol_id`` from a name-based tool). |
| `max_depth` | int | `5` | Maximum BFS depth (default 5). |
| `max_nodes` | int | `200` | Cap on total symbols returned (default 200). |
| `branch` | str? | `null` | Branch or view of ``symbol_id``'s repo to walk; see instructions.<br>Callers in other repos are read from their default view. |

## `code_indexed_branches`

The in-flight branch views each indexed repo's store carries, and who owns each.

A non-default branch is indexed onto a view per writer
(docs/BRANCH_INDEXING.md). Pass ``branch`` to see every writer's view of
it, which is how you find a teammate's in-flight work, and pass a returned
``view`` back as the ``branch`` argument of the name-routed tools.

Each row is ``{repo, views}``, where a view is
``{view, branch, actor}`` — ``view`` the name to pass back, ``branch`` the
sanitized git branch it is a view of, ``actor`` its owner (null on a
single-writer local store). ``views`` is null for a store that could not be
listed.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `branch` | str? | `null` | A git branch name (not a view name). Omit to list every view. |

## `code_indexed_repos`

The repositories that have a code graph indexed, and how big each is.

Use it to check coverage before trusting a negative result — a symbol
search returning nothing means something different when the repo in
question was never indexed. ``last_indexed`` is a Unix timestamp.

``files: null`` means the store could not be read (``unreadable`` says
why), so no ``code_*`` tool can query that repo: its empty results are not
"nothing found". ``bytes`` and ``last_indexed`` are null for a graph on the
shared omnigraph-server.

*Read-only.*

*Takes no parameters.*

## `code_interface_consumers`

Find repos that CONSUME a cross-repo contract ``key`` of ``kind``.

The mirror of ``code_interface_providers`` — e.g. which repos read an env var
or call an API endpoint. Spans every indexed repo.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `kind` | `env_var` \| `package` \| `service` \| `endpoint` | **required** | ``env_var``, ``package``, ``service``, or ``endpoint``. |
| `key` | str | **required** | The contract value (``endpoint`` paths are normalized). |
| `min_precision` | `precise` \| `heuristic` \| `fuzzy` | `'heuristic'` | See instructions. |
| `in_repo` | str? | `null` | Keep only the bindings in this exact repo URI. Omitted, every repo. |

## `code_interface_providers`

Find repos that PROVIDE a cross-repo contract ``key`` of ``kind``.

Spans every indexed repo via the shared bridge store. Examples:
``code_interface_providers("env_var", "MITOL_APP_BASE_URL")`` returns the
ol-infrastructure binding that sets it; ``("endpoint", "/api/v1/courses/")``
returns the Django/OpenAPI route that serves it.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `kind` | `env_var` \| `package` \| `service` \| `endpoint` | **required** | ``env_var``, ``package``, ``service``, or ``endpoint``. |
| `key` | str | **required** | The contract value. For ``endpoint`` a raw path is accepted and<br>normalized (path params collapse to ``{}``). |
| `min_precision` | `precise` \| `heuristic` \| `fuzzy` | `'heuristic'` | See instructions. |
| `in_repo` | str? | `null` | Keep only the bindings in this exact repo URI. Omitted, every repo. |

## `code_interface_search`

BM25 search over cross-repo interface bindings (by normalized key).

Use to discover contract keys when you only remember part of a name —
e.g. ``code_interface_search("BASE_URL")`` or ``("courses", kind="endpoint")``.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `query` | str | **required** | Search terms matched against the normalized key. |
| `kind` | `env_var` \| `endpoint` \| `package` \| `service`? | `null` | Optional filter to one kind. |
| `min_precision` | `precise` \| `heuristic` \| `fuzzy` | `'heuristic'` | See instructions. |

## `code_precise_edges`

Precise cross-repo edges resolved by canonical symbol string.

Joins every repo's unresolved external-symbol references against other
repos' exported symbols — a read-time join, distinct from the coarser
(kind, key_norm) heuristic grouping ``code_interface_*`` use. Each edge
carries ``match_count`` (how many providers a reference joined to) and
``ambiguous_version`` (true when more than one provider survives version
disambiguation); filter to ``preferred`` edges to narrow a fan-out to its
best candidate(s). A reference with no precise match shows up in
``code_unresolved_symbols`` instead.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | str? | `null` | Keep only edges whose consumer OR provider is this repo. Omit to see<br>every precise edge in the bridge store. |

## `code_reindex`

Index (or re-index) the current repo, or a subpath of it.

Incremental by default — unchanged files (matching content hash) are
skipped. Lazily creates the per-repo store on first run. Returns a summary of
files scanned/indexed/skipped and symbols/edges written.

Supports task-augmented execution (`io.modelcontextprotocol/tasks`) for a
client that would rather poll than hold the call open.

*Writes (destructive, idempotent).*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | str? | `null` | File or directory under the repo. Defaults to the repo root. |
| `force` | bool | `False` | Re-index every file regardless of content hash. |

## `code_repo_dependencies`

The repo-to-repo dependency graph over every indexed repo.

Aggregates the bridge store's interface bindings into "repo A depends on
repo B" links (A consumes a contract B provides; for ``service`` bindings,
the deploying repo depends on what it deploys). Returns
``{"repos": [...], "edges": [{consumer, provider, weight, kinds,
contracts}]}`` — the coarse, whole-SOA view, where
``code_interface_providers``/``_consumers`` answer about one contract key.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `kind` | `env_var` \| `endpoint` \| `package` \| `service`? | `null` | Filter to one contract kind (``env_var``/``package``/``service``/``endpoint``). |
| `repo` | str? | `null` | Keep only links touching a repo whose URI contains this substring. |
| `min_precision` | `precise` \| `heuristic` \| `fuzzy` | `'heuristic'` | See instructions. |

## `code_repo_symbols`

A repo's cross-repo symbol table (docs/SYMBOL_TABLE.md).

One row per (role, symbol): ``exported`` rows are the repo's public
contract surface — what other repos can resolve against — and ``external``
rows are the unresolved references Stage 2 joins against other repos'
exports. Use it to see what a repo publishes and what it expects from the
rest of the SOA; ``code_precise_edges`` is the resolved join over the same
table.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | str? | `null` | Canonical repo URI. Defaults to the current detected repo. |
| `role` | `exported` \| `external`? | `null` | Filter to ``exported`` or ``external`` rows. |
| `scheme` | str? | `null` | Filter to one symbol scheme (``http``/``env``/``pkg``/``svc``). |

## `code_search_symbol`

Full-text search over symbol qualified names and docstrings (BM25-ranked).

Use to locate a symbol when you only remember part of its name, or by what
its docstring says it does (e.g. "rate limiting"). Each result carries its
``symbol_id`` and ``repo`` of origin.

Name matches come first, then symbols whose docstring matches and whose
name does not. Each group holds up to 50 results per store.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `query` | str | **required** | Search terms matched against ``qualified_name`` and ``docstring``. |
| `kind` | `function` \| `method` \| `class` \| `module` \| `variable` \| `interface` \| `type` \| `enum` \| `key` \| `table` \| `cte` \| `block`? | `null` | Filter to one symbol kind. ``kind="function"`` leaves out the many<br>YAML ``key`` symbols when searching for code. |
| `repo` | str? | `null` | Repo to search; see instructions. |
| `branch` | str? | `null` | Branch or view to read; see instructions. |

## `code_store_health`

Whether every code graph — per-repo AND the shared cross-repo bridge — opens.

The readiness check for witan-code. Call it when ``code_*`` tools return
errors, or return nothing where you expected something, before concluding
that a symbol or a consumer does not exist.

Covers the cross-repo bridge graph too, which the interface and
cross-repo tools read and no repo listing shows.

Returns ``{"stores": [{store, label, kind, ok, files, error,
stale_schema}], "ok": bool, "stale_schema": [store, ...]}``. ``label``
names which graph a row is. ``stale_schema`` lists stores the installed
omnigraph can no longer read; rebuild them with
``witan-code reindex --rebuild``.

*Read-only.*

*Takes no parameters.*

## `code_symbols_in_file`

List symbols defined in ``path`` (repo-relative or absolute).

A file is inherently repo-local, so this resolves a single repo (no fan-out):
the given ``repo`` or, if omitted, the current repo.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | str | **required** | Source file path. |
| `repo` | str? | `null` | Repo the file belongs to. Defaults to the current repo. |
| `branch` | str? | `null` | Branch or view to read; see instructions. |

## `code_unresolved_symbols`

External symbol references with no precise cross-repo match.

Surfaces indexing-coverage gaps: a repo consumes a contract (env var,
package, endpoint, service) that no indexed repo currently exports —
either the provider repo isn't indexed yet, or the reference genuinely has
no provider in this SOA. These still get a heuristic-tier chance via
``code_interface_consumers``/``_providers``; this tool finds what's NOT
precisely resolved.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | str? | `null` | Keep only unresolved references from this consumer repo. Omit to see<br>every unresolved reference in the bridge store. |
