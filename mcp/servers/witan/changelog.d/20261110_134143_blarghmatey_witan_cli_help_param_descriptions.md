### Fixed

- `--help` shows every parameter's description. Most commands documented
  parameters as `name: text` on one line, which cyclopts' numpydoc parser
  reads as a parameter type, so `--help` printed nothing for them or only
  the wrapped continuation line (e.g. `--remove-repo  after additions.`).
