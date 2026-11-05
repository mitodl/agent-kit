### Changed

- A deployment's ingest client asserts the caller's actor to the data tier
  (`Omnigraph-If-Actor`), so on omnigraph 0.14 a token mapped to the wrong
  actor is refused instead of writing under someone else's name. `StoreRef`
  carries the `actor`.
