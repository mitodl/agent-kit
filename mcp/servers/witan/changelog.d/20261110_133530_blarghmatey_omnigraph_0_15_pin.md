### Changed

- The `witan` and `omnigraph-server` images bake omnigraph 0.15.0. Storage
  format (16) and HTTP contract (0.14) are unchanged. The `omnigraph-server`
  image takes the release CLI again instead of building the patched one,
  because 0.15 accepts an inline `s3://` cluster root (upstream #1189) and
  lists `cleanup` versions (upstream #1188), the two changes the patch
  backported. Its `edu.mit.ol.omnigraph.cli-source` label is gone.
