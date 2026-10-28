### Changed

- Every omnigraph command witan builds itself (`schema apply`, `upgrade`,
  `snapshot`, `export`, `load` in `witan migrate storage`, and the export in
  `witan migrate merge`) addresses its store with `store_cli_args`, so it
  takes `--target` on omnigraph 0.14 and `--store` before it. 0.14 removed
  the positional store URI from `schema apply` and `upgrade`.
- `witan migrate merge` decides whether a store is the configured one by its
  resolved server and graph (`store_address`) rather than by comparing CLI
  flags, which now depend on the binary.
- `config/operator-config.example.yaml` is the omnigraph 0.14 format (a
  `direct` target and `defaults.target`, with a commented server target). It
  documents running the CLI by hand; witan passes its own generated operator
  config on 0.14 and does not read this file.
- Raised the `witan-core` floor to `>=0.47` for `store_address` and
  `store_cli_args(..., binary=)`.
