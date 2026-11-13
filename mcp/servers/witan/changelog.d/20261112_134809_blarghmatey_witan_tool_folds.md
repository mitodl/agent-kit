### Removed

- Breaking: `task_unlink` is gone. Pass `remove=True` to `task_link`, with the
  same `from_slug`, `to_slug` and `kind`, to remove a link. The result and the
  `blocked_by`/`parent_slug` resync are unchanged. `witan task unlink` still
  works and calls it.
- Breaking: `workflow_project_search` is gone. Pass `query` to
  `workflow_project_list` for the same BM25 search over title and description,
  ranked and capped at 20. Its `repo`, `status`, `phase` and `ready` filters
  apply to the matches.
- Breaking: `workflow_project_block`, `workflow_project_unblock` and
  `workflow_project_get_blockers` are gone. Call `workflow_project_update` on
  the blocked project with `add_blocked_by=[<blocker>]` or
  `remove_blocked_by=[<blocker>]`, the same shape as `add_repos`/`remove_repos`.
  A self-block now raises `ValueError` instead of returning `linked: false`.
  `workflow_project_get` gains a `blockers` list with each blocker's `slug`,
  `title`, `status` and `phase`, the fields `witan project show` read from
  `get_blockers`. `witan project block`/`unblock` keep their names.
- Breaking: `topic_get`, `symbol_context`, `memory_neighbors`,
  `memory_symbols` and `memory_for_contract` are gone; each is now a `recall`
  seed. A seed that has something to say about itself adds a section to the
  result, present only when its argument was passed:
  - `topic_get(topic)` is `recall(topic=…)`. Its `topic` section is the Topic
    node, or `null` for none.
  - `symbol_context(symbol_id)` is `recall(symbol_id=…)`. Its `symbol`
    section lists the tasks.
  - `memory_neighbors(slug)` and `memory_symbols(slug)` are
    `recall(memory=slug)`. Its `memory` section holds `neighbors` (every edge
    kind, each with its `edge` properties) and `symbols`. The `kinds` filter
    is gone; read the kinds you want from the result.
  - `memory_for_contract(key_norm, kind)` is
    `recall(contract=key_norm, contract_kind=kind)`. Its `contract` section
    holds the Topic and the witan-code `bindings`.

  The memories come back in recall's ranked list. Pass `hops=0`,
  `include_superseded=True` and a high `limit` to get exactly the set the old
  tool returned, with nothing expanded in or pruned out (it is still ranked
  rather than in store order). The web UI's memory panel
  and topic page read through `recall` now.

### Fixed

- `recall(memory=…)`'s symbol definitions and `recall(contract=…)`'s code
  bindings are awaited. witan-code's read tools are `async`, so
  `memory_symbols` stored an un-awaited coroutine as each `definition` and
  `memory_for_contract` returned coroutines as its `bindings`.
- A `name:kind` topic spec whose name starts with `tp-` (e.g.
  `tp-client:contract`) resolves as a spec on read when no Topic has that
  literal slug. `memory_link` already created `tp-contract-tp-client` for it,
  but `topic_get` and `recall(contract="tp-client")` looked up the literal
  slug and found nothing.
