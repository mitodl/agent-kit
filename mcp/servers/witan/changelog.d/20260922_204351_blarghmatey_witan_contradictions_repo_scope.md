### Fixed

- `recall(repo=X)` no longer reports a Contradicts pair with both sides in
  another repo. Topic-sibling expansion crosses repos, so recall could flag a
  pair that `memory_contradictions(repo=X)` and the memory view's inbox omit.
  Recall now reports a pair only when both memories are in its result and at
  least one is in `repo`, the same rule `memory_contradictions` scopes by, so
  every pair recall flags is in the inbox for the same `repo`. `repo=""` still
  reports every pair among the returned memories.
