### Changed

- A branch view on a deployed server reads, writes and lists its commits
  over HTTP, with the branch as a request field, instead of through the
  omnigraph CLI, which 0.14 refuses for a plain-http server.
  `branch_last_write` (the reaper's idleness check) uses
  `OmnigraphClient.commit_list`.
- Raised the `witan-core` floor to `>=0.49` for `commit_list` and for the
  branched HTTP transport.
