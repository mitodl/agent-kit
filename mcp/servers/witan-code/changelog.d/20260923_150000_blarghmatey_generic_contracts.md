### Added

- **`code_repo_dependencies` marks each contract `generic`.** A stoplisted key
  (DEBUG, PORT, ...) still makes its edge, as before, but its entry in the
  edge's `contracts` now carries `"generic": true`, so a reader can hide the
  keys that couple every repo to every other without the tool deciding for
  them. Additive: every other field is unchanged.
- **`code_interface_providers`, `code_interface_consumers` and the
  `cross_repo` rows of `code_cross_repo_impact` return each binding's
  `confidence`.** An endpoint
  consumer under 0.5 makes no `code_repo_dependencies` edge, but these tools
  return every row, so without the score a reader could not tell the rows that
  made an edge from the phantoms. Additive.
