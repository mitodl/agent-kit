### Added

- `memory_stale_refs` (and `witan memory stale-refs`) resolves every current
  memory's `symbol_refs` against witan-code and lists the memories with a ref
  that is `missing` (its repo is indexed and the symbol is not) or `unknown`
  (not a symbol id, the repo is not indexed or its store is unreadable, or
  witan-code is unreachable). It only reports. A missing ref carries
  `candidates`, the indexed symbols with the same name, since a move or
  rename leaves the memory true.

### Fixed

- `memory_symbols` stored an un-awaited coroutine as each ref's `definition`,
  and `memory_for_contract` returned coroutines as its `bindings`, because
  witan-code's read tools are `async`. Both now await the call.
