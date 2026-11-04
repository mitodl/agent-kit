### Changed

- Raised the `witan-core` floor to `>=0.50`, which pins omnigraph 0.14
  (storage format 16) and sends `Omnigraph-Http-Api: 0.14`. A 0.14 data tier
  refuses an older client's requests.
- The `witan` image bakes the omnigraph 0.14.0 CLI and labels itself
  `edu.mit.ol.omnigraph.internal-schema=16`. The `omnigraph-server` image
  ships the 0.14.0 server with the patched CLI (`OMNIGRAPH_CLI_SOURCE=patched`)
  so its entrypoint and maintenance scripts can still reach the S3 cluster
  root.
