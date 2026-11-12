### Fixed

- Errors a command prints itself (a misconfigured remote, a config that will
  not load, `No task ...`/`No project ...`, a refused `migrate merge`) now go
  to stderr. They went to stdout, so under `--output-format json|toml|yaml` a
  consumer parsing stdout got a sentence instead of a document. `print_error`
  no longer takes `stderr=`; it always writes to stderr.
