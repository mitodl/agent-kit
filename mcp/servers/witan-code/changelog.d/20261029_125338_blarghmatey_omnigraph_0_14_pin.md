### Changed

- Raised the `witan-core` floor to `>=0.50`, which pins omnigraph 0.14
  (storage format 16) and speaks HTTP contract 0.14 or 0.13, whichever the
  server stamps. A 0.14 data tier refuses an older client's requests.
