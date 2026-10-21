### Changed

- `task_claim` writes its CodeBranch upsert and `WorksOn` edge in the claim's
  own fenced commit instead of a second one, so a claim or lease renewal costs
  one graph commit instead of two. On omnigraph 0.13 same-branch commits
  serialize at about 1s each, which caps a graph at about 0.9 writes/s. A
  claim refused or lost to a rival still writes neither, and a retry rebuilds
  the branch steps from its own read.
