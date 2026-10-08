### Fixed

- A cluster graph that is not served is recognised on omnigraph 0.13, whose
  404 says `graph not found` without the quoted id. It was reported as an
  unreachable cluster.
