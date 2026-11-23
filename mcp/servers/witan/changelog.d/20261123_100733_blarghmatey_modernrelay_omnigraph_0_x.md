### Changed

- The Cedar bundles grant `witan-admin` the two actions 0.16 adds,
  `maintain` (optimize and index builds) and `reclaim` (cleanup), on the
  memory, code and bridge graphs. 0.16 judges a direct maintenance command on
  a cluster graph by the graph's applied policy and denies one that names no
  actor (upstream #1423), so without them the nightly window's every
  `optimize` and `cleanup` is refused.
- The maintenance window passes `OMNIGRAPH_MAINTENANCE_ACTOR` to `optimize`
  and `cleanup` as `--as`, and fails before stopping the server when the
  config directory holds policy bundles and it is unset. The `omnigraph`
  stack's maintenance CronJob has to set it to `svc-witan-admin` before this
  image deploys.
