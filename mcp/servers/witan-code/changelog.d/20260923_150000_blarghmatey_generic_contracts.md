### Added

- **`code_repo_dependencies` marks each contract `generic`.** A stoplisted key
  (DEBUG, PORT, ...) still makes its edge, as before, but its entry in the
  edge's `contracts` now carries `"generic": true`, so a reader can hide the
  keys that couple every repo to every other without the tool deciding for
  them. Additive: every other field is unchanged.
