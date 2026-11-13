### Added

- Every tool carries MCP annotations. The `code_*` queries and the read-only
  `code_store_*` tools are `readOnlyHint`; `code_reindex` and
  `code_store_open` are non-destructive and idempotent; the
  `code_store_mutate*` and `code_store_load` tools are destructive.

### Changed

- Parameter descriptions are cut to what an agent needs to choose a value.
  `repo`, `branch`, `symbol_id` and `min_precision` point at the server
  instructions instead of repeating them. The manifest drops from 25,598 to
  21,157 characters.
