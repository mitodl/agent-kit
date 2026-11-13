<!--
  GENERATED FILE — DO NOT EDIT BY HAND.
  Regenerate with `just docs-gen` (or `./bin/gen_docs.py`).
  Source of truth: the registered FastMCP tool objects
-->

# Memory & recall

Recording durable knowledge in the shared graph, and reading it back. `recall` is the default read — it composes BM25 search, graph expansion, superseded-pruning, and re-ranking into one call. The narrower reads below exist for when you already know exactly what you want.

## `recall`

Graph-aware contextual recall — the composition of every other memory tool.

Seeds from any combination of ``query`` (BM25), ``symbol_id`` (memories
whose symbol_refs include it), ``task`` (memories it Addresses, those
sharing its symbol_refs, and those its title closely matches), ``topic``
and ``contract`` (memories tagged to it), and ``memory`` (that memory).
Expands ``hops``
(default 1, capped at 2) along AppliesTo/RelatedTo edges, topic siblings, and
provenance siblings; prunes superseded memories (unless
``include_superseded``); flags Contradicts pairs; and re-ranks with the
composite score minus a distance penalty so seeds outrank neighbours.

A Contradicts pair is reported when both memories are returned and at
least one is in ``repo`` (with ``all_repos=True``, any pair). A neighbour
reached over an ``inferred`` edge ranks below one over an asserted edge.
With no edges in the graph the result equals ``memory_search``.

Returns ``{"memories": [...ranked...], "contradictions": [...], "seeds": {...}}``.
Past a total content budget a memory's ``content`` is cut, marked by
``content_truncated`` and ``content_chars``; ``memory_get`` returns the full
text.

Some seeds also add a section about the seed itself, which the ranked list
has no room for. Each key is present only when its argument was passed:

- ``topic``: the resolved Topic node, or ``null`` when there is none.
- ``symbol``: ``{"symbol_id", "tasks"}``, the tasks whose ``symbol_refs``
  include it (direction: symbol -> work).
- ``memory``: ``{"slug", "neighbors", "symbols"}``. ``neighbors`` holds the
  memories directly linked to it, grouped by edge kind (``supersedes``,
  ``superseded_by``, ``refines``, ``applies_to``, ``related_to``,
  ``contradicts``; every kind present, each possibly empty, symmetric kinds
  unioned over both directions), each carrying an ``edge`` dict
  (``{confidence, role, author, created_at}``, all ``null`` on links made
  before edge properties existed). ``symbols`` is each of its
  ``symbol_refs`` as ``{"symbol_ref", "definition"?}`` (direction: memory
  -> symbols), the definition looked up in witan-code when it is reachable.
- ``contract``: ``{"key_norm", "kind", "topic", "bindings"}``, where
  ``bindings`` is ``{"providers", "consumers"}`` from witan-code's bridge
  store when ``contract_kind`` is given and witan-code is reachable, and
  empty lists otherwise.

To list exactly what a seed resolves to, pass ``hops=0``,
``include_superseded=True`` and a ``limit`` above the expected count.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `query` | str? | `null` | Free-text query, matched against ``content`` and ``title``. |
| `symbol_id` | str? | `null` | Seed from memories and tasks whose ``symbol_refs`` include this symbol.<br>Use it before editing a symbol. |
| `task` | str? | `null` | Seed from a ``tk-`` task: memories it addresses, shares symbols with,<br>or matches by title. Loads context for a task without claiming it. |
| `topic` | str? | `null` | Seed from a ``tp-`` slug or ``name:kind`` spec (e.g. ``uv:topic``).<br>Crosses repos. |
| `repo` | str? | `null` | Repo scoping; see instructions. Scopes the ``query`` seed and which<br>contradiction pairs are reported; other seeds and expansion cross<br>repos. |
| `kind` | `pattern` \| `project_fact` \| `lesson` \| `agent_context`? | `null` | Restrict the ``query`` seed to one memory kind. |
| `hops` | int | `1` | Expansion depth, 0 to 2. ``0`` returns the seeds alone; ``1`` is<br>almost always right. |
| `limit` | int | `20` | Maximum memories to return after re-ranking. |
| `include_superseded` | bool | `False` | Also return superseded memories (history, not current knowledge). |
| `session_id` | str? | `null` | Your agent session id; only logged. |
| `memory` | str? | `null` | Seed from a memory slug; adds the ``memory`` section (its links and<br>the code symbols it names). |
| `contract` | str? | `null` | Seed from a normalised contract key (e.g. ``DATABASE_URL``) through its<br>``<key>:contract`` Topic. Adds the ``contract`` section. Tag a memory<br>to one with ``memory_link(<slug>, "<key_norm>:contract", "tagged")``. |
| `contract_kind` | `env_var` \| `endpoint` \| `package` \| `service`? | `null` | With ``contract``, also look up the code that provides or consumes the<br>key in witan-code. |
| `all_repos` | bool | `False` | Seed ``query`` from every repo and report every contradiction pair. |

## `memory_store`

Store a new memory in the shared graph.

Prefer this over your private built-in/session memory for anything durable
and team-shareable — patterns, project facts, lessons, decisions — so other
agents and future sessions can find it. Returns the slug of the created node
so callers can link to it.

*Writes (non-destructive).*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `kind` | `pattern` \| `project_fact` \| `lesson` \| `agent_context` | **required** | ``pattern`` (a convention or reusable technique), ``project_fact`` (a<br>structural fact about a repo or service), ``lesson`` (a correction or<br>cautionary finding) or ``agent_context`` (what a future agent on this<br>task should know). |
| `title` | str | **required** | Short label, used in listings and search. |
| `content` | str | **required** | Full text: the what, the why, and examples. The primary search target. |
| `repo` | str? | `null` | Repo scoping; see instructions. |
| `language` | str? | `null` | For a ``pattern``, e.g. ``python``. |
| `category` | str? | `null` | For a ``project_fact``, e.g. ``architecture``, ``deployment``. |
| `severity` | `info` \| `warning` \| `critical`? | `null` | For a ``lesson``. |
| `tags` | list[str]? | `null` | Free-form tags for grouping. |
| `symbol_refs` | list[str]? | `null` | Symbol ids this memory concerns, e.g. the function a lesson is about. |
| `confidence` | float? | `null` | Your trust in it, 0.0 to 1.0. Feeds ranking. |
| `session_slug` | str? | `null` | See instructions. Pass it whenever you have one. |
| `all_repos` | bool | `False` | Store under no repo; see instructions. |

## `memory_get`

Retrieve a single memory by its slug.

Returns the full node or ``null`` if not found.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The memory slug. |
| `include_topics` | bool | `False` | Attach the Topics this memory is tagged with, each with its ``edge``<br>(``edge.confidence`` is ``inferred`` for a topic promoted from a tag). |
| `session_id` | str? | `null` | Your agent session id; only logged. See instructions. |

## `memory_update`

Correct a memory's fields in place. Only non-null arguments are applied.

For a field that was always wrong: a mis-scoped ``repo``, a typo'd title,
a missing tag. If the knowledge itself changed, store a new memory and
supersede this one (see instructions). ``kind`` is not updatable; a
different kind is a different memory. Returns the updated node, or
``null`` if no memory has that slug.

*Writes (destructive, idempotent).*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The memory slug. |
| `title` | str? | `null` | New short label. |
| `content` | str? | `null` | Replacement text. Leaves no record that it changed. |
| `repo` | str? | `null` | Repo URI to rescope the memory to. |
| `language` | str? | `null` | For a ``pattern``, e.g. ``python``. |
| `category` | str? | `null` | For a ``project_fact``, e.g. ``architecture``. |
| `severity` | `info` \| `warning` \| `critical`? | `null` | For a ``lesson``. |
| `tags` | list[str]? | `null` | Replaces the list. Each tag becomes a Topic; a removed tag keeps its<br>``Tagged`` edge. |
| `symbol_refs` | list[str]? | `null` | Symbol ids this memory concerns. Replaces the list. |
| `confidence` | float? | `null` | Your trust in it, 0.0 to 1.0. Feeds ranking. |

## `memory_delete`

Hard-delete a memory that should never have existed: an accidental
duplicate, a test write, a node in the wrong graph. Otherwise update or
supersede it (see instructions). Author-only.

Not a way to erase secrets: prior commits keep the content, so rotate the
credential. The memory's edges go with it, both directions; Topics on the
far end survive.

*Writes (destructive, idempotent).*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The memory to delete permanently. |
| `confirm` | bool | `False` | Must be ``True``; otherwise a no-op returning ``{"deleted": False}``. |

## `memory_list`

List memories (no search), optionally filtered by kind, repo, and/or language.

Browse stored memories without a search query — e.g. all ``lesson`` or
``pattern`` memories. Ordered most-recent first. To load context prefer
``recall``; use this for a plain kind-scoped listing (e.g.
``memory_list(kind="project_fact")`` at session start, or
``memory_list(kind="pattern", language="python")`` before writing code).

Returns at most the 100 most recent matches, after every filter, so fewer
than 100 rows is the whole listing. The slim unscoped listing has no cap.
A ``language``-filtered or slim listing filters the 10,000 most recent
memories.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `kind` | `pattern` \| `project_fact` \| `lesson` \| `agent_context`? | `null` | Filter to one memory kind. Omit for all kinds. |
| `repo` | str? | `null` | Repo scoping; see instructions (including the slim unscoped listing). |
| `language` | str? | `null` | Filter by language (e.g. ``python``), case-insensitive. Not applied to<br>the slim listing. |
| `include_superseded` | bool | `False` | Also return superseded memories. |
| `all_repos` | bool | `False` | List every repo's memories, with full content. |

## `memory_search`

Plain BM25 text search over memories (no graph expansion — for that use
``recall``).

Returns the top-20 memories ranked by BM25 relevance. Superseded memories are
hidden unless ``include_superseded=True``. Past a total content budget a
memory's ``content`` is cut, marked by ``content_truncated`` and
``content_chars``; ``memory_get`` returns the full text.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `query` | str | **required** | Free-text query, matched against ``content`` and ``title``. |
| `repo` | str? | `null` | Repo scoping; see instructions. |
| `kind` | `pattern` \| `project_fact` \| `lesson` \| `agent_context`? | `null` | Filter to one memory kind. |
| `include_superseded` | bool | `False` | Also return superseded memories. |
| `all_repos` | bool | `False` | Search every repo; see instructions. |

## `memory_link`

Create a typed edge between two memories.

- ``supersedes``  — ``from`` (newer) replaces ``to`` (older). ``to`` is hidden
                    from default ``memory_search`` results.
- ``refines``     — ``from`` sharpens/extends ``to`` without replacing it.
- ``applies_to``  — ``from`` (a pattern/lesson) applies in the context of ``to``
                    (a project_fact).
- ``contradicts`` — ``from`` and ``to`` conflict. Symmetric; surfaced for
                    review, never hidden.
- ``related_to``  — soft association. Symmetric.
- ``tagged``      — ``from`` (a Memory) is about ``to`` (a Topic). ``to`` is
                    either an existing Topic slug (``tp-...``) or a ``name:kind``
                    spec (e.g. ``cryptography:topic``, ``DATABASE_URL:contract``),
                    in which case the Topic is auto-created.

For memory↔memory kinds both endpoints must already exist as ``Memory`` nodes;
the edge is not written otherwise (avoids dead off-type edges). A memory cannot
link to itself. Returns ``linked: False`` in those cases rather than raising.

*Writes (destructive).*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `from_slug` | str | **required** | The edge's source; for ``supersedes``, the newer memory. |
| `to_slug` | str | **required** | The edge's target; for ``supersedes``, the older memory. For<br>``tagged``, a ``tp-`` slug or a ``name:kind`` spec. |
| `kind` | `supersedes` \| `refines` \| `applies_to` \| `contradicts` \| `related_to` \| `tagged` | **required** | The edge kind; see above. |
| `role` | str? | `null` | Why these two are linked (e.g. "same incident"), stored on the edge.<br>Worth a sentence on ``related_to``. Read it back with<br>``recall(memory=...)``; the returned ``edge`` omits it. |
| `confidence` | `asserted` \| `inferred` | `'asserted'` | ``asserted`` when you know they belong together, ``inferred`` for a<br>guess. ``recall`` ranks neighbours over inferred edges lower. |

## `memory_contradictions`

List every pair of memories that contradict each other, newest link first.

For reviewing conflicts: needs no seed, where ``recall`` reports a pair
only when both memories land in its result.

Each row is ``{"a": {...}, "b": {...}, "edge": {...}}``. ``a`` and ``b``
carry ``slug, title, kind, repo, author, updated_at, content, confidence``;
``a`` is the side the link was made from. ``edge`` is ``{confidence, role,
author, created_at}``. One row per unordered pair. Superseding either side
resolves a contradiction, so those pairs are dropped by default.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | str? | `null` | Repo scoping; see instructions. A pair is in scope when either memory<br>is. With no repo, only pairs touching an unscoped memory. |
| `include_superseded` | bool | `False` | Also return resolved pairs (either side superseded). |
| `all_repos` | bool | `False` | Every pair, in any repo. |

## `memory_stale_refs`

Memories whose ``symbol_refs`` no longer resolve in the code graph.

A review list, never an edit. Resolves every current memory's
``symbol_refs`` against witan-code and reports each ref as ``live`` (the
symbol is still defined), ``missing`` (its repo is indexed and the symbol is
not), or ``unknown`` (not a ``repo#path::Name`` id, the repo has no readable
index, or witan-code is unreachable). A missing symbol may have been renamed
or moved, so the memory can still be true: ``candidates`` lists the indexed
symbols with the same name to check first, then ``memory_update`` the refs
or supersede the memory.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | str? | `null` | Repo scoping; see instructions. |
| `all_repos` | bool | `False` | Check every repo's memories. |
