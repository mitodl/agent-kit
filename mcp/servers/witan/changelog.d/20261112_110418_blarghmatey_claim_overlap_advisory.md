### Added

- A first `task_claim` (not a renewal) returns `overlaps`: other in-flight
  tasks (`in_progress` with a live lease) whose `symbol_refs` share a symbol or
  a file with the claimed task's, shared symbols first. It is advisory and never
  refuses the claim, and a failed lookup returns an empty list rather than
  failing it. Each first claim logs a `witan.claim.overlaps` line with the
  count, so the share of claims that found one is countable.
