### Added

- `OmnigraphClient.commit_list(branch)`: the commits reachable from a
  branch's head. A deployed server is asked with
  `GET /graphs/<id>/commits?branch=…` (`PooledTransport.commits`), a local
  store with `omnigraph commit list`. Output that is not a commit array
  raises.

### Changed

- A client that injects `--branch` into `query` and `mutate` (witan-code's
  branch views) now uses the pooled HTTP transport against a deployed server
  and sends the branch as the request body's `branch`, which the 0.13.0 and
  0.14.0 servers both read. Previously those calls shelled out, which the
  0.14 CLI refuses for a plain-http server. An injected arg with no body
  field still keeps the client on the CLI. A missing branch is a 404
  `not_found` and fails on the first attempt.
- Group-commit lanes are keyed on the injected args too, so writes to two
  branches are never committed together.
