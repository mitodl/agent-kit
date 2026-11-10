### Fixed

- `witan migrate schema` against a served graph now refuses up front with the
  path that works: omnigraph changes a served graph's schema only through
  `cluster apply`, which omnigraph-server runs on every start, so deploy or
  restart omnigraph-server. With an omnigraph 0.14+ CLI it used to fail on
  plain HTTP to a non-loopback server, which pointed at TLS, a fix that would
  still have been refused.
