### Fixed

- The CLI transport no longer passes a query's or mutation's params, or a
  composed batch's source, on the command line, so neither is limited by
  ARG_MAX. Linux caps a single argv element at 128 KiB, and a write past it
  (a task with a 200 KiB description, or a batch of 128 `merge_task`
  statements) failed with `Argument list too long` before omnigraph ran.
  `read`, `change` and `change_many` now write them to files in a private
  temporary directory (under `TMPDIR`, removed when the call returns) and pass
  `--params-file` and `--query <path>`. The HTTP transport is unchanged.
