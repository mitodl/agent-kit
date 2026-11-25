### Changed

- `witan task claim` prints the lease the server granted alongside the
  assignee, e.g. `(assignee=alice, lease 120 minutes)`. A lease that is not
  whole minutes is printed in seconds, as the `witan task run` prompt states
  it.
