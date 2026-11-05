### Added

- `OmnigraphClient(..., actor=...)` asserts the actor its token belongs to on
  every remote request: `Omnigraph-If-Actor` over the pooled transport
  (query, mutate, load, export, commit list) and `--as` on a write over the
  0.14 CLI. A 0.14 server refuses a token that resolves to anyone else with
  `412 actor_mismatch` before doing anything; a 0.13 server ignores the
  header. Without `actor`, requests are unchanged.
- `ACTOR_MISMATCH` classifies that refusal. It is terminal and never a lost
  race: a compare-and-swap caller gets a `RuntimeError` naming the actor,
  not `OmnigraphConflict`, and group commit fails every member of the group
  instead of reading the 412 as the fenced member's failed precondition.
