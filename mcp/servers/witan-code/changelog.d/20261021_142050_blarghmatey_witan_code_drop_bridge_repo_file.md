### Removed

- `InterfaceBinding.repo_file` is gone from the bridge schema. The per-file
  purge deletes on `repo` and `file` directly, which GQ 2.1 allows. omnigraph
  0.13 cannot drop the column in place, so the deployed `code-bridge` graph has
  to be recreated and every repo reindexed when this ships.

### Changed

- A local `_bridge.omni` whose schema omnigraph reports it cannot migrate in
  place (`schema plan` answers `supported: false`) is now deleted and recreated
  instead of failing every apply. It comes back empty. Run
  `witan-code reindex <repo root>` in each indexed repo to restore its
  cross-repo bindings.
