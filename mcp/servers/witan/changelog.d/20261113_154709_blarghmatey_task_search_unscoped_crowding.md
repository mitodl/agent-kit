### Fixed

- A repo-scoped `task_search` returns its unscoped (`repo=None`) matches
  however many matches in other repos outrank them. The unscoped half was
  picked out of the all-repos read after that read's cap, so 20 better
  matches elsewhere (10,000 with a `limit` above 20) left it empty. It now
  reads through its own query pair with a repo-null predicate. The `assignee`
  filter also moved into the query, ahead of the same cap, and selects only
  that holder and its `#<session>` qualifiers, so tasks held by identities
  that merely contain it (`xagentA` for `agentA`) cannot fill the cap.
