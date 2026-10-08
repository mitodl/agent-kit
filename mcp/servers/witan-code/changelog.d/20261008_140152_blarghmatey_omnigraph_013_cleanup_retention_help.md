### Changed

- `witan-code cleanup` no longer defaults to `--keep 10`. From omnigraph 0.12
  `--keep` counts graph commits per live branch, not versions per table, so
  that default would cut a store to its last ten writes. With neither bound
  it now keeps the last 30 days, as the Stop hook does. The help text
  describes both bounds in graph commits. `--older-than` on its own
  no longer adds an implicit `--keep 10`.
- The refusal for a shared cluster graph notes that from omnigraph 0.12
  compaction is also refused while the server holds the cluster's `serve`
  lock.
