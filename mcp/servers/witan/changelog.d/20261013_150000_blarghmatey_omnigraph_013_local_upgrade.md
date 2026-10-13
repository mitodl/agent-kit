### Changed

- `witan migrate storage` converts the store in place with `omnigraph upgrade`
  when the current binary offers that route, which omnigraph 0.13 does for a
  format-9 store written by 0.11. Commit history and branches are kept and no
  old binary is needed. A copy of the store is taken first as
  `<store>.pre-migrate`. Older stores are still rebuilt by export and load.
  The result carries `method`: `upgrade` or `rebuild`.
