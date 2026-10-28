### Changed

- The `schema plan` check on a local code store addresses the store with
  `store_cli_args`, so it takes `--target` on omnigraph 0.14 (which removed
  the positional store URI) and `--store` before it. Code and bridge store
  `init` passes the generated operator config on 0.14 (`init_cli_args`).
- Raised the `witan-core` floor to `>=0.47` for `store_cli_args(..., binary=)`.
