### Fixed

- A remote failure or refusal caught by the `witan` entrypoint (an unreachable
  deployment, a Cedar denial, a missing tool, a local `Refusal`) now prints to
  stderr. It went to stdout, so under `--output-format json|toml|yaml` a
  consumer parsing stdout got a sentence instead of a document. This also
  covers `witan code …`, which runs under this entrypoint rather than
  witan-code's own.
