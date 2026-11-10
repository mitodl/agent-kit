### Fixed

- `--help` for `reap-views`, `setup` and the global options shows each
  parameter's description. They were documented as `name: text` on one
  line, which cyclopts' numpydoc parser reads as a parameter type.
