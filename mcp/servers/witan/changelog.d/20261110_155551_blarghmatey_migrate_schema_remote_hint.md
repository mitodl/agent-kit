### Fixed

- A deployment no longer tells a caller to run `witan migrate schema` when its
  graph is missing an edge property or the `TaskComment` type. That command
  calls `apply_schema`, which the remote CLI refuses, so the hint was a dead
  end in exactly the case it fires. A deployment now says the schema ships in
  the omnigraph-server image and is applied when that image is deployed.
