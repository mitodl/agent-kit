### Changed

- `witan setup` installs omnigraph 0.15.0 (tag `v0.15.0`). Its
  `omnigraph version --json` reports storage format 16, serving 14 to 16,
  and HTTP contract 0.14, the same as 0.14.0, so no graph is rebuilt and the
  contract negotiation is unchanged. 0.15 sign-ins write a login cache that
  older binaries refuse, so log out with 0.15 before going back to 0.14.
