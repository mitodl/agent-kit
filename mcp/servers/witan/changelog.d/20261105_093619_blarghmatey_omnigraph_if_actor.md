### Changed

- A deployment's per-actor omnigraph client asserts its actor to the data
  tier (`Omnigraph-If-Actor`), so on omnigraph 0.14 a token mapped to the
  wrong actor is refused instead of writing under someone else's name.
