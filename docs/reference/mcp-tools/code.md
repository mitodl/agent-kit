<!--
  GENERATED FILE — DO NOT EDIT BY HAND.
  Regenerate with `just docs-gen` (or `./bin/gen_docs.py`).
  Source of truth: the registered FastMCP tool objects
-->

# Code graph

Exact symbol lookups, caller graphs, change-impact analysis, and cross-repo contract tracing, served from a tree-sitter index. Reach for these instead of grep when you need a definition, a blast radius, or the provider of a shared env var, endpoint, package, or service.

## `code_callers`

Find symbols that call ``symbol_id`` (incoming Calls edges).

Heuristic name-resolution based; not a precise call graph. For calls plus
other references use code_find_references.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `symbol_id` | str | **required** | The symbol to start from, as returned in the ``symbol_id`` field of<br>code_find_definition or code_search_symbol. Form:<br>``<repo>#<path/to/file.py>::<QualifiedName>``. |

## `code_cross_repo_impact`

Find the cross-repo surface of a symbol.

Looks up the interface bindings attributed to ``symbol_id`` (env vars it
reads, packages it imports, endpoints it serves/calls), then returns every
binding for those same contracts in OTHER repos — i.e. who else is coupled to
this symbol across the SOA. Returns a shaped empty result when the symbol has
no cross-repo surface or the bridge store does not exist yet.

Generic env vars (DEBUG, PORT, …) are flagged ``generic`` and excluded from
the cross-repo fan-out so a trivial edit doesn't appear to touch every repo.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `symbol_id` | str | **required** | The symbol whose cross-repo surface to trace, as returned in the<br>``symbol_id`` field of code_find_definition or code_search_symbol. Its<br>repo prefix also scopes the bridge read, so asking about another repo's<br>symbol does not pick up an unrelated overlay from your own checkout. |
| `min_precision` | `precise` \| `heuristic` \| `fuzzy` | `'heuristic'` | ``heuristic`` (default) \| ``precise`` — see server instructions. |

## `code_find_definition`

Find symbol definitions whose name or qualified name matches ``name``.

Returns matching symbols (function, method, class, module, …) with their
``symbol_id``, file, line range, signature, docstring, and ``repo`` of
origin. Feed a returned ``symbol_id`` to code_find_references / code_callers
/ code_impact.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | str | **required** | Bare name (``run``) or qualified name (``Service.run``). |
| `repo` | str? | `null` | Canonical repo URI to search. Defaults to the repo detected from the<br>checkout, and fans out across every indexed repo only when no repo is<br>detected at all. Note this differs from the witan memory/task tools:<br>``repo=""`` does **not** force a fan-out here — it is falsy, so it<br>behaves exactly like omitting the argument. |
| `branch` | str? | `null` | Git branch whose indexed view to query (e.g. another agent's in-flight<br>branch). Defaults to the checkout's branch when querying the current<br>repo; when ``repo`` names a different repo and ``branch`` is omitted,<br>reads that store's default (main) view. |

## `code_find_references`

Find symbols that reference or call ``symbol_id`` (incoming edges).

Supersets code_callers (references includes calls); use code_callers when
you want calls only. Heuristic — may miss or over-report.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `symbol_id` | str | **required** | The symbol to start from, as returned in the ``symbol_id`` field of<br>code_find_definition or code_search_symbol. Form:<br>``<repo>#<path/to/file.py>::<QualifiedName>``. |

## `code_impact`

Estimate change impact: the transitive set of callers of ``symbol_id``.

Performs a breadth-first traversal over Calls edges, capping at ``max_depth``
levels and ``max_nodes`` total. Returns the reached symbols and whether the
traversal was truncated. Heuristic — see code_callers.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `symbol_id` | str | **required** | The symbol to start from, as returned in the ``symbol_id`` field of<br>code_find_definition or code_search_symbol. Form:<br>``<repo>#<path/to/file.py>::<QualifiedName>``.<br>This is the symbol you are about to change; the result is its blast<br>radius. |
| `max_depth` | int | `5` | Maximum BFS depth (default 5). |
| `max_nodes` | int | `200` | Cap on total symbols returned (default 200). |

## `code_indexed_branches`

The in-flight branch views each indexed repo's store carries, and who owns each.

A non-default git branch is indexed onto its own store view, named for the
writer as well as the branch (docs/BRANCH_INDEXING.md), so several people
— or an agent and its human, or one developer in two worktrees — can each
have a view of the same branch without overwriting one another.

Pass ``branch`` (a raw git branch name) to see every writer's view of that
one branch: this is how you find a teammate's in-flight work. Feed a
returned ``view`` straight back as the ``branch`` argument of
code_find_definition / code_search_symbol / code_symbols_in_file to read
it. Omit ``branch`` to list everything.

Each row is ``{repo, views}``, where a view is
``{view, branch, actor}`` — ``view`` the name to pass back, ``branch`` the
sanitized git branch it is a view of, ``actor`` its owner (null on a
single-writer local store). ``views`` is null for a store that could not be
listed.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `branch` | str? | `null` | A raw git branch name (not a view name) to filter by, showing every<br>writer's view of that one branch — this is how you find a teammate's<br>in-flight work. Omit to list every view of every indexed repo. |

## `code_indexed_repos`

The repositories that have a code graph indexed, and how big each is.

Use it to check coverage before trusting a negative result — a symbol
search returning nothing means something different when the repo in
question was never indexed. ``last_indexed`` is a Unix timestamp.

``files`` is None for a store that could not be read, and ``unreadable``
then carries why. Read the pair together: a repo listed with ``files: null``
is NOT a repo with little indexed, it is one whose code graph no ``code_*``
tool can query at all, and treating its empty results as "nothing found"
is a confident wrong answer. ``unreadable`` is null on a healthy store.

``bytes`` and ``last_indexed`` are both null for a graph on the shared
omnigraph-server: they describe a directory on this machine, and a client
of a shared graph has neither the directory nor any business reporting the
server's disk. ``files`` stays real — it is a query, not a walk.

*Takes no parameters.*

## `code_interface_consumers`

Find repos that CONSUME a cross-repo contract ``key`` of ``kind``.

The mirror of ``code_interface_providers`` — e.g. which repos read an env var
or call an API endpoint. Spans every indexed repo.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `kind` | `env_var` \| `package` \| `service` \| `endpoint` | **required** | ``env_var``, ``package``, ``service``, or ``endpoint``. |
| `key` | str | **required** | The contract value (``endpoint`` paths are normalized). |
| `min_precision` | `precise` \| `heuristic` \| `fuzzy` | `'heuristic'` | ``heuristic`` (default) \| ``precise`` — see server instructions. |

## `code_interface_providers`

Find repos that PROVIDE a cross-repo contract ``key`` of ``kind``.

Spans every indexed repo via the shared bridge store. Examples:
``code_interface_providers("env_var", "MITOL_APP_BASE_URL")`` returns the
ol-infrastructure binding that sets it; ``("endpoint", "/api/v1/courses/")``
returns the Django/OpenAPI route that serves it.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `kind` | `env_var` \| `package` \| `service` \| `endpoint` | **required** | ``env_var``, ``package``, ``service``, or ``endpoint``. |
| `key` | str | **required** | The contract value. For ``endpoint`` a raw path is accepted and<br>normalized (path params collapse to ``{}``). |
| `min_precision` | `precise` \| `heuristic` \| `fuzzy` | `'heuristic'` | ``heuristic`` (default) \| ``precise`` — see server instructions. |

## `code_interface_search`

BM25 search over cross-repo interface bindings (by normalized key).

Use to discover contract keys when you only remember part of a name —
e.g. ``code_interface_search("BASE_URL")`` or ``("courses", kind="endpoint")``.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `query` | str | **required** | Search terms matched against the normalized key. |
| `kind` | `env_var` \| `endpoint` \| `package` \| `service`? | `null` | Optional filter to one kind. |
| `min_precision` | `precise` \| `heuristic` \| `fuzzy` | `'heuristic'` | ``heuristic`` (default) \| ``precise`` — see server instructions. |

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

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | str? | `null` | Keep only edges whose consumer OR provider is this repo. Omit to see<br>every precise edge in the bridge store. |

## `code_reindex`

Index (or re-index) the current repo, or a subpath of it.

Incremental by default — unchanged files (matching content hash) are
skipped. Lazily creates the per-repo store on first run. Returns a summary of
files scanned/indexed/skipped and symbols/edges written.

A full rebuild runs for minutes on a large repo, so this tool accepts
task-augmented execution (`io.modelcontextprotocol/tasks`): a client that
asks for it gets a task handle back immediately and polls `tasks/get`,
instead of holding one tool call open for the whole index. Asking is the
client's choice — omit it and the call runs to completion as it always has.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | str? | `null` | Optional file or directory under the repo. Defaults to the repo root<br>(or cwd if not in a git repo). |
| `force` | bool | `False` | Re-index every file regardless of content hash. |

## `code_repo_dependencies`

The repo-to-repo dependency graph over every indexed repo.

Aggregates the bridge store's interface bindings into "repo A depends on
repo B" links (A consumes a contract B provides; for ``service`` bindings,
the deploying repo depends on what it deploys). Returns
``{"repos": [...], "edges": [{consumer, provider, weight, kinds,
contracts}]}`` — the coarse, whole-SOA view, where
``code_interface_providers``/``_consumers`` answer about one contract key.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `kind` | `env_var` \| `endpoint` \| `package` \| `service`? | `null` | Filter to one contract kind (``env_var``/``package``/``service``/``endpoint``). |
| `repo` | str? | `null` | Keep only links touching a repo whose URI contains this substring. |
| `min_precision` | `precise` \| `heuristic` \| `fuzzy` | `'heuristic'` | ``heuristic`` (default) \| ``precise`` — see server instructions. |

## `code_repo_symbols`

A repo's cross-repo symbol table (docs/SYMBOL_TABLE.md).

One row per (role, symbol): ``exported`` rows are the repo's public
contract surface — what other repos can resolve against — and ``external``
rows are the unresolved references Stage 2 joins against other repos'
exports. Use it to see what a repo publishes and what it expects from the
rest of the SOA; ``code_precise_edges`` is the resolved join over the same
table.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | str? | `null` | Canonical repo URI. Defaults to the current detected repo. |
| `role` | `exported` \| `external`? | `null` | Filter to ``exported`` or ``external`` rows. |
| `scheme` | str? | `null` | Filter to one symbol scheme (``http``/``env``/``pkg``/``svc``). |

## `code_search_symbol`

Full-text/substring search over symbol qualified names (BM25-ranked).

Use to locate a symbol when you only remember part of its name. Each result
carries its ``symbol_id`` and ``repo`` of origin. BM25 ranking is per-store;
cross-repo fan-out concatenates each store's ranked results.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `query` | str | **required** | Search terms matched against ``qualified_name``. |
| `kind` | `function` \| `method` \| `class` \| `module` \| `variable` \| `interface` \| `type` \| `enum` \| `key` \| `table` \| `cte` \| `block`? | `null` | Optional filter to a single symbol kind: ``function``, ``method``,<br>``class``, ``module``, ``variable``, ``interface``, ``type``, ``enum``,<br>``key``, ``table``, ``cte``, or ``block``. Pass e.g. ``kind="function"``<br>to exclude the many YAML ``key`` symbols when searching for code. |
| `repo` | str? | `null` | Canonical repo URI to search. Defaults to the repo detected from the<br>checkout, and fans out across every indexed repo only when no repo is<br>detected at all — ``repo=""`` does **not** force that, unlike the witan<br>memory/task tools; it is falsy and behaves like omitting the argument.<br>BM25 ranking is per-store, so a fan-out concatenates each store's<br>ranked results rather than producing one global ordering. |
| `branch` | str? | `null` | Git branch whose indexed view to query. Defaults to the checkout's<br>branch when querying the current repo; when ``repo`` names a different<br>repo and ``branch`` is omitted, reads that store's default (main) view. |

## `code_store_health`

Whether every code graph — per-repo AND the shared cross-repo bridge — opens.

The readiness check for witan-code. Call it when ``code_*`` tools return
errors, or return nothing where you expected something, before concluding
that a symbol or a consumer does not exist.

The bridge graph is why this is its own tool rather than a column on
code_indexed_repos. It belongs to no repo, so it appears in no repo
listing, yet ``code_interface_search`` / ``code_interface_providers`` /
``code_interface_consumers`` / ``code_cross_repo_impact`` all read it and
nothing else does. A bridge that cannot be opened makes every one of those
fail while every per-repo listing still looks healthy — which is how it
stayed broken for six weeks.

Returns ``{"stores": [{store, label, kind, ok, files, error,
stale_schema}], "ok": bool, "stale_schema": [store, ...]}``. ``store`` is
the full address; ``label`` is the short form that identifies WHICH graph a
row is, which for a cluster graph the address does not (it ends in the
endpoint, identical on every row). ``stale_schema`` names the
stores written by an omnigraph whose on-disk format the installed binary
no longer reads — the one failure with a known remedy, since a code graph
is derived from its checkout and is rebuilt by reindexing
(``witan-code reindex --rebuild``) rather than migrated.

*Takes no parameters.*

## `code_symbols_in_file`

List symbols defined in ``path`` (repo-relative or absolute).

A file is inherently repo-local, so this resolves a single repo (no fan-out):
the given ``repo`` or, if omitted, the current repo.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | str | **required** | Source file path. |
| `repo` | str? | `null` | Canonical repo URI the file belongs to. Defaults to the current repo. |

## `code_unresolved_symbols`

External symbol references with no precise cross-repo match.

Surfaces indexing-coverage gaps: a repo consumes a contract (env var,
package, endpoint, service) that no indexed repo currently exports —
either the provider repo isn't indexed yet, or the reference genuinely has
no provider in this SOA. These still get a heuristic-tier chance via
``code_interface_consumers``/``_providers``; this tool finds what's NOT
precisely resolved.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | str? | `null` | Keep only unresolved references from this consumer repo. Omit to see<br>every unresolved reference in the bridge store. |
