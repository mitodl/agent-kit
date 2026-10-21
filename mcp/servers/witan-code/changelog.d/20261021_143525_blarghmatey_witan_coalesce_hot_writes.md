### Changed

- A bridge write clears the repo's symbol table in the same commit as its
  per-file bindings purge, instead of a separate one. Every index that writes
  bindings now costs the bridge one commit fewer, on a graph whose main branch
  every repo shares and whose commits serialize.
