### Changed

- The `witan` and `omnigraph-server` images bake omnigraph 0.16.0 and label
  storage format 17 (`edu.mit.ol.omnigraph.internal-schema`), still serving
  from 14. Deployed graphs stay at format 16: 0.16 never stamps an existing
  graph 17, and `omnigraph upgrade` refuses cluster graphs. A graph a 0.16
  `cluster apply` creates is born at 17, which a 0.15 image cannot open, so
  rolling back to 0.15 after one is created strands it. The HTTP contract
  stays 0.14.
