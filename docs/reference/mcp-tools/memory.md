<!--
  GENERATED FILE — DO NOT EDIT BY HAND.
  Regenerate with `just docs-gen` (or `./bin/gen_docs.py`).
  Source of truth: the registered FastMCP tool objects
-->

# Memory & recall

Recording durable knowledge in the shared graph, and reading it back. `recall` is the default read — it composes BM25 search, graph expansion, superseded-pruning, and re-ranking into one call. The narrower reads below exist for when you already know exactly what you want.

## `recall`

Graph-aware contextual recall — the composition of every other memory tool.

Seeds from any combination of ``query`` (BM25), ``symbol_id``
(``symbol_context``), ``task`` (memories it Addresses, those sharing its
symbol_refs, and those its title closely matches), and ``topic`` (memories
tagged to it). Expands ``hops``
(default 1, capped at 2) along AppliesTo/RelatedTo edges, topic siblings, and
provenance siblings; prunes superseded memories (unless
``include_superseded``); flags Contradicts pairs; and re-ranks with the
composite score minus a distance penalty so seeds outrank neighbours.

A Contradicts pair is reported when both memories are in the returned
result AND at least one of them is in ``repo``, the rule
``memory_contradictions`` scopes by. Expansion can return memories from
other repos, but a pair with neither side in ``repo`` is left out, so every
pair here is also in ``memory_contradictions`` for the same ``repo`` and
``include_superseded``. Pass ``repo=""`` to report every pair among the
returned memories. The one mode where the two disagree: with no repo
detected and none passed, the ``query`` seed searches every repo, so
recall reports every pair among the returned memories, as with
``repo=""``, while ``memory_contradictions`` keeps only pairs touching an
unscoped memory.

Expansion is CONFIDENCE-WEIGHTED: a neighbour reached over an ``inferred``
edge — today that means a Tagged edge promoted from a free-string tag,
rather than a link someone named — costs ``w_inferred_edge`` extra
distance, so a shared tag no longer pulls as hard as an asserted
``related_to``. The surcharge accumulates along the path, so extending an
inferred edge with an asserted one does not wash the penalty out. A
neighbour reachable both ways is scored at its best route. Edges written
before edge properties existed are unstamped and score as asserted, so this
reweights the graph as it is rewritten rather than all at once;
``WITAN_RANK_W_INFERRED_EDGE=0`` turns it off.

With no edges in the graph the result equals ``memory_search`` — expansion is
additive, never lossy. Embeddings are deferred behind ``WITAN_EMBED_ENABLED``
(default off); ``recall`` works with BM25 only and needs no embedding provider.

Returns ``{"memories": [...ranked...], "contradictions": [...], "seeds": {...}}``.
Past a total content budget a memory's ``content`` is cut, marked by
``content_truncated`` and ``content_chars``; ``memory_get`` returns the full
text.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `query` | str? | `null` | Free-text BM25 query, matched against ``content`` and ``title``. |
| `symbol_id` | str? | `null` | A code-graph symbol id (``repo#path::Name``) to seed from — the memories<br>and tasks whose ``symbol_refs`` include it. Use this before editing a<br>symbol: "what do we already know about this function?" |
| `task` | str? | `null` | A ``tk-`` slug to seed from — the memories it ``Addresses``, those<br>sharing its ``symbol_refs``, and those its title closely matches. A first<br>``task_claim`` already returns the best of these, so this is the one-call<br>way to load context for a task you are resuming or looking at without<br>claiming. |
| `topic` | str? | `null` | A Topic slug (``tp-...``) or a ``name:kind`` spec (e.g. ``uv:topic``,<br>``DATABASE_URL:contract``) to seed from. Topics are a cross-repo join<br>surface, so this seed in particular can pull in other repositories. |
| `repo` | str? | `null` | Repo scoping — see instructions. Applies to the ``query`` seed and to<br>which contradiction pairs are reported (see above); symbol, task, and<br>topic seeds resolve wherever they live, and expansion crosses repos. |
| `kind` | `pattern` \| `project_fact` \| `lesson` \| `agent_context`? | `null` | Restrict the ``query`` seed to one memory kind: ``pattern``,<br>``project_fact``, ``lesson``, or ``agent_context``. |
| `hops` | int | `1` | How far to expand from the seeds along ``AppliesTo`` / ``RelatedTo``<br>edges, topic siblings, and provenance siblings. Clamped to 0–2. ``0``<br>disables expansion and returns the seeds alone; ``1`` (the default) is<br>almost always right, since each extra hop widens results faster than it<br>deepens them. Counts HOPS, not the fractional distance the inferred-edge<br>surcharge produces — the surcharge only reranks, it never truncates the<br>walk. |
| `limit` | int | `20` | Maximum memories to return after re-ranking. |
| `include_superseded` | bool | `False` | When ``True``, keep memories that a newer memory ``Supersedes``. Default<br>``False`` hides them, which is what makes recall return current<br>knowledge rather than its history. |
| `session_id` | str? | `null` | The calling agent session's id. Only logged, never changes the result;<br>see ``memory_get``. |

## `memory_store`

Store a new memory in the shared graph.

Prefer this over your private built-in/session memory for anything durable
and team-shareable — patterns, project facts, lessons, decisions — so other
agents and future sessions can find it. Returns the slug of the created node
so callers can link to it.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `kind` | `pattern` \| `project_fact` \| `lesson` \| `agent_context` | **required** | ``pattern``      — coding convention or reusable technique<br>``project_fact`` — structural fact about a repo/service<br>``lesson``       — a correction or cautionary finding<br>``agent_context``— information a future agent on this task should know |
| `title` | str | **required** | Short, human-readable label. Used in listings and search. |
| `content` | str | **required** | Full text of the memory. Be specific: include the what, why, and any<br>examples. This is the primary search target. |
| `repo` | str? | `null` | Repo scoping — see instructions. |
| `language` | str? | `null` | Programming language (for ``pattern`` kind). e.g. ``python``, ``typescript``. |
| `category` | str? | `null` | Thematic category (for ``project_fact`` kind).<br>e.g. ``architecture``, ``deployment``, ``testing``, ``dependencies``. |
| `severity` | `info` \| `warning` \| `critical`? | `null` | Importance level (for ``lesson`` kind).<br>``info`` \| ``warning`` \| ``critical``. |
| `tags` | list[str]? | `null` | Optional list of free-form tags for grouping. |
| `symbol_refs` | list[str]? | `null` | Optional code-graph symbol ids (``<repo>#<path/to/file.py>::<Name>``,<br>from the witan-code tools' ``symbol_id`` field) this memory concerns,<br>e.g. the function a lesson is about. Stored as a soft reference. |
| `confidence` | float? | `null` | Optional author/agent trust in this memory, 0.0–1.0. Feeds the search<br>re-rank; omitted memories use the configured default. |
| `session_slug` | str? | `null` | The ``ws-`` handle returned by ``workflow_session_start``, recording<br>which session produced this memory. Pass it whenever you have one: the<br>protocol carries no session state, so against a deployed service this is<br>the only way the ``SessionProduced`` provenance edge can be created.<br>Omit it under a local stdio server, which finds the handle itself. |

## `memory_get`

Retrieve a single memory by its slug.

Returns the full node or ``null`` if not found.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``pat-`` / ``pf-`` / ``les-`` / ``ctx-`` slug to retrieve. |
| `include_topics` | bool | `False` | When ``True``, attach a ``topics`` list of the Topic nodes this memory is<br>tagged with (slug/name/kind), each with an ``edge`` dict describing the<br>link. ``edge.confidence`` is what separates a topic someone named from<br>one promoted out of the memory's free-string ``tags``. |
| `session_id` | str? | `null` | The calling agent session's id. Only logged, never changes the result:<br>it is what ties this read to the ``task_claim`` that surfaced the<br>memory. The CLI's remote proxy fills it in; under local stdio the<br>server falls back to ``$CLAUDE_SESSION_ID`` (see ``task_claim``). |

## `memory_update`

Correct a memory's fields in place. Only non-null arguments are applied.

This is the repair tool for a memory whose *content was always meant to be
what you are about to write* — a wrong ``repo`` (so it never showed up in
repo-scoped reads), a typo'd title, a missing tag. Returns the updated node,
or ``null`` if no memory has that slug.

Which tool to reach for:

- a field is wrong → ``memory_update`` (this one)
- the knowledge itself changed → ``memory_store`` the new one, then
  ``memory_link(kind="supersedes")``; the old one stays readable as history
- it should never have existed (accidental duplicate, test write) →
  ``memory_delete``
- it contains secret material → **rotate the credential.** Neither this tool
  nor ``memory_delete`` erases the old value from the graph's history.

``kind`` is deliberately not updatable: a memory that turns out to be a
different kind is a different memory — store it and supersede this one.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``pat-`` / ``pf-`` / ``les-`` / ``ctx-`` slug of the memory to<br>correct. |
| `title` | str? | `null` | New short, human-readable label. |
| `content` | str? | `null` | New full text. Replaces the existing content rather than appending —<br>and note that rewriting content here leaves no record that it changed.<br>If the *knowledge* changed, store a new memory and supersede this one<br>instead. |
| `repo` | str? | `null` | Canonical repo URI to (re)scope the memory to. Case-folded on write, as<br>every other repo-key path does: correcting a mis-scoped memory is this<br>tool's headline use, and a key that does not match what repo detection<br>returns would just mis-scope it again, differently. |
| `language` | str? | `null` | Programming language (``pattern`` kind). e.g. ``python``, ``typescript``. |
| `category` | str? | `null` | Thematic category (``project_fact`` kind). e.g. ``architecture``,<br>``deployment``, ``testing``, ``dependencies``. |
| `severity` | `info` \| `warning` \| `critical`? | `null` | Importance (``lesson`` kind): ``info`` \| ``warning`` \| ``critical``. |
| `tags` | list[str]? | `null` | Free-form tags. Replaces the existing list. Each tag is also promoted to<br>a ``Topic`` and linked. **Tags removed here keep their ``Tagged``<br>edge** — edges cannot be individually retracted, and deleting the Topic<br>to drop one would take out every other memory's edge to it. The string<br>list stays authoritative for what the memory claims to be tagged with. |
| `symbol_refs` | list[str]? | `null` | Code-graph symbol ids (``repo#path::Name``) this memory concerns.<br>Replaces the existing list. |
| `confidence` | float? | `null` | Author/agent trust in this memory, 0.0–1.0. Feeds the recall re-rank. |

## `memory_delete`

Hard-delete a memory. Graph hygiene only — NOT a way to erase secrets.

Use when a memory should never have existed: an accidental duplicate, a test
write, a node created against the wrong graph. For anything else prefer
``memory_update`` (a field is wrong) or ``memory_store`` +
``memory_link(kind="supersedes")`` (the knowledge changed) — superseding is
the soft delete, and it keeps the history legible.

**This does not erase content.** The row remains fully readable, content
included, from any prior commit of the graph. If a memory captured a
credential, the fix is to **rotate the credential**; scrubbing history is an
admin ``omnigraph cleanup``, which no MCP tool performs.

Deleting a node also removes its incident edges in both directions, so a
deleted memory leaves no dangling ``Supersedes``/``RelatedTo``/``Tagged``
behind. Topic nodes on the far end of a ``Tagged`` edge survive and may be
left with no memories.

That is not the only way to remove an edge — edges are deletable on their
own (``task_unlink``, ``_unlink_edge``). Deleting the node is still the
only route for a *memory* edge, since no unlink tool covers those yet.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The memory to delete permanently. |
| `confirm` | bool | `False` | Must be ``True``. Without it this is a no-op returning<br>``{"deleted": False, "reason": ...}``. |

## `memory_list`

List memories (no search), optionally filtered by kind, repo, and/or language.

Browse stored memories without a search query — e.g. all ``lesson`` or
``pattern`` memories. Ordered most-recent first. To load context prefer
``recall``; use this for a plain kind-scoped listing (e.g.
``memory_list(kind="project_fact")`` at session start, or
``memory_list(kind="pattern", language="python")`` before writing code).

Returns at most the 100 most recent matching memories. Every filter,
including dropping superseded memories, applies before that cap, so fewer
than 100 rows is the whole listing and exactly 100 means there may be
more. The slim unscoped listing has no 100-row cap. A ``language``-filtered
or slim listing reads the 10,000 most recent memories (current ones only,
unless ``include_superseded``) and filters those, so it is complete only
while the store holds fewer than 10,000 of them.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `kind` | `pattern` \| `project_fact` \| `lesson` \| `agent_context`? | `null` | Optional filter: ``pattern``, ``project_fact``, ``lesson``, or<br>``agent_context``. Omit to list all kinds. |
| `repo` | str? | `null` | Repo scoping — see instructions. With no repo detected and none passed,<br>returns slim records (slug, kind, title, tags — no content) for unscoped<br>memories; ``memory_get`` a slug for its full content. |
| `language` | str? | `null` | Optional filter by ``language`` (e.g. ``python``), case-insensitive;<br>applies to the full-content results, not the slim unscoped listing. |
| `include_superseded` | bool | `False` | When ``True``, keep memories that a newer memory ``Supersedes``. Default<br>``False`` drops them, as ``memory_search`` and ``recall`` do. |

## `memory_search`

Plain BM25 text search over memories (no graph expansion — for that use
``recall``).

Returns the top-20 memories ranked by BM25 relevance. Superseded memories are
hidden unless ``include_superseded=True``. Past a total content budget a
memory's ``content`` is cut, marked by ``content_truncated`` and
``content_chars``; ``memory_get`` returns the full text.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `query` | str | **required** | Free-text search query. Searched against ``content`` and ``title``.<br>A title-only match never outranks a content match on relevance: BM25<br>scores the two fields on scales that cannot be compared, so which one<br>wins is a policy rather than a measurement, and this is the policy.<br>Final order is the composite score, which also weighs recency,<br>corroboration and confidence, so a well-corroborated title-only hit<br>can still finish above a marginal content hit. |
| `repo` | str? | `null` | Repo scoping — see instructions. |
| `kind` | `pattern` \| `project_fact` \| `lesson` \| `agent_context`? | `null` | Optional filter: ``pattern``, ``project_fact``, ``lesson``,<br>or ``agent_context``. |
| `include_superseded` | bool | `False` | When ``True``, keep memories that a newer memory ``Supersedes``. Default<br>``False`` drops them. |

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

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `from_slug` | str | **required** | The memory the edge points **from**. Direction is load-bearing for the<br>asymmetric kinds — for ``supersedes`` this is the *newer* memory. |
| `to_slug` | str | **required** | The memory the edge points **to** — for ``supersedes``, the older memory<br>being replaced. For ``kind="tagged"`` this is a Topic instead: either an<br>existing ``tp-`` slug or a ``name:kind`` spec, which auto-creates it. |
| `kind` | `supersedes` \| `refines` \| `applies_to` \| `contradicts` \| `related_to` \| `tagged` | **required** | Which edge to write: ``supersedes`` \| ``refines`` \| ``applies_to`` \|<br>``contradicts`` \| ``related_to`` \| ``tagged``. See the descriptions<br>above — ``supersedes`` is the one that changes what default reads<br>return. |
| `role` | str? | `null` | Free text saying WHY these two are linked, stored on the edge and<br>returned by ``memory_neighbors``. The edge kind says what KIND of<br>relation it is; this says what the relation is ABOUT ("both configure<br>the Vault sidecar", "same incident"). Worth a sentence on a<br>``related_to``, where the kind alone tells a later reader nothing.<br>Scanned on the way in like any other free text, so it can be redacted<br>before it lands. The returned ``edge`` therefore reports the three<br>machine-set properties and NOT ``role`` — read it back with<br>``memory_neighbors`` to see what was actually stored. |
| `confidence` | `asserted` \| `inferred` | `'asserted'` | ``asserted`` (default) when you are naming both endpoints because you<br>know they belong together; ``inferred`` when the link is a guess worth<br>recording but not worth ranking on. ``recall`` expands along asserted<br>edges in preference to inferred ones, so this is the knob that decides<br>whether a speculative link pulls its neighbour into other people's<br>results. |

## `memory_neighbors`

Return the memories directly linked to ``slug``, grouped by edge kind.

For symmetric kinds (``contradicts``, ``related_to``) both directions are
unioned and de-duplicated. ``superseded_by`` is the inbound side of
``supersedes``: the memories that replaced this one, which is what a reader
who landed on a superseded memory needs next. Use after ``memory_get`` to
see what a memory connects to.

Each neighbour carries an ``edge`` dict — ``{confidence, role, author,
created_at}`` — describing the LINK rather than either endpoint. ``role``
is what answers "why are these two connected", which the edge kind alone
does not; ``created_at`` is when the link was made, which neither node's
timestamps record. All four are ``null`` on edges written before the
properties existed, and nothing backfills them.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The memory whose neighbours to fetch. |
| `kinds` | list[`supersedes` \| `refines` \| `applies_to` \| `contradicts` \| `related_to` \| `tagged` \| `superseded_by`]? | `null` | Optional subset of edge kinds to include. Omit (``None``) for all kinds;<br>an explicit empty list returns no kinds. |

## `memory_contradictions`

List every pair of memories that contradict each other, newest link first.

An unranked enumeration of the ``contradicts`` edges, for reviewing
conflicts rather than loading context. ``recall`` reports a contradiction
only when both memories land in its result, and ``memory_neighbors`` needs a
slug to start from; this needs neither. Both apply the same ``repo`` rule
(below), so every pair ``recall`` reports is one this lists for the same
``repo`` and ``include_superseded``. The exception is no repo detected and
none passed: ``recall``'s ``query`` seed then spans every repo, so it
reports every pair among its result, while this keeps only pairs touching
an unscoped memory.

Each row is ``{"a": {...}, "b": {...}, "edge": {...}}``. ``a`` and ``b``
carry ``slug, title, kind, repo, author, updated_at, content, confidence``;
``a`` is the side the link was made from. ``edge`` is ``{confidence, role, author, created_at}``,
all ``null`` on links written before edge properties existed.

One row per unordered pair. ``contradicts`` is symmetric but can be stored
in both directions; when it is, the newer link is the one reported, the same
newest-wins rule as ``memory_neighbors``.

Superseding either side is how a contradiction gets resolved, so a pair
with a superseded memory in it is dropped by default. That is also what
keeps this in agreement with ``recall``, which prunes superseded memories
before it looks for pairs.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | str? | `null` | Repo scoping — see instructions. A pair is included when EITHER memory<br>is in scope, since a contradiction across two repos concerns both. With<br>no repo detected and none passed, only pairs touching an unscoped memory<br>(``repo`` null) are returned. |
| `include_superseded` | bool | `False` | When ``True``, keep pairs where either memory has been superseded, i.e.<br>the resolved ones. |

## `memory_symbols`

Code symbols a memory concerns (direction: memory → symbols).

The reverse of ``symbol_context``: returns each of the memory's ``symbol_refs``,
enriched with the live definition when witan-code is reachable, or the raw ref
strings otherwise.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The memory whose ``symbol_refs`` to resolve. An unknown slug returns an<br>empty ``symbols`` list rather than raising. |

## `memory_for_contract`

What do we know about a contract (env_var / endpoint / package / service)?

Resolves the ``Topic{kind:"contract", name:key_norm}`` anchor and walks
``Tagged`` to the memories about it (a single Layer-1 traversal, cross-repo by
nature), then — best-effort — asks witan-code for the bridge bindings that
share the same ``key_norm`` so callers can pivot to the code that produces or
consumes it. The two halves are joined in Python on the shared key, never an
edge across stores.

Tag a memory to a contract first with
``memory_link(memory_slug, "<key_norm>:contract", "tagged")`` (``from`` is the
memory's slug).

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `key_norm` | str | **required** | The normalised contract key (e.g. ``DATABASE_URL`` or<br>``GET /api/v1/courses/``). |
| `kind` | `env_var` \| `endpoint` \| `package` \| `service`? | `null` | The bridge binding kind (``env_var`` / ``endpoint`` / ``package`` /<br>``service``) used to look up bindings. Omit to skip the bridge lookup. |

## `symbol_context`

Memories and tasks attached to a code symbol (direction: symbol → work).

The reverse of ``memory_symbols``: given a symbol id, returns the memories and
tasks whose ``symbol_refs`` include it — "what lessons and open tasks concern
this function?". Call it after locating a symbol with the witan-code ``code_*``
tools, before editing.

``symbol_id`` has the form ``<repo>#<path/to/file.py>::<QualifiedName>``; the
repo prefix scopes the lookup, or the current repo when the id has no ``#``.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `symbol_id` | str | **required** | The symbol to look up, as returned in the ``symbol_id`` field of the<br>witan-code ``code_*`` tools. |

## `topic_get`

Resolve a Topic and return it with the memories tagged to it.

``topic`` is either a Topic slug (``tp-...``) or a ``name:kind`` spec
(e.g. ``uv:topic``). Because topics are a cross-repo join surface, the
returned memories may span repositories — this is the traversal-based
retrieval primitive: two memories in different repos sharing a topic are
one hop apart.

Returns ``{"topic": {...}, "memories": [...]}`` or ``null`` if no such Topic.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `topic` | str | **required** | Either a Topic slug (``tp-...``) or a ``name:kind`` spec (``uv:topic``,<br>``DATABASE_URL:contract``). A spec resolves through the same<br>deterministic slugify used on write rather than an exact name match, so<br>a tag stored as ``UV`` is still found by ``uv:topic``. |
