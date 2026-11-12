### Fixed

- More CLI errors now print to stderr: a misconfigured remote, a config that
  will not load (`witan task run`), `No task ...` from `task close` and the
  other task and project commands that still printed it to stdout, and a
  refused `witan migrate merge`, including the accounting printed after a merge
  that failed part-way. They went to stdout, so under
  `--output-format json|toml|yaml` a consumer parsing stdout got a sentence
  instead of a document, and a redirected `migrate merge` log kept the totals
  but not the error that explained them.
