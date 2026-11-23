### Changed

- `witan setup` installs omnigraph 0.16.0 (tag `v0.16.0`). Its
  `omnigraph version --json` reports storage format 17, serving 14 to 17, and
  HTTP contract 0.14, so `_OMNIGRAPH_INTERNAL_SCHEMA` moves to 17 and the
  contract negotiation is unchanged. An existing format-16 store opens and is
  written in place and stays at 16, so nothing is rebuilt. A store 0.16
  creates is born at 17, which 0.15 refuses, so going back to 0.15 after
  `witan setup` creates a store means rebuilding that store.
