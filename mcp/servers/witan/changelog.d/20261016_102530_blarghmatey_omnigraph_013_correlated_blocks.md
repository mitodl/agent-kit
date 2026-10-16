### Changed

- `memory_list`'s language-filtered and slim listings and
  `memory_contradictions` drop superseded memories in the query with
  `not { $_ supersedes $m }`, instead of reading the superseded set
  separately and filtering in Python. That is one read instead of two, and
  the language-filtered and slim listings now prune before their 10,000-row
  cap rather than after it.
- The ranking edge index reads the superseded and contradicted sets with
  `exists { }` blocks: one row per memory rather than per edge, and one
  read instead of two for the contradicted set.
