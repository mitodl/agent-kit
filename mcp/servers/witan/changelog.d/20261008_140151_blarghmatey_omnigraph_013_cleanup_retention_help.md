### Changed

- `witan cleanup` no longer defaults to `--keep 10`. From omnigraph 0.12
  `--keep` counts graph commits per live branch, not versions per table, so
  that default would cut a store to its last ten writes. With neither bound
  it now keeps the last 30 days, as the Stop hook does. The help text
  describes both bounds in graph commits. `--older-than` on its own
  no longer adds an implicit `--keep 10`.
- `witan optimize` and `witan cleanup` say why they do nothing for a deployed
  (`http(s)`) graph: they are direct-storage commands, and from omnigraph 0.12
  they are refused while the server holds the cluster's `serve` lock.
